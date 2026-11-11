from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.profile import account_link, calendar_age
from wot_verify.wargaming import ClanProfile, PlayerProfile, ProfileMembership


@pytest.fixture(params=["fr", "en"])
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
            database=str(tmp_path / "profile.sqlite3"),
            locale=request.param,
        )
    )

    def member(discord_id, *roles):
        return SimpleNamespace(
            id=discord_id,
            roles=[SimpleNamespace(id=r) for r in roles],
            edit=AsyncMock(),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )

    members = {7: member(7, 2), 8: member(8, 2), 9: member(9, 2, 3), 10: member(10)}
    bot.member = AsyncMock(side_effect=lambda discord_id: members[discord_id])
    bot.store.link(7, "eu", 100, "OldName")
    bot.store.link(8, "eu", 200, "Other")
    bot.store.link(9, "eu", 300, "SavedContact")
    bot.wg = SimpleNamespace(
        player_profile=AsyncMock(
            return_value=PlayerProfile("CurrentName", 1308919860, 1793797200, 1794128880)
        ),
        profile_membership=AsyncMock(
            return_value=ProfileMembership(42, "TAG", "private", "Soldat")
        ),
        clan_profile=AsyncMock(
            return_value=ClanProfile(
                "Clan name", 99, 1308919860, 400, "Commander", "https://example.org/logo.png"
            )
        ),
        nickname=AsyncMock(return_value="ContactNow"),
        clan_membership=AsyncMock(return_value=(42, "personnel_officer", "TAG")),
    )
    interaction = SimpleNamespace(
        guild_id=1,
        user=members[7],
        channel=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
        created_at=datetime.now(UTC),
    )
    yield bot, members, interaction
    bot.store.close()


def values(bot, report):
    return {field.name: field.value for field in report.fields}


async def execute(bot, interaction, target=None):
    try:
        await bot.tree.get_command("profile").callback(interaction, target)
    except (ValueError, discord.HTTPException) as exc:
        await bot.tree.on_error(interaction, exc)


async def test_complete_public_profile_is_read_only(env):
    bot, members, interaction = env
    bot.store.reserve_contact(9, "eu", 42)
    before = list(bot.store.db.iterdump())
    await execute(bot, interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = interaction.channel.send.call_args.kwargs
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "delete_after" not in kwargs
    report = kwargs["embed"]
    fields = values(bot, report)
    assert report.title == "CurrentName [TAG]"
    assert report.thumbnail.url == "https://example.org/logo.png"
    assert fields[bot.t("profile_account_id")] == "100"
    assert fields[bot.t("profile_created_at")] == "<t:1308919860:F>"
    assert fields[bot.t("profile_last_battle")] == "<t:1793797200:F>"
    assert fields[bot.t("profile_logout")] == "<t:1794128880:F>"
    assert fields[bot.t("profile_age")] != bot.t("profile_unavailable")
    assert fields[bot.t("profile_rank")] == "Soldat"
    assert "https://eu.wargaming.net/clans/wot/42" in fields[bot.t("profile_clan")]
    assert fields[bot.t("profile_members_count")] == "99"
    for key, name, account in (
        ("profile_commander", "Commander", 400),
        ("profile_contact", "ContactNow", 300),
    ):
        assert fields[bot.t(key)] == account_link("eu", bot.config.locale, account, name)
    assert len(report) < 6000
    assert all(len(f.value) <= 1024 for f in report.fields)
    assert "PRIVATE_" not in str(report.to_dict())
    assert list(bot.store.db.iterdump()) == before
    for member in members.values():
        member.edit.assert_not_awaited()
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()


async def test_visitor_can_select_another_player(env):
    bot, members, interaction = env
    interaction.user = members[10]
    await execute(bot, interaction, members[8])
    bot.wg.player_profile.assert_awaited_once_with(200)
    fields = values(bot, interaction.channel.send.call_args.kwargs["embed"])
    assert fields[bot.t("profile_account_id")] == "200"
    assert fields[bot.t("profile_contact")] == bot.t("profile_none")


@pytest.mark.parametrize("nickname", ["Masheik_SortLarrowZouave", "_Commander_", "Commander__WoT"])
async def test_commander_and_contact_links_preserve_nickname_underscores(env, nickname):
    bot, _, interaction = env
    bot.store.reserve_contact(9, "eu", 42)
    bot.wg.clan_profile.return_value = ClanProfile("Clan", 1, None, 400, nickname, None)
    bot.wg.nickname.return_value = nickname
    await execute(bot, interaction)
    fields = values(bot, interaction.channel.send.call_args.kwargs["embed"])
    for key, account_id in (("profile_commander", 400), ("profile_contact", 300)):
        assert fields[bot.t(key)] == (
            f"[{nickname}](https://worldoftanks.eu/{bot.config.locale}"
            f"/community/accounts/{account_id}-{nickname}/)"
        )


@pytest.mark.parametrize(
    "state,key",
    [
        ("role", "profile_player_required"),
        ("link", "profile_not_linked"),
        ("region", "profile_wrong_region"),
        ("guild", "wrong_server"),
        ("dm", "wrong_server"),
        ("channel", "discord_error"),
    ],
)
async def test_invalid_targets_fail_privately_without_wg_calls(env, state, key):
    bot, members, interaction = env
    if state == "role":
        members[7].roles = []
    elif state == "link":
        bot.store.unlink(7)
    elif state == "region":
        bot.config = replace(bot.config, region="asia")
    elif state in {"guild", "dm"}:
        interaction.guild_id = 999 if state == "guild" else None
    else:
        interaction.channel = None
    channel = interaction.channel
    await execute(bot, interaction)
    if channel:
        channel.send.assert_not_awaited()
    bot.wg.player_profile.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(bot.t(key), ephemeral=True)


@pytest.mark.parametrize("part", ["player", "membership", "clan"])
async def test_api_failure_is_private_or_explicitly_partial(env, part):
    bot, _, interaction = env
    getattr(
        bot.wg,
        {"player": "player_profile", "membership": "profile_membership", "clan": "clan_profile"}[
            part
        ],
    ).side_effect = ValueError("wg_error")
    await execute(bot, interaction)
    if part == "player":
        interaction.channel.send.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(bot.t("wg_error"), ephemeral=True)
    else:
        report = interaction.channel.send.call_args.kwargs["embed"]
        fields = values(bot, report)
        assert fields[bot.t("profile_clan")] == bot.t("profile_clan_unavailable")
        assert bot.t("status_no_clan") not in fields.values()
        assert report.title == ("CurrentName" if part == "membership" else "CurrentName [TAG]")
        assert not report.thumbnail.url


async def test_without_clan_hides_clan_details(env):
    bot, _, interaction = env
    bot.wg.profile_membership.return_value = None
    await execute(bot, interaction)
    report = interaction.channel.send.call_args.kwargs["embed"]
    fields = values(bot, report)
    assert report.title == "CurrentName"
    assert fields[bot.t("profile_clan")] == bot.t("status_no_clan")
    assert bot.t("profile_rank") not in fields
    assert bot.t("profile_contact") not in fields
    bot.wg.clan_profile.assert_not_awaited()


async def test_missing_optional_information_is_unavailable(env):
    bot, _, interaction = env
    bot.wg.player_profile.return_value = PlayerProfile("CurrentName", None, None, None)
    bot.wg.profile_membership.return_value = ProfileMembership(42, "TAG", "new_role", None)
    bot.wg.clan_profile.return_value = ClanProfile("Clan name", None, None, None, None, None)
    await execute(bot, interaction)
    report = interaction.channel.send.call_args.kwargs["embed"]
    fields = values(bot, report)
    for key in (
        "created_at",
        "age",
        "last_battle",
        "logout",
        "rank",
        "members_count",
        "clan_created",
        "commander",
    ):
        assert fields[bot.t("profile_" + key)] == bot.t("profile_unavailable")
    assert not report.thumbnail.url


@pytest.mark.parametrize(
    "state,key",
    [
        ("left", "profile_contact_review"),
        ("roles", "profile_contact_review"),
        ("clan", "profile_contact_review"),
        ("rank", "profile_contact_review"),
        ("wg", "profile_contact_unknown"),
        ("discord", "profile_contact_unknown"),
        ("nickname", "profile_contact_saved_name"),
        ("unlinked", "profile_contact_unusable"),
        ("region", "profile_contact_unusable"),
    ],
)
async def test_stale_contact_is_preserved_and_warned(env, state, key):
    bot, members, interaction = env
    bot.store.reserve_contact(9, "eu", 42)
    if state == "roles":
        members[9].roles = []
    elif state in {"left", "discord"}:

        def fetch(discord_id):
            if discord_id == 9:
                cls = discord.NotFound if state == "left" else discord.Forbidden
                raise cls(Mock(status=404 if state == "left" else 403), "failure")
            return members[discord_id]

        bot.member.side_effect = fetch
    elif state == "clan":
        bot.wg.clan_membership.return_value = (43, "commander", "NEW")
    elif state == "rank":
        bot.wg.clan_membership.return_value = (42, "private", "TAG")
    elif state == "wg":
        bot.wg.clan_membership.side_effect = ValueError("wg_error")
    elif state == "nickname":
        bot.wg.nickname.side_effect = ValueError("wg_error")
    elif state == "unlinked":
        bot.store.db.execute("DELETE FROM links WHERE discord_id=9")
    else:
        bot.store.db.execute("UPDATE links SET region='asia' WHERE discord_id=9")
    before = list(bot.store.db.iterdump())
    await execute(bot, interaction)
    fields = values(bot, interaction.channel.send.call_args.kwargs["embed"])
    contact = fields[bot.t("profile_contact")]
    assert bot.t(key) in contact
    if state not in {"unlinked", "region"}:
        assert "/community/accounts/300-" in contact
    assert contact != bot.t("profile_none")
    assert list(bot.store.db.iterdump()) == before


@pytest.mark.parametrize("saved", ["SavedContact [TAG]", "SavedContact [TAG] 🕯️🕯"])
async def test_saved_contact_link_uses_wot_name_without_discord_decorations(env, saved):
    bot, _, interaction = env
    bot.store.reserve_contact(9, "eu", 42)
    bot.store.mark_synced(9, saved)
    bot.wg.nickname.side_effect = ValueError("wg_error")
    await execute(bot, interaction)
    fields = values(bot, interaction.channel.send.call_args.kwargs["embed"])
    contact = fields[bot.t("profile_contact")]
    assert account_link("eu", bot.config.locale, 300, "SavedContact") in contact
    assert bot.t("profile_contact_saved_name") in contact
    assert "%20" not in contact


async def test_unusable_saved_contact_name_is_not_a_broken_or_oversized_link(env):
    bot, _, interaction = env
    bot.store.reserve_contact(9, "eu", 42)
    bot.store.mark_synced(9, "x" * 2000)
    bot.wg.nickname.side_effect = ValueError("wg_error")
    await execute(bot, interaction)
    fields = values(bot, interaction.channel.send.call_args.kwargs["embed"])
    assert fields[bot.t("profile_contact")] == bot.t("profile_contact_unusable")


@pytest.mark.parametrize("change", ["unlink", "identity", "role"])
async def test_change_during_lookup_prevents_publication(env, change):
    bot, members, interaction = env
    player = bot.wg.player_profile.return_value

    async def lookup(_):
        if change == "role":
            members[7].roles = []
        else:
            bot.store.unlink(7)
            if change == "identity":
                bot.store.link(7, "eu", 101, "Replacement")
        return player

    bot.wg.player_profile.side_effect = lookup
    await execute(bot, interaction)
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("status,code", [(403, 50013), (400, 50035)])
async def test_publication_failure_is_private_and_logged_without_response_body(
    env, caplog, status, code
):
    bot, _, interaction = env
    cls = discord.Forbidden if status == 403 else discord.HTTPException
    interaction.channel.send.side_effect = cls(
        Mock(status=status), {"code": code, "message": "PRIVATE_TOKEN PRIVATE_APP"}
    )
    await execute(bot, interaction)
    key = "profile_publish_forbidden" if status == 403 else "discord_error"
    interaction.followup.send.assert_awaited_once_with(bot.t(key), ephemeral=True)
    interaction.channel.send.assert_awaited_once()
    interaction.delete_original_response.assert_not_awaited()
    assert f"Profile publication failed: status={status} code={code}" in caplog.text
    assert "PRIVATE_" not in caplog.text


async def test_member_lookup_failure_does_not_report_publication_permissions(env):
    bot, _, interaction = env
    bot.member.side_effect = discord.Forbidden(Mock(status=403), "failure")
    await execute(bot, interaction)
    interaction.channel.send.assert_not_awaited()
    bot.wg.player_profile.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(bot.t("discord_error"), ephemeral=True)


async def test_acknowledgement_cleanup_failure_does_not_report_publication_failure(env):
    bot, _, interaction = env
    interaction.delete_original_response.side_effect = discord.NotFound(Mock(status=404), "gone")
    await execute(bot, interaction)
    interaction.channel.send.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()


async def test_cooldown_applies_per_caller(env):
    bot, members, interaction = env
    command = bot.tree.get_command("profile")
    assert command.guild_only
    assert command.parameters[0].name == "member"
    assert not command.parameters[0].required
    check = command.checks[0]
    assert await check(interaction)
    with pytest.raises(app_commands.CommandOnCooldown):
        await check(interaction)
    interaction.user = members[10]
    assert await check(interaction)


async def test_external_markdown_and_mentions_are_escaped(env):
    bot, _, interaction = env
    bot.wg.player_profile.return_value = PlayerProfile("@everyone_*", None, None, None)
    bot.wg.clan_profile.return_value = ClanProfile(
        "[Clan](https://evil.test) @everyone", 1, None, 400, "Commander", None
    )
    await execute(bot, interaction)
    report = interaction.channel.send.call_args.kwargs["embed"]
    assert "@everyone" not in str(report.to_dict())
    assert "[Clan](https://evil.test)" not in str(report.to_dict())
    assert "%2F" in account_link("eu", "fr", 400, "bad/name")
    unsafe = account_link("eu", "fr", 400, "](@everyone)")
    assert unsafe.startswith(r"[\]\(@")
    assert "@everyone" not in unsafe


@pytest.mark.parametrize(
    "created,today,expected",
    [
        (date(2011, 6, 24), date(2026, 11, 11), (15, 4, 18)),
        (date(2024, 1, 31), date(2024, 2, 28), (0, 0, 28)),
        (date(2023, 1, 31), date(2023, 2, 28), (0, 1, 0)),
        (date(2024, 2, 29), date(2025, 2, 28), (1, 0, 0)),
        (date(2024, 2, 29), date(2025, 2, 27), (0, 11, 29)),
        (date(2024, 2, 29), date(2028, 2, 29), (4, 0, 0)),
        (date(2025, 11, 12), date(2026, 11, 11), (0, 11, 30)),
        (date(2026, 11, 11), date(2026, 11, 11), (0, 0, 0)),
    ],
)
def test_calendar_age(created, today, expected):
    assert calendar_age(created, today) == expected
