from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.reports import diagnose_report, status_report


@dataclass(order=True)
class Role:
    position: int
    id: int
    managed: bool = field(default=False, compare=False)
    permissions: object = field(default_factory=discord.Permissions.none, compare=False)

    def is_default(self):
        return self.id == 1


@pytest.fixture(params=["en", "fr"])
def env(tmp_path, request):
    bot = VerifyBot(
        Config(
            discord_token="PRIVATE_TOKEN",
            wg_app_id="PRIVATE_APP",
            state_secret="x" * 32,
            guild_id=1,
            role_id=2,
            clan_role_id=3,
            public_url="https://example.org",
            database=str(tmp_path / "reports.sqlite3"),
            locale=request.param,
        )
    )
    roles = {2: Role(2, 2), 3: Role(3, 3)}
    permissions = discord.Permissions(manage_roles=True, manage_nicknames=True)
    me = SimpleNamespace(id=99, top_role=Role(10, 99), guild_permissions=permissions)
    guild = SimpleNamespace(id=1, owner_id=98, me=me, get_role=roles.get)
    member = SimpleNamespace(
        id=7,
        guild=guild,
        nick="Player",
        roles=[roles[2]],
        top_role=Role(2, 2),
        edit=AsyncMock(),
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )
    bot.member = AsyncMock(return_value=member)
    bot.wg = SimpleNamespace(
        nickname=AsyncMock(return_value="Player"),
        clan_membership=AsyncMock(return_value=(42, "commander", "TAG")),
    )
    channel = SimpleNamespace(
        permissions_for=lambda _: discord.Permissions(view_channel=True, send_messages=True)
    )
    interaction = SimpleNamespace(
        guild_id=1,
        guild=guild,
        channel=channel,
        user=member,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        permissions=discord.Permissions.none(),
    )
    yield bot, member, roles, interaction
    bot.store.close()


@pytest.mark.parametrize("state", ["ok", "missing", "hierarchy", "admin"])
def test_diagnose_visitor_role(env, state):
    bot, _, roles, interaction = env
    bot.config = replace(bot.config, visitor_role_id=4)
    if state != "missing":
        roles[4] = Role(20 if state == "hierarchy" else 4, 4)
        if state == "admin":
            roles[4].permissions = discord.Permissions(administrator=True)
    report = diagnose_report(bot, interaction)
    visitor = next(f for f in report.fields if f.name == bot.t("status_visitor_role"))
    expected = {
        "ok": "diagnose_ok",
        "missing": "diagnose_role_missing",
        "hierarchy": "diagnose_role_hierarchy",
        "admin": "diagnose_role_invalid",
    }[state]
    assert visitor.value == f"4 · {bot.t(expected)}"


def values(report):
    return "\n".join(str(f.value) for f in report.fields)


async def test_status_unlinked_does_not_fetch_any_account(env):
    bot, _, _, _ = env
    bot.store.link(8, "eu", 200, "OtherMember")
    report = await status_report(bot, 7)
    assert report.description == bot.t("status_no_link")
    assert "OtherMember" not in str(report.to_dict())
    bot.member.assert_not_awaited()
    bot.wg.nickname.assert_not_awaited()


async def test_status_command_private_and_read_only(env):
    bot, member, _, interaction = env
    bot.store.link(7, "eu", 100, "Player")
    bot.store.mark_synced(7, "Player")
    before = list(bot.store.db.iterdump())
    await bot.tree.get_command("status").callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True
    report = kwargs["embed"]
    assert report.description == bot.t("status_ok")
    assert "100 (EU)" in values(report)
    assert "[TAG]" in values(report)
    assert "<t:" in values(report)
    assert list(bot.store.db.iterdump()) == before
    member.edit.assert_not_awaited()
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    assert len(str(report.to_dict())) < 6000
    assert "PRIVATE_" not in str(report.to_dict())


async def test_status_missing_role_and_blocked_nickname(env):
    bot, member, _, _ = env
    bot.store.link(7, "eu", 100, "Player")
    member.roles = []
    member.nick = "Old"
    member.top_role = member.guild.me.top_role
    report = await status_report(bot, 7)
    assert bot.t("status_never") in values(report)
    assert bot.t("status_resync_hint") in report.description
    assert bot.t("status_nickname_blocked") in report.description


@pytest.mark.parametrize("kind", ["none", "failure", "wrong_region"])
async def test_status_no_clan_is_distinct_from_unavailable(env, kind):
    bot, _, _, _ = env
    bot.store.link(7, "com" if kind == "wrong_region" else "eu", 100, "Player")
    if kind == "none":
        bot.wg.clan_membership.return_value = None
    else:
        bot.wg.clan_membership.side_effect = ValueError("wg_error")
    report = await status_report(bot, 7)
    clan = next(f.value for f in report.fields if f.name == bot.t("status_clan"))
    assert clan == bot.t("status_no_clan" if kind == "none" else "status_unknown")
    if kind != "none":
        assert bot.t("status_wg_unavailable") in report.description
    if kind == "wrong_region":
        bot.wg.nickname.assert_not_awaited()
        bot.wg.clan_membership.assert_not_awaited()


@pytest.mark.parametrize(
    "state", ["active", "missing_role", "orphan_role", "changed_clan", "lost_rank", "wg_failure"]
)
async def test_status_clan_contact_states(env, state):
    bot, member, roles, _ = env
    bot.store.link(7, "eu", 100, "Player")
    if state != "orphan_role":
        bot.store.reserve_contact(7, "eu", 42)
    if state != "missing_role":
        member.roles.append(roles[3])
    if state == "changed_clan":
        bot.wg.clan_membership.return_value = (43, "commander", "NEW")
    if state == "lost_rank":
        bot.wg.clan_membership.return_value = (42, "private", "TAG")
    if state == "wg_failure":
        bot.wg.clan_membership.side_effect = ValueError("wg_error")
    before = list(bot.store.db.iterdump())
    report = await status_report(bot, 7)
    expected = next(f.value for f in report.fields if f.name == bot.t("status_expected_nickname"))
    if state == "active":
        assert expected == "Player [TAG]"
    elif state in {"changed_clan", "lost_rank"}:
        assert bot.t("clan_review") in report.description
        assert expected == bot.t("status_unknown")
    elif state == "wg_failure":
        assert expected == bot.t("status_unknown")
    else:
        assert bot.t("status_contact_review") in report.description
    assert list(bot.store.db.iterdump()) == before


async def test_status_failed_nickname_lookup_does_not_show_saved_name_as_current(env):
    bot, _, _, _ = env
    bot.store.link(7, "eu", 100, "OldSavedName")
    bot.wg.nickname.side_effect = ValueError("wg_error")
    report = await status_report(bot, 7)
    assert bot.t("status_unknown") in values(report)
    assert bot.t("status_wg_unavailable") in report.description


async def test_diagnose_is_private_and_requires_admin_at_runtime(env):
    bot, _, _, interaction = env
    command = bot.tree.get_command("diagnose")
    assert command.default_permissions.administrator
    with pytest.raises(app_commands.MissingPermissions):
        await command.checks[0](interaction)
    interaction.permissions = discord.Permissions(administrator=True)
    assert command.checks[0](interaction)
    await command.callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert bot.t("diagnose_role_missing") not in values(kwargs["embed"])
    assert "PRIVATE_" not in str(kwargs["embed"].to_dict())
    bot.member.assert_not_awaited()
    bot.wg.nickname.assert_not_awaited()


@pytest.mark.parametrize("kind", ["missing", "managed", "default", "admin", "hierarchy"])
def test_diagnose_invalid_player_roles(env, kind):
    bot, _, roles, interaction = env
    if kind == "missing":
        roles.pop(2)
        key = "diagnose_role_missing"
    elif kind == "managed":
        roles[2].managed = True
        key = "diagnose_role_invalid"
    elif kind == "default":
        roles[2].id = 1
        key = "diagnose_role_invalid"
    elif kind == "admin":
        roles[2].permissions.administrator = True
        key = "diagnose_role_invalid"
    else:
        roles[2] = interaction.guild.me.top_role
        key = "diagnose_role_hierarchy"
    assert bot.t(key) in values(diagnose_report(bot, interaction))


def test_diagnose_missing_permissions_and_disabled_contact(env):
    bot, _, _, interaction = env
    bot.config = SimpleNamespace(role_id=2, clan_role_id=0, locale=bot.config.locale)
    interaction.guild.me.guild_permissions = discord.Permissions.none()
    interaction.channel.permissions_for = lambda _: discord.Permissions.none()
    report = diagnose_report(bot, interaction)
    for key in (
        "status_disabled",
        "diagnose_grant_roles",
        "diagnose_grant_nicknames",
        "diagnose_grant_view",
        "diagnose_grant_send",
    ):
        assert bot.t(key) in values(report)


async def test_reports_restricted_to_configured_guild(env):
    bot, _, _, interaction = env
    interaction.guild_id = 999
    with pytest.raises(app_commands.CheckFailure):
        await bot.tree.interaction_check(interaction)


def test_diagnose_thread_uses_thread_send_permission(env):
    bot, _, _, interaction = env
    interaction.channel = Mock(spec=discord.Thread)
    interaction.channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True, send_messages_in_threads=False
    )
    assert bot.t("diagnose_grant_send") in values(diagnose_report(bot, interaction))
    interaction.channel.permissions_for.return_value.send_messages_in_threads = True
    assert bot.t("diagnose_grant_send") not in values(diagnose_report(bot, interaction))


def test_diagnose_missing_guild_and_channel(env):
    bot, _, _, interaction = env
    interaction.channel = None
    assert bot.t("diagnose_channel_unknown") in values(diagnose_report(bot, interaction))
    interaction.guild = None
    with pytest.raises(ValueError, match="not_ready"):
        diagnose_report(bot, interaction)


async def test_status_pending_cleanup_and_server_owner(env):
    bot, member, _, _ = env
    bot.store.link(7, "eu", 100, "Player")
    bot.store.remember_tag(7, " [OLD]")
    member.nick = "Player [OLD]"
    member.guild.owner_id = member.id
    report = await status_report(bot, 7)
    assert bot.t("status_tag_pending") in report.description
    assert bot.t("status_nickname_blocked") in report.description
    assert bot.store.contact_tags(7) == [" [OLD]"]


async def test_cooldown_explains_wait_instead_of_admin_permissions(env):
    bot, _, _, interaction = env
    interaction.response.is_done = lambda: True
    error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 20)
    await bot.tree.on_error(interaction, error)
    interaction.followup.send.assert_awaited_once_with(bot.t("command_cooldown"), ephemeral=True)


@pytest.mark.parametrize("can_read", [True, False])
def test_diagnose_checks_read_history_for_saved_panel(env, can_read):
    bot, _, _, interaction = env
    interaction.channel.permissions_for = lambda _: discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=can_read
    )
    report = diagnose_report(bot, interaction)
    field = next(f for f in report.fields if f.name == bot.t("diagnose_read_history"))
    assert field.value == bot.t("diagnose_ok" if can_read else "diagnose_grant_history")
