import asyncio
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from wot_verify.admin import Inspection, report_file
from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.i18n import CATALOGS
from wot_verify.statistics import local_statistics


@pytest.fixture(params=["en", "fr"])
def env(tmp_path, monkeypatch, request):
    bot = VerifyBot(
        Config(
            discord_token="PRIVATE_TOKEN",
            wg_app_id="PRIVATE_APP",
            state_secret="x" * 32,
            guild_id=1,
            role_id=2,
            clan_role_id=3,
            visitor_role_id=4,
            public_url="https://example.org",
            database=str(tmp_path / "admin.sqlite3"),
            locale=request.param,
        )
    )
    members = {}
    accounts = {}
    memberships = {}

    async def fetch_members(limit=None):
        for member in list(members.values()):
            yield member

    roles = {
        identifier: SimpleNamespace(
            id=identifier,
            managed=False,
            permissions=discord.Permissions.none(),
            is_default=lambda identifier=identifier: identifier == 1,
        )
        for identifier in (1, 2, 3, 4)
    }
    guild = SimpleNamespace(
        id=1, fetch_members=fetch_members, get_role=roles.get, role_objects=roles
    )

    def add(discord_id=7, name="Player", roles=(2,), account_id=100, clan=(42, "commander", "TAG")):
        member = SimpleNamespace(
            id=discord_id,
            nick=name,
            display_name=name,
            bot=False,
            guild=guild,
            roles=[SimpleNamespace(id=r) for r in roles],
            edit=AsyncMock(),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        members[discord_id] = member
        base = name.split(" [")[0]
        accounts[base.casefold()] = (account_id, base)
        memberships[account_id] = clan
        return member

    async def find(name):
        return accounts.get(name.casefold())

    async def nickname(account_id):
        return next(name for identifier, name in accounts.values() if identifier == account_id)

    async def clan(account_id):
        return memberships.get(account_id)

    async def member(discord_id):
        if discord_id not in members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "")
        return members[discord_id]

    bot.member = AsyncMock(side_effect=member)
    bot.sync_visitor_role = AsyncMock()
    bot.wg = SimpleNamespace(
        find_exact=AsyncMock(side_effect=find),
        nickname=AsyncMock(side_effect=nickname),
        clan_membership=AsyncMock(side_effect=clan),
    )

    async def direct(self, method, *args):
        return await method(*args)

    monkeypatch.setattr(Inspection, "api", direct)
    interaction = SimpleNamespace(
        guild_id=1,
        guild=guild,
        permissions=discord.Permissions(administrator=True),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
    )
    yield SimpleNamespace(
        bot=bot,
        members=members,
        add=add,
        accounts=accounts,
        clans=memberships,
        guild=guild,
        interaction=interaction,
    )
    bot.store.close()


async def test_preview_is_read_only_and_apply_imports_player_contact_visitor(env):
    member = env.add(name="Player [TAG]", roles=(2, 3, 4))
    before = list(env.bot.store.db.iterdump())
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    assert list(env.bot.store.db.iterdump()) == before
    assert inspection.metrics["importable"] == 1
    assert inspection.metrics["contacts_importable"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()
    await inspection.apply()
    assert env.bot.store.get(7)["account_id"] == 100
    assert env.bot.store.get(7)["synced_at"] == 0
    assert env.bot.store.contact(7)["clan_id"] == 42
    assert env.bot.store.contact_tags(7) == [" [TAG]"]
    env.bot.sync_visitor_role.assert_awaited_once_with(member, player=True)
    member.edit.assert_not_awaited()
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    again = Inspection(env.bot)
    await again.scan(env.guild)
    await again.apply()
    assert again.metrics["imported"] == 0
    assert again.metrics["contacts_imported"] == 0


@pytest.mark.parametrize("kind", ["missing", "wrong_tag", "api_failure"])
async def test_desynchronized_and_unknown_identities_are_not_imported(env, kind):
    env.add(name="Player [TAG]", roles=(2, 3, 4))
    if kind == "missing":
        env.accounts.clear()
    elif kind == "wrong_tag":
        env.clans[100] = (42, "commander", "NEW")
    else:
        env.bot.wg.find_exact.side_effect = ValueError("wg_error")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert inspection.metrics["wg_unknown" if kind == "api_failure" else "desynchronized"] == 1
    if kind == "api_failure":
        assert inspection.metrics["desynchronized"] == 0
    env.bot.sync_visitor_role.assert_not_awaited()


@pytest.mark.parametrize("kind", ["account_taken", "duplicate_account", "existing_other_link"])
async def test_identity_conflicts_never_replace_links(env, kind):
    env.add()
    if kind == "account_taken":
        env.bot.store.link(99, "eu", 100, "Player")
    elif kind == "duplicate_account":
        env.add(discord_id=8)
    else:
        env.bot.store.link(7, "eu", 101, "Other")
        env.accounts["other"] = (101, "Other")
    before = list(env.bot.store.db.iterdump())
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert list(env.bot.store.db.iterdump()) == before
    env.bot.sync_visitor_role.assert_not_awaited()


@pytest.mark.parametrize("kind", ["clan_taken", "duplicate_clan", "bad_rank", "missing_tag"])
async def test_contact_conflict_allows_independent_player_import(env, kind):
    env.add(name="Player" if kind == "missing_tag" else "Player [TAG]", roles=(2, 3))
    if kind == "clan_taken":
        env.bot.store.reserve_contact(99, "eu", 42)
    elif kind == "duplicate_clan":
        env.add(discord_id=8, name="Other [TAG]", roles=(2, 3), account_id=101)
    elif kind == "bad_rank":
        env.clans[100] = (42, "private", "TAG")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7)["account_id"] == 100
    assert env.bot.store.contact(7) is None
    if kind == "duplicate_clan":
        assert env.bot.store.contact(8) is None
        assert env.bot.store.get(8) is not None


async def test_visitor_failure_keeps_imported_link(env):
    env.add(roles=(2, 4))
    env.bot.sync_visitor_role.side_effect = ValueError("discord_error")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7) is not None
    assert inspection.metrics["visitor_failed"] == 1


@pytest.mark.parametrize("kind", ["name", "roles", "link", "clan"])
async def test_changes_after_preview_are_skipped(env, kind):
    member = env.add(name="Player [TAG]", roles=(2, 3, 4))
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    if kind == "name":
        member.nick = "Other"
    elif kind == "roles":
        member.roles = []
    elif kind == "link":
        env.bot.store.link(7, "eu", 102, "Other")
    else:
        env.clans[100] = (43, "commander", "NEW")
    await inspection.apply()
    assert inspection.metrics["imported"] == 0
    assert inspection.metrics["changed_during_scan"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()


async def test_incomplete_discord_enumeration_does_not_claim_absence_or_apply(env):
    member = env.add(roles=(2, 4))
    env.bot.store.link(99, "eu", 200, "Absent")

    async def partial(limit=None):
        yield member
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")

    env.guild.fetch_members = partial
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert not inspection.complete
    assert inspection.metrics["linked_absent"] == 0
    assert env.bot.store.get(7) is None
    file = report_file(env.bot, {}, inspection)
    assert env.bot.t("status_unknown") in file.fp.getvalue().decode()


async def test_live_metrics_and_missing_contact_grade_are_checked(env):
    env.add(name="Old", roles=(4,))
    env.accounts["player"] = (100, "Player")
    del env.accounts["old"]
    env.clans[100] = (43, "private", "NEW")
    env.bot.store.link(7, "eu", 100, "Player")
    env.bot.store.reserve_contact(7, "eu", 42)
    env.bot.store.link(99, "eu", 200, "Absent")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    assert inspection.metrics["linked_absent"] == 1
    assert inspection.metrics["linked_without_player"] == 1
    assert inspection.metrics["reservation_without_role"] == 1
    assert inspection.metrics["clan_changed"] == 1
    assert inspection.metrics["rank_lost"] == 1
    assert inspection.metrics["nickname_mismatch"] == 1


@pytest.mark.parametrize("name", ["stats", "admin_import_players"])
async def test_commands_private_runtime_permissions_and_report_translation(env, name):
    env.add()
    command = env.bot.tree.get_command(name)
    assert command.default_permissions.administrator
    env.interaction.permissions = discord.Permissions.none()
    with pytest.raises(app_commands.MissingPermissions):
        await command.checks[0](env.interaction)
    env.interaction.permissions = discord.Permissions(administrator=True)
    assert command.checks[0](env.interaction)
    before = list(env.bot.store.db.iterdump())
    await command.callback(env.interaction)
    env.interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert env.interaction.followup.send.call_args.kwargs["ephemeral"]
    message = env.interaction.followup.send.return_value
    final = message.edit.call_args.kwargs
    text = final["attachments"][0].fp.getvalue().decode()
    assert "PRIVATE_" not in text
    assert "<@" not in text
    assert env.bot.t("metric_importable") in text
    assert list(env.bot.store.db.iterdump()) == before
    assert all(
        "metric_" + key in CATALOGS[env.bot.config.locale] for key in Inspection(env.bot).metrics
    )
    assert all(
        "metric_" + key in CATALOGS[env.bot.config.locale]
        for key in local_statistics(env.bot.store, 4_000_000)
    )
    await env.bot.tree.get_command("stats").callback(env.interaction)
    assert env.interaction.followup.send.call_args.args == (env.bot.t("admin_busy"),)


async def test_command_confirmation_applies_import(env):
    env.add()
    await env.bot.tree.get_command("admin_import_players").callback(env.interaction, confirm=True)
    assert env.bot.store.get(7) is not None


async def test_wrong_server_and_timeout_release_operation(env, monkeypatch):
    from wot_verify import admin

    env.interaction.guild_id = 999
    with pytest.raises(ValueError, match="wrong_server"):
        await env.bot.tree.get_command("stats").callback(env.interaction)
    env.interaction.guild_id = 1
    monkeypatch.setattr(admin, "OPERATION_TIMEOUT", 0)
    await env.bot.tree.get_command("stats").callback(env.interaction)
    final = env.interaction.followup.send.return_value.edit.call_args.kwargs
    assert env.bot.t("admin_partial") in final["attachments"][0].fp.getvalue().decode()


async def test_import_rejects_foreign_region_identity(env):
    env.add()
    env.bot.store.link(7, "com", 100, "Player")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert inspection.metrics["wg_unknown"] == 1
    env.bot.wg.nickname.assert_not_awaited()
    env.bot.wg.clan_membership.assert_not_awaited()


async def test_import_rechecks_unique_constraints_after_scan(env):
    env.add(roles=(2, 4))
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    env.bot.store.link(99, "eu", 100, "Player")
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert inspection.metrics["import_failed"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()


def test_import_transaction_rolls_back_link_on_clan_conflict(env):
    env.bot.store.reserve_contact(99, "eu", 42)
    with pytest.raises(ValueError):
        env.bot.store.import_player(7, "eu", 100, "Player", clan_id=42, tag="TAG")
    assert env.bot.store.get(7) is None


async def test_contact_without_player_is_never_imported(env):
    member = env.add(name="Player [TAG]", roles=(3,))
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert inspection.metrics["contact_without_player"] == 1
    assert env.bot.store.get(member.id) is None


def test_local_statistics_preserve_callers_pending_transaction(env):
    env.bot.store.db.execute("INSERT INTO links VALUES (7, 'eu', 100, 'Player', 0)")
    local_statistics(env.bot.store, 4_000_000)
    env.bot.store.db.rollback()
    assert env.bot.store.get(7) is None


async def test_running_guard_blocks_other_admin_operations(env):
    started = asyncio.Event()
    release = asyncio.Event()
    original = env.guild.fetch_members

    async def blocked(limit=None):
        started.set()
        await release.wait()
        async for member in original(limit=limit):
            yield member

    env.guild.fetch_members = blocked
    task = asyncio.create_task(env.bot.tree.get_command("stats").callback(env.interaction))
    await started.wait()
    try:
        await env.bot.tree.get_command("admin_import_players").callback(
            env.interaction, confirm=True
        )
        assert env.interaction.followup.send.call_args.args == (env.bot.t("admin_busy"),)
    finally:
        release.set()
        await task


async def test_operation_failure_releases_guard_after_cooldown(env, monkeypatch):
    from wot_verify import admin

    env.interaction.followup.send.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), ""
    )
    with pytest.raises(discord.Forbidden):
        await env.bot.tree.get_command("stats").callback(env.interaction)
    env.interaction.followup.send.side_effect = None
    monkeypatch.setattr(admin, "OPERATION_COOLDOWN", 0)
    await env.bot.tree.get_command("stats").callback(env.interaction)
    assert env.interaction.followup.send.call_args.kwargs["wait"]


async def test_unknown_recheck_is_reported_without_import(env):
    env.add()
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    env.bot.wg.find_exact.side_effect = ValueError("wg_error")
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert inspection.metrics["changed_during_scan"] == 0
    assert inspection.metrics["wg_unknown"] == 1
    assert inspection.metrics["import_failed"] == 1


async def test_timeout_during_application_preserves_completed_results(env, monkeypatch):
    from wot_verify import admin

    env.add()
    env.add(discord_id=8, name="Other", account_id=101)
    original = Inspection.apply

    async def interrupted(self):
        # Apply just one reviewed identity, then simulate the operation deadline.
        self.reviews = self.reviews[:1]
        await original(self)
        raise TimeoutError

    monkeypatch.setattr(admin.Inspection, "apply", interrupted)
    await env.bot.tree.get_command("admin_import_players").callback(env.interaction, confirm=True)
    assert env.bot.store.db.execute("SELECT count(*) FROM links").fetchone()[0] == 1
    final = env.interaction.followup.send.return_value.edit.call_args.kwargs
    assert env.bot.t("admin_partial") in final["attachments"][0].fp.getvalue().decode()


async def test_discord_change_at_final_fetch_is_skipped(env):
    env.add(roles=(2, 4))
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    member = env.members[7]
    calls = 0

    async def fetch(discord_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            member.roles = []
        return member

    env.bot.member.side_effect = fetch
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert inspection.metrics["changed_during_scan"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()


async def test_nickname_falls_back_to_display_name(env):
    member = env.add()
    member.nick = None
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7)["account_id"] == 100


async def test_api_starts_are_spaced_and_concurrency_bounded(tmp_path):
    # Use the real rate limiter, independently from the fixture's fast API double.
    bot = VerifyBot(
        Config(
            discord_token="token",
            wg_app_id="app",
            state_secret="x" * 32,
            guild_id=1,
            role_id=2,
            public_url="https://example.org",
            database=str(tmp_path / "rate.sqlite3"),
        )
    )
    try:
        inspection = Inspection(bot)
        starts = []
        active = 0
        maximum = 0

        async def request():
            nonlocal active, maximum
            starts.append(asyncio.get_running_loop().time())
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.7)
            active -= 1

        await asyncio.gather(*(inspection.api(request) for _ in range(3)))
        assert maximum <= 2
        assert all(right - left >= 0.48 for left, right in pairwise(starts))
    finally:
        bot.store.close()


async def test_absent_contact_clan_and_rank_are_still_checked_without_role_mutation(env):
    env.bot.store.link(99, "eu", 200, "Departed")
    env.bot.store.reserve_contact(99, "eu", 42)
    env.clans[200] = (43, "private", "NEW")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert inspection.metrics["linked_absent"] == 1
    assert inspection.metrics["clan_changed"] == 1
    assert inspection.metrics["rank_lost"] == 1
    assert inspection.processed == inspection.total == 1
    assert env.bot.store.contact(99)["clan_id"] == 42
    env.bot.member.assert_not_awaited()
    env.bot.sync_visitor_role.assert_not_awaited()


async def test_invalid_identity_does_not_block_valid_candidate_for_free_clan(env):
    env.add(name="Player [TAG]", roles=(2, 3))
    env.bot.store.link(99, "eu", 100, "Player")
    env.add(discord_id=8, name="Other [TAG]", roles=(2, 3), account_id=101)
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert env.bot.store.get(8)["account_id"] == 101
    assert env.bot.store.contact(8)["clan_id"] == 42
    assert inspection.metrics["identity_conflict"] == 1
    assert inspection.metrics["contacts_imported"] == 1


@pytest.mark.parametrize("failure", ["search", "clan"])
async def test_ambiguous_names_remain_blocked_when_one_api_check_fails(env, failure):
    env.add(name="Player [TAG]", roles=(2, 3))
    env.add(discord_id=8, name="player", roles=(2,))
    calls = {"search": 0, "clan": 0}

    async def find(name):
        calls["search"] += 1
        if failure == "search" and calls["search"] == 1:
            raise ValueError("wg_error")
        return 100, "Player"

    async def clan(account_id):
        calls["clan"] += 1
        if failure == "clan" and calls["clan"] == 1:
            raise ValueError("wg_error")
        return 42, "commander", "TAG"

    env.bot.wg.find_exact.side_effect = find
    env.bot.wg.clan_membership.side_effect = clan
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert env.bot.store.get(8) is None
    assert inspection.metrics["wg_unknown"] == 1
    assert inspection.metrics["identity_conflict"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()


@pytest.mark.parametrize("kind", ["default", "admin", "managed", "missing"])
async def test_import_reuses_player_role_safety_checks(env, kind):
    if kind == "default":
        env.bot.config = replace(env.bot.config, role_id=1)
        env.add(roles=(1, 4))
    else:
        env.add(roles=(2, 4))
        if kind == "admin":
            env.guild.role_objects[2].permissions.administrator = True
        elif kind == "managed":
            env.guild.role_objects[2].managed = True
        else:
            del env.guild.role_objects[2]
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7) is None
    assert inspection.metrics["unsafe_role"] == 1
    env.bot.sync_visitor_role.assert_not_awaited()


@pytest.mark.parametrize("kind", ["admin", "managed", "missing"])
async def test_unsafe_contact_role_does_not_create_reservation(env, kind):
    env.add(name="Player [TAG]", roles=(2, 3))
    if kind == "admin":
        env.guild.role_objects[3].permissions.administrator = True
    elif kind == "managed":
        env.guild.role_objects[3].managed = True
    else:
        del env.guild.role_objects[3]
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7)["account_id"] == 100
    assert env.bot.store.contact(7) is None
    assert inspection.metrics["unsafe_role"] == 1


async def test_existing_owner_keeps_priority_over_unknown_duplicate(env):
    owner = env.add(roles=(2, 4))
    env.bot.store.link(7, "eu", 100, "Player")
    env.add(discord_id=8)
    env.bot.wg.find_exact.side_effect = ValueError("wg_error")
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.get(7)["account_id"] == 100
    assert env.bot.store.get(8) is None
    env.bot.sync_visitor_role.assert_awaited_once_with(owner, player=True)


@pytest.mark.parametrize("role_id", [2, 3])
async def test_role_safety_is_rechecked_at_final_member_fetch(env, role_id):
    member = env.add(name="Player [TAG]", roles=(2, 3, 4))
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    calls = 0

    async def fetch(discord_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            env.guild.role_objects[role_id].permissions.administrator = True
        return member

    env.bot.member.side_effect = fetch
    await inspection.apply()
    assert env.bot.store.contact(7) is None
    if role_id == 2:
        assert env.bot.store.get(7) is None
        env.bot.sync_visitor_role.assert_not_awaited()
    else:
        assert env.bot.store.get(7)["account_id"] == 100
        env.bot.sync_visitor_role.assert_awaited_once_with(member, player=True)
    assert inspection.metrics["unsafe_role"] == 1


async def test_repeated_partial_imports_make_progress_past_completed_players(env, monkeypatch):
    env.add(discord_id=7, name="Done", account_id=100)
    env.bot.store.link(7, "eu", 100, "Done")
    env.add(discord_id=8, name="Pending", account_id=101)
    env.add(discord_id=9, name="Last", account_id=102)

    async def direct(self, method, *args):
        return await method(*args)

    # The remaining operation budget allows one identity recheck per invocation.
    # Cancelling an awaited API call mirrors asyncio.timeout's cancellation.
    for expected_id in (8, 9):
        monkeypatch.setattr(Inspection, "api", direct)
        inspection = Inspection(env.bot)
        await inspection.scan(env.guild)
        calls = 0

        async def deadline(self, method, *args):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise asyncio.CancelledError
            return await method(*args)

        monkeypatch.setattr(Inspection, "api", deadline)
        try:
            await inspection.apply()
        except asyncio.CancelledError:
            pass
        assert env.bot.store.get(expected_id) is not None
        assert inspection.metrics["imported"] == 1


async def test_already_linked_contact_still_imports_missing_tag_tracking(env):
    env.add(name="Player [TAG]", roles=(2, 3))
    env.bot.store.link(7, "eu", 100, "Player")
    env.bot.store.reserve_contact(7, "eu", 42)
    inspection = Inspection(env.bot)
    await inspection.scan(env.guild)
    await inspection.apply()
    assert env.bot.store.contact_tags(7) == [" [TAG]"]
