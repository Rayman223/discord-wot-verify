import asyncio
import logging

import aiohttp
import discord

log = logging.getLogger(__name__)


class VerificationNotices:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, row, *, nickname_pending=False):
        """Caller holds the bot lock and has confirmed player-role assignment."""
        pending = self.bot.store.verification_notice(row["discord_id"])
        if pending is None or (pending["region"], pending["account_id"]) != (
            row["region"],
            row["account_id"],
        ):
            return
        panel = self.bot.store.panel(self.bot.config.guild_id)
        channel_id = panel["channel_id"] if panel is not None else pending["channel_id"]
        if not channel_id:
            return
        try:
            await asyncio.wait_for(
                self._send(channel_id, row["discord_id"], nickname_pending), timeout=5
            )
        except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError):
            log.warning(
                "Verification confirmation could not be posted for member %s", row["discord_id"]
            )
            return
        self.bot.store.mark_notice_delivered(row["discord_id"])

    async def _send(self, channel_id, discord_id, nickname_pending):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.bot.config.guild_id
        ):
            raise ValueError("Invalid verification channel")
        key = (
            "verification_role_pending_nickname"
            if nickname_pending
            else "verification_role_confirmed"
        )
        await channel.send(
            self.bot.t(key).format(
                member=f"<@{discord_id}>", role=f"<@&{self.bot.config.role_id}>"
            ),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=discord_id)],
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )
