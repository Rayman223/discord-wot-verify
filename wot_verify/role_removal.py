"""Private self-service withdrawal from the player and clan contact roles."""

import logging
import time

import aiohttp
import discord

log = logging.getLogger(__name__)
DISCORD_IO_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError)


def all_removed_message(bot, cleaned):
    key = "unlinked" if cleaned else "unlinked_tag_pending"
    return bot.t(key) + "\n" + bot.t("remove_role_visitor")


async def remove_all(bot, discord_id):
    # Unlike /unlink, this UI explicitly promises to restore the visitor role.
    if not getattr(bot.config, "visitor_role_id", 0):
        raise ValueError("remove_role_visitor_required")
    cleaned = await bot.remove(discord_id)
    return all_removed_message(bot, cleaned)


def error_message(bot, error):
    log.warning("Role withdrawal failed: %s", type(error).__name__)
    return bot.t(str(error)) if isinstance(error, ValueError) else bot.t("discord_error")


async def request_role_removal(bot, interaction):
    if interaction.guild_id != bot.config.guild_id:
        return await interaction.response.send_message(bot.t("wrong_server"), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    view = None
    try:
        async with bot.lock:
            member = await bot.member(interaction.user.id)
            roles = {role.id for role in member.roles}
            if getattr(bot.config, "clan_role_id", 0) in roles:
                view = RoleRemovalView(bot, interaction.user.id)
                message = bot.t("remove_role_choice")
            elif (
                bot.config.role_id in roles
                or bot.store.get(member.id)
                or bot.store.contact(member.id)
                or bot.store.contact_tags(member.id)
            ):
                message = await remove_all(bot, member.id)
            else:
                message = bot.t("remove_role_nothing")
    except (*DISCORD_IO_ERRORS, ValueError) as error:
        message = error_message(bot, error)
    await interaction.followup.send(message, view=view, ephemeral=True)


class RoleRemovalView(discord.ui.View):
    def __init__(self, bot, owner_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.owner_id = owner_id
        self.expires_at = time.monotonic() + 180
        self.completed = False
        for key, callback, style in (
            ("remove_all_roles_button", self.remove_all, discord.ButtonStyle.danger),
            ("remove_contact_button", self.remove_contact, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=bot.t(key), style=style)
            button.callback = callback
            self.add_item(button)

    async def remove_all(self, interaction):
        await self.remove(interaction, contact_only=False)

    async def remove_contact(self, interaction):
        await self.remove(interaction, contact_only=True)

    async def remove(self, interaction, *, contact_only):
        if interaction.guild_id != self.bot.config.guild_id:
            return await interaction.response.send_message(
                self.bot.t("wrong_server"), ephemeral=True
            )
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                self.bot.t("remove_role_owner_only"), ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.bot.lock:
                if self.completed or self.is_finished() or time.monotonic() >= self.expires_at:
                    raise ValueError("remove_role_expired")
                if contact_only:
                    cleaned = await self.bot.remove_contact(self.owner_id)
                    message = self.bot.t("clan_removed" if cleaned else "clan_tag_cleanup_error")
                else:
                    message = await remove_all(self.bot, self.owner_id)
                self.completed = True
                self.stop()
        except (*DISCORD_IO_ERRORS, ValueError) as error:
            return await interaction.followup.send(error_message(self.bot, error), ephemeral=True)
        await interaction.edit_original_response(content=message, view=None)
