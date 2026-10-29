import asyncio
import logging

import aiohttp
import discord

log = logging.getLogger(__name__)


class VerificationNotices:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, row, *, nickname_pending=False, contact=False, clan_tag=""):
        """Send at most one confirmed role notice per link or clan reservation."""
        # Caller holds the bot lock and has confirmed the relevant role assignment.
        pending = (
            self.bot.store.contact_notice(row["discord_id"])
            if contact
            else self.bot.store.verification_notice(row["discord_id"])
        )
        if pending is None or (pending["region"], pending["account_id"]) != (
            row["region"],
            row["account_id"],
        ):
            return
        if contact:
            reservation = self.bot.store.contact(row["discord_id"])
            if reservation is None or (reservation["region"], reservation["clan_id"]) != (
                pending["region"],
                pending["clan_id"],
            ):
                return
            role_id = self.bot.config.clan_role_id
            clan_tag = clan_tag or pending["clan_tag"]
            key = "contact_role_pending_nickname" if nickname_pending else "contact_role_confirmed"
        else:
            role_id = self.bot.config.role_id
            key = (
                "verification_role_pending_nickname"
                if nickname_pending
                else "verification_role_confirmed"
            )
        panel = self.bot.store.panel(self.bot.config.guild_id)
        channel_id = panel["channel_id"] if panel is not None else pending["channel_id"]
        if not channel_id:
            return
        try:
            await asyncio.wait_for(
                self._send(channel_id, row["discord_id"], role_id, key, clan_tag), timeout=5
            )
        except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError):
            log.warning(
                "Verification confirmation could not be posted for member %s", row["discord_id"]
            )
            return
        if contact:
            self.bot.store.mark_contact_notice_delivered(row["discord_id"])
        else:
            self.bot.store.mark_notice_delivered(row["discord_id"])

    async def _send(self, channel_id, discord_id, role_id, key, clan_tag):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if (
            not isinstance(channel, discord.abc.Messageable)
            or getattr(getattr(channel, "guild", None), "id", None) != self.bot.config.guild_id
        ):
            raise ValueError("Invalid verification channel")
        await channel.send(
            self.bot.t(key).format(
                member=f"<@{discord_id}>",
                role=f"<@&{role_id}>",
                clan_tag=discord.utils.escape_markdown(clan_tag),
            ),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=discord_id)],
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )
