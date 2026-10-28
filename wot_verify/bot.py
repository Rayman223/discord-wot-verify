import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .i18n import t
from .security import States
from .store import Store
from .wargaming import Wargaming

log = logging.getLogger(__name__)


class LoginView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label=t("login_button"), custom_id="wot:verify:v1", style=discord.ButtonStyle.primary
        )
        button.callback = self.login
        self.add_item(button)

    async def login(self, interaction):
        if interaction.guild_id != self.bot.config.guild_id:
            return await interaction.response.send_message(t("wrong_server"), ephemeral=True)
        if not self.bot.is_ready():
            return await interaction.response.send_message(t("not_ready"), ephemeral=True)
        state = self.bot.states.issue(interaction.user.id, interaction.channel_id)
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=t("login_button"), url=f"{self.bot.config.public_url}/login?state={state}"
            )
        )
        await interaction.response.send_message(t("login_prompt"), view=view, ephemeral=True)


class VerifyBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.store = Store(config.database)
        self.states = States(config.state_secret, config.guild_id, self.store)
        self.lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.runner = None
        self.register_commands()

    async def setup_hook(self):
        from aiohttp import web

        from .web import application

        self.session = aiohttp.ClientSession()
        self.wg = Wargaming(self.session, self.config.wg_app_id, self.config.region)
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
        await member.add_roles(self.role(member), reason="Wargaming account verification")
        try:
            if member.nick != nickname:
                await member.edit(nick=nickname, reason="Wargaming nickname synchronization")
        except discord.Forbidden:
            raise ValueError("nickname_blocked") from None
        except discord.HTTPException:
            raise ValueError("nickname_error") from None
        self.store.mark_synced(member.id, nickname)

    async def remove(self, discord_id):
        # Keep the link on permission/network failures so an administrator can retry.
        try:
            member = await self.member(discord_id)
        except discord.NotFound:
            member = None
        if member:
            await member.remove_roles(self.role(member), reason="Wargaming account unlinked")
        self.store.unlink(discord_id)

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
                await member.send(t("rejoined"))
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
                except (discord.HTTPException, ValueError):
                    log.warning("Periodic sync failed for Discord member %s", row["discord_id"])
            await asyncio.sleep(1)

    @monthly_sync.before_loop
    async def before_monthly(self):
        await self.wait_until_ready()

    def register_commands(self):
        async def check(interaction):
            if interaction.guild_id != self.config.guild_id:
                raise app_commands.CheckFailure(t("wrong_server"))
            return True

        self.tree.interaction_check = check

        @self.tree.command(description="Post the Wargaming verification panel in this channel")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def setup(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await interaction.channel.send(t("panel"), view=LoginView(self))
            await interaction.followup.send(t("setup_done"), ephemeral=True)

        @self.tree.command(description="Refresh your player role and World of Tanks nickname")
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
        async def resync(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            async with self.lock:
                row = self.store.get(interaction.user.id)
                if row is None:
                    raise ValueError("not_linked")
                await self.sync(row)
            await interaction.followup.send(t("synced"), ephemeral=True)

        @self.tree.command(description="Remove your account link and player role")
        @app_commands.guild_only()
        async def unlink(interaction: discord.Interaction, confirm: bool = False):
            await interaction.response.defer(ephemeral=True)
            if not confirm:
                return await interaction.followup.send(t("confirm_unlink"), ephemeral=True)
            async with self.lock:
                await self.remove(interaction.user.id)
            await interaction.followup.send(t("unlinked"), ephemeral=True)

        @self.tree.command(description="Remove a link by Discord user ID or Wargaming account ID")
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
                await self.remove(row["discord_id"])
            await interaction.followup.send(t("unlinked"), ephemeral=True)

        @self.tree.error
        async def error(interaction, exc):
            original = getattr(exc, "original", exc)
            if isinstance(original, ValueError):
                message = t(str(original))
            elif isinstance(original, discord.HTTPException):
                message = t("discord_error")
            elif isinstance(original, app_commands.CheckFailure):
                message = t("admin_only")
            else:
                message = t("error")
            log.warning("Command failed: %s", type(original).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
