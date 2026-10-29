import asyncio
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .audit import AuditJournal
from .contacts import register_contacts_command
from .i18n import t
from .openid import OpenID
from .security import States
from .store import Store
from .wargaming import CONTACT_RANKS, Wargaming

log = logging.getLogger(__name__)
DISCORD_IO_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError)


class LoginView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label=self.bot.t("login_button"),
            custom_id="wot:verify:v1",
            style=discord.ButtonStyle.primary,
        )
        button.callback = self.login
        self.add_item(button)
        contact = discord.ui.Button(
            label=self.bot.t("clan_button"),
            custom_id="wot:clan-contact:v1",
            style=discord.ButtonStyle.secondary,
        )
        contact.callback = self.clan_contact
        self.add_item(contact)

    async def clan_contact(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id != self.bot.config.guild_id:
            return await interaction.followup.send(self.bot.t("wrong_server"), ephemeral=True)
        try:
            async with self.bot.lock:
                await self.bot.claim_contact(interaction.user.id)
            message = self.bot.t("clan_success")
        except ValueError as exc:
            message = self.bot.t(str(exc))
        except DISCORD_IO_ERRORS:
            message = self.bot.t("clan_discord_error")
        await interaction.followup.send(message, ephemeral=True)

    async def login(self, interaction):
        if interaction.guild_id != self.bot.config.guild_id:
            return await interaction.response.send_message(
                self.bot.t("wrong_server"), ephemeral=True
            )
        if not self.bot.is_ready():
            return await interaction.response.send_message(self.bot.t("not_ready"), ephemeral=True)
        state = self.bot.states.issue(interaction.user.id, interaction.channel_id)
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=self.bot.t("login_button"),
                url=f"{self.bot.config.public_url}/login?state={state}",
            )
        )
        await interaction.response.send_message(
            self.bot.t("login_prompt"), view=view, ephemeral=True
        )


class VerifyBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.store = Store(config.database)
        self.audit = AuditJournal(self)
        self.states = States(config.state_secret, config.guild_id, self.store)
        self.lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.runner = None
        self.register_commands()
        register_contacts_command(self)

    def t(self, key):
        return t(key, getattr(self.config, "locale", "en"))

    async def setup_hook(self):
        from aiohttp import web

        from .web import application

        self.session = aiohttp.ClientSession()
        self.wg = Wargaming(self.session, self.config.wg_app_id, self.config.region)
        self.openid = OpenID(self.session, self.config.public_url, self.config.region, self.store)
        self.add_view(LoginView(self))
        self.runner = web.AppRunner(application(self), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.config.port).start()
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.monthly_sync.start()

    async def close(self):
        self.monthly_sync.cancel()
        task = self.monthly_sync.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.runner:
            await self.runner.cleanup()
        if hasattr(self, "session"):
            await self.session.close()
        self.store.close()
        await super().close()

    async def member(self, discord_id):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise ValueError("not_ready")
        return await guild.fetch_member(discord_id)

    def role(self, member):
        role = member.guild.get_role(self.config.role_id)
        if role is None or role.is_default() or role.managed or role.permissions.administrator:
            raise ValueError("discord_error")
        return role

    async def sync(self, row):
        member = await self.member(row["discord_id"])
        nickname = await self.wg.nickname(row["account_id"])
        suffix = ""
        is_contact = self.store.contact(row["discord_id"]) and any(
            role.id == getattr(self.config, "clan_role_id", 0) for role in member.roles
        )
        if is_contact:
            base_nickname = nickname
            nickname = await self.contact_nickname(row, nickname)
            suffix = nickname[len(base_nickname) :]
            self.store.remember_tag(member.id, suffix)
        await member.add_roles(self.role(member), reason="Wargaming account verification")
        try:
            if member.nick != nickname:
                await member.edit(nick=nickname, reason="Wargaming nickname synchronization")
        except discord.Forbidden:
            raise ValueError("nickname_blocked") from None
        except DISCORD_IO_ERRORS:
            raise ValueError("nickname_error") from None
        if is_contact:
            self.store.confirm_tag(member.id, suffix)
        else:
            self.store.clear_tags(member.id)
        self.store.mark_synced(member.id, nickname)
        self.store.clear_sync_failures(member.id)

    def contact_role(self, member):
        role_id = getattr(self.config, "clan_role_id", 0)
        if not role_id:
            raise ValueError("clan_disabled")
        role = member.guild.get_role(role_id)
        if (
            role is None
            or role.is_default()
            or role.managed
            or role.permissions.administrator
            or role.id == self.config.role_id
        ):
            raise ValueError("clan_discord_error")
        return role

    async def contact_nickname(self, row, nickname):
        membership = await self.wg.clan_membership(row["account_id"])
        contact = self.store.contact(row["discord_id"])
        if (
            membership is None
            or membership[1] not in CONTACT_RANKS
            or (
                contact
                and (contact["region"], contact["clan_id"]) != (row["region"], membership[0])
            )
        ):
            raise ValueError("clan_review")
        result = f"{nickname} [{membership[2]}]"
        if len(result) > 32:
            raise ValueError("clan_name_long")
        return result

    async def claim_contact(self, discord_id):
        # Caller holds the shared lock across lookup, reservation and Discord updates.
        if not self.is_ready():
            raise ValueError("not_ready")
        if not getattr(self.config, "clan_role_id", 0):
            raise ValueError("clan_disabled")
        row = self.store.get(discord_id)
        if row is None:
            raise ValueError("clan_not_linked")
        membership = await self.wg.clan_membership(row["account_id"])
        if membership is None or membership[1] not in CONTACT_RANKS:
            raise ValueError("clan_ineligible")
        current = self.store.contact(discord_id)
        if current and (current["region"], current["clan_id"]) != (row["region"], membership[0]):
            raise ValueError("clan_review")
        nickname = await self.wg.nickname(row["account_id"])
        nickname = f"{nickname} [{membership[2]}]"
        if len(nickname) > 32:
            raise ValueError("clan_name_long")
        member = await self.member(discord_id)
        if not any(role.id == self.config.role_id for role in member.roles):
            raise ValueError("clan_player_required")
        role = self.contact_role(member)
        self.store.reserve_contact(discord_id, row["region"], membership[0])
        try:
            await member.add_roles(role, reason="Verified clan contact")
        except discord.Forbidden:
            if current is None:
                self.store.release_contact(discord_id)
            raise
        # On timeout/unknown Discord result, keep the reservation to prevent duplicates.
        self.store.remember_tag(discord_id, f" [{membership[2]}]")
        if current is None:
            await self.audit.emit("contact_added", discord_id)
        try:
            if member.nick != nickname:
                await member.edit(nick=nickname, reason="Clan contact nickname")
        except DISCORD_IO_ERRORS:
            raise ValueError("clan_nickname_error") from None
        self.store.confirm_tag(discord_id, f" [{membership[2]}]")

    async def remove_contact(self, discord_id, member=None, *, was_contact=False):
        contact = self.store.contact(discord_id)
        if (
            not getattr(self.config, "clan_role_id", 0)
            and not contact
            and not self.store.contact_tags(discord_id)
        ):
            return True
        if member is None:
            try:
                member = await self.member(discord_id)
            except discord.NotFound:
                member = None
        if member:
            role_id = getattr(self.config, "clan_role_id", 0)
            has_role = any(role.id == role_id for role in member.roles)
            # Support contacts created before tag tracking, but never infer a tag
            # from the nickname of someone who is not a contact.
            if (
                (has_role or (was_contact and contact is not None))
                and not self.store.contact_tags(discord_id)
                and member.nick
            ):
                suffix = re.search(r" \[[A-Za-z0-9_-]{2,5}\]$", member.nick)
                if suffix:
                    self.store.remember_tag(discord_id, suffix[0])
            if has_role:
                await member.remove_roles(self.contact_role(member), reason="Clan contact removed")
        self.store.release_contact(discord_id)
        if contact or (member and has_role):
            await self.audit.emit("contact_removed", discord_id)
        if member and member.nick:
            for suffix in self.store.contact_tags(discord_id):
                if member.nick.endswith(suffix):
                    try:
                        await member.edit(
                            nick=member.nick[: -len(suffix)] or None,
                            reason="Clan contact tag removed",
                        )
                    except DISCORD_IO_ERRORS:
                        log.warning("Clan tag cleanup pending for Discord member %s", discord_id)
                        return False
                    break
        self.store.clear_tags(discord_id)
        return True

    async def on_member_update(self, before, after):
        if after.guild.id != self.config.guild_id:
            return
        role_id = getattr(self.config, "clan_role_id", 0)
        if not role_id:
            return
        old = {role.id for role in before.roles}
        new = {role.id for role in after.roles}
        player_removed = self.config.role_id in old - new
        contact_removed = role_id in old - new
        if not player_removed and not contact_removed:
            return
        async with self.lock:
            try:
                # Gateway events may be queued: never act on a stale member snapshot.
                member = await self.member(after.id)
                roles = {role.id for role in member.roles}
                if (
                    player_removed
                    and self.config.role_id not in roles
                    and (role_id in old or self.store.contact(after.id))
                ) or (contact_removed and role_id not in roles):
                    await self.remove_contact(after.id, member, was_contact=role_id in old)
            except discord.NotFound:
                self.store.release_contact(after.id)
            except (*DISCORD_IO_ERRORS, ValueError):
                log.warning("Clan contact cleanup failed for Discord member %s", after.id)

    async def remove(self, discord_id):
        # Keep the link on permission/network failures so an administrator can retry.
        linked = self.store.get(discord_id) is not None
        try:
            member = await self.member(discord_id)
        except discord.NotFound:
            member = None
        cleaned = True
        if member:
            cleaned = await self.remove_contact(discord_id, member)
            await member.remove_roles(self.role(member), reason="Wargaming account unlinked")
        else:
            self.store.clear_tags(discord_id)
        self.store.unlink(discord_id)
        if linked:
            await self.audit.emit("unlinked", discord_id)
        return cleaned

    async def on_member_join(self, member):
        if member.guild.id != self.config.guild_id:
            return
        async with self.lock:
            if not self.store.get(member.id):
                return
            try:
                await member.add_roles(self.role(member), reason="Restore verified player role")
            except (discord.HTTPException, ValueError):
                log.warning("Could not restore role for Discord member %s", member.id)
                return
            try:
                await member.send(self.t("rejoined"))
            except discord.HTTPException:
                pass

    @tasks.loop(hours=24)
    async def monthly_sync(self):
        for row in self.store.due():
            async with self.lock:
                current = self.store.get(row["discord_id"])
                if current is None:
                    continue
                try:
                    await self.sync(current)
                except (*DISCORD_IO_ERRORS, ValueError):
                    log.warning("Periodic sync failed for Discord member %s", row["discord_id"])
                    if self.store.sync_failure_notice(row["discord_id"], int(time.time())):
                        await self.audit.emit("sync_failed", row["discord_id"])
            await asyncio.sleep(1)

    @monthly_sync.before_loop
    async def before_monthly(self):
        await self.wait_until_ready()

    def register_commands(self):
        async def check(interaction):
            if interaction.guild_id != self.config.guild_id:
                raise app_commands.CheckFailure(self.t("wrong_server"))
            return True

        self.tree.interaction_check = check

        @self.tree.command(description=self.t("command_admin_contact_remove"))
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_roles=True)
        @app_commands.checks.has_permissions(manage_roles=True)
        async def admin_contact_remove(interaction: discord.Interaction, discord_id: str):
            await interaction.response.defer(ephemeral=True)
            if (
                not discord_id.isascii()
                or not discord_id.isdigit()
                or not 0 < int(discord_id) < 2**63
            ):
                raise ValueError("bad_target")
            async with self.lock:
                cleaned = await self.remove_contact(int(discord_id))
            await interaction.followup.send(
                self.t("clan_removed" if cleaned else "clan_tag_cleanup_error"), ephemeral=True
            )

        @self.tree.command(description=self.t("command_setup"))
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def setup(interaction: discord.Interaction):
            from .panel import update_panel

            await interaction.response.defer(ephemeral=True)
            async with self.lock:
                message = await update_panel(self, interaction.channel, LoginView(self))
            await interaction.followup.send(message, ephemeral=True)

        @self.tree.command(description=self.t("command_resync"))
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
        async def resync(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            async with self.lock:
                row = self.store.get(interaction.user.id)
                if row is None:
                    raise ValueError("not_linked")
                await self.sync(row)
            await interaction.followup.send(self.t("synced"), ephemeral=True)

        @self.tree.command(description=self.t("command_unlink"))
        @app_commands.guild_only()
        async def unlink(interaction: discord.Interaction, confirm: bool = False):
            await interaction.response.defer(ephemeral=True)
            if not confirm:
                return await interaction.followup.send(self.t("confirm_unlink"), ephemeral=True)
            async with self.lock:
                cleaned = await self.remove(interaction.user.id)
            await interaction.followup.send(
                self.t("unlinked" if cleaned else "unlinked_tag_pending"), ephemeral=True
            )

        @self.tree.command(description=self.t("command_admin_unlink"))
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def admin_unlink(
            interaction: discord.Interaction, discord_id: str = "", account_id: str = ""
        ):
            await interaction.response.defer(ephemeral=True)
            if bool(discord_id) == bool(account_id):
                raise ValueError("bad_target")
            raw = discord_id or account_id
            if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**63:
                raise ValueError("bad_target")
            async with self.lock:
                row = (
                    self.store.get(int(discord_id))
                    if discord_id
                    else self.store.by_account(self.config.region, int(account_id))
                )
                if row is None:
                    raise ValueError("not_linked")
                cleaned = await self.remove(row["discord_id"])
            await interaction.followup.send(
                self.t("unlinked" if cleaned else "unlinked_tag_pending"), ephemeral=True
            )

        @self.tree.error
        async def error(interaction, exc):
            original = getattr(exc, "original", exc)
            if isinstance(original, ValueError):
                message = self.t(str(original))
            elif isinstance(original, discord.HTTPException):
                message = self.t("discord_error")
            elif isinstance(original, app_commands.CommandOnCooldown):
                message = self.t("command_cooldown")
            elif isinstance(original, app_commands.CheckFailure):
                message = self.t("admin_only")
            else:
                message = self.t("error")
            log.warning("Command failed: %s", type(original).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
