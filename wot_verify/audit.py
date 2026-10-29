import asyncio
import logging
import time

import aiohttp
import discord

log = logging.getLogger(__name__)
EVENTS = frozenset({"linked", "unlinked", "contact_added", "contact_removed", "sync_failed"})


class AuditJournal:
    def __init__(self, bot):
        self.bot = bot

    async def emit(self, event, discord_id):
        channel_id = getattr(self.bot.config, "log_channel_id", 0)
        if not channel_id or event not in EVENTS:
            return
        try:
            await asyncio.wait_for(self._send(channel_id, event, int(discord_id)), timeout=5)
        except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError, TypeError):
            # Never propagate logging failures or include URLs/tokens/exception bodies.
            log.warning("Moderator journal unavailable")

    async def _send(self, channel_id, event, discord_id):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.bot.config.guild_id
        ):
            raise ValueError("Invalid journal channel")
        guild = channel.guild
        # Default members must be excluded; explicit grants only to moderators/bot.
        if channel.permissions_for(guild.default_role).view_channel:
            raise ValueError("Journal channel is public")
        for target, overwrite in channel.overwrites.items():
            if overwrite.view_channel is not True:
                continue
            target = await self._resolve_target(guild, target)
            if target is None:
                continue
            if isinstance(target, discord.Role):
                privileged = target.permissions.administrator or target.permissions.manage_roles
                is_bot = (
                    guild.me is not None
                    and target.is_bot_managed()
                    and target.tags.bot_id == guild.me.id
                )
            elif isinstance(target, discord.Member):
                privileged = (
                    target.guild_permissions.administrator or target.guild_permissions.manage_roles
                )
                is_bot = guild.me is not None and target.id == guild.me.id
            else:
                raise TypeError("Unknown permission target")
            if not privileged and not is_bot:
                raise ValueError("Journal channel grants access to non-moderators")
        await channel.send(
            f"<t:{int(time.time())}:F> · {self.bot.t('audit_' + event)} · Discord ID: {discord_id}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _resolve_target(self, guild, target):
        """Resolve SDK cache placeholders; ignore only confirmed absent targets."""
        if not isinstance(target, discord.Object):
            return target
        if target.type is discord.Role:
            return next((role for role in await guild.fetch_roles() if role.id == target.id), None)
        if target.type in {discord.User, discord.Member}:
            try:
                return await guild.fetch_member(target.id)
            except discord.NotFound:
                return None
        raise TypeError("Unknown permission target")
