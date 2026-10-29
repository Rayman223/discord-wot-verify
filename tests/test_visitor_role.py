from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from wot_verify.bot import VerifyBot
from wot_verify.config import Config


@pytest.fixture
def env(tmp_path):
    bot = VerifyBot(
        Config(
            "token",
            "app",
            "x" * 32,
            1,
            2,
            "https://example.org",
            database=str(tmp_path / "visitor.sqlite3"),
            visitor_role_id=4,
        )
    )
    roles = {
        i: SimpleNamespace(
            id=i, managed=False, is_default=lambda: False, permissions=discord.Permissions.none()
        )
        for i in (2, 3, 4, 5)
    }
    member = SimpleNamespace(
        id=7,
        nick="Player",
        roles=[roles[4], roles[5]],
        guild=SimpleNamespace(id=1, get_role=roles.get),
        edit=AsyncMock(),
        send=AsyncMock(),
    )

    async def add(role, **kwargs):
        if role.id not in {r.id for r in member.roles}:
            member.roles.append(role)

    async def remove(role, **kwargs):
        member.roles = [r for r in member.roles if r.id != role.id]

    member.add_roles = AsyncMock(side_effect=add)
    member.remove_roles = AsyncMock(side_effect=remove)
    bot.member = AsyncMock(return_value=member)
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    bot.store.link(7, "eu", 100, "Player")
    yield bot, member, roles
    bot.store.close()


def role_ids(member):
    return {role.id for role in member.roles}


@pytest.mark.parametrize("action", ["sync", "join"])
async def test_player_assignment_removes_visitor_and_preserves_other_roles(env, action):
    bot, member, _ = env
    if action == "sync":
        await bot.sync(bot.store.get(7))
    else:
        await bot.on_member_join(member)
    assert role_ids(member) == {2, 5}
    member.remove_roles.assert_awaited_once()


async def test_unlink_restores_visitor(env):
    bot, member, roles = env
    member.roles = [roles[2], roles[5]]
    await bot.remove(7)
    assert role_ids(member) == {4, 5}
    assert bot.store.get(7) is None


@pytest.mark.parametrize("player", [False, True])
async def test_manual_role_change_works_without_clan_feature(env, player):
    bot, member, roles = env
    before = SimpleNamespace(roles=[roles[4]] if player else [roles[2]])
    member.roles = [roles[2], roles[4]] if player else []
    await bot.on_member_update(before, member)
    assert role_ids(member) == ({2} if player else {4})


async def test_stale_player_removal_keeps_current_player_and_removes_visitor(env):
    bot, member, roles = env
    member.roles = [roles[2], roles[4]]
    await bot.on_member_update(
        SimpleNamespace(roles=[roles[2]]), SimpleNamespace(id=7, guild=member.guild, roles=[])
    )
    assert role_ids(member) == {2}


async def test_departure_after_player_addition_preserves_clan_reservation(env):
    bot, member, roles = env
    bot.config = replace(bot.config, clan_role_id=3)
    bot.store.reserve_contact(7, "eu", 42)
    bot.member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason=""), "")
    # Restoring player on rejoin creates an addition event. The member leaves
    # before this queued event can fetch them: this is not a contact withdrawal.
    before = SimpleNamespace(roles=[])
    after = SimpleNamespace(id=7, guild=member.guild, roles=[roles[2]])
    await bot.on_member_update(before, after)
    assert bot.store.contact(7) is not None
    assert bot.store.clan_contact("eu", 42)["discord_id"] == 7
    assert bot.store.get(7) is not None


async def test_visitor_only_event_does_not_loop(env):
    bot, member, roles = env
    before = SimpleNamespace(roles=[roles[2], roles[4]])
    member.roles = [roles[2]]
    await bot.on_member_update(before, member)
    bot.member.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()


async def test_foreign_guild_ignored(env):
    bot, _, roles = env
    await bot.on_member_update(
        SimpleNamespace(roles=[roles[2]]),
        SimpleNamespace(id=7, guild=SimpleNamespace(id=99), roles=[]),
    )
    bot.member.assert_not_awaited()


async def test_failed_player_assignment_keeps_visitor(env):
    bot, member, _ = env
    member.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(discord.Forbidden):
        await bot.sync(bot.store.get(7))
    assert role_ids(member) == {4, 5}
    member.remove_roles.assert_not_awaited()


async def test_failed_player_removal_does_not_grant_visitor(env):
    bot, member, roles = env
    member.roles = [roles[2]]
    member.remove_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(discord.Forbidden):
        await bot.remove(7)
    assert role_ids(member) == {2}
    member.add_roles.assert_not_awaited()
    assert bot.store.get(7) is not None


async def test_blocked_nickname_does_not_keep_visitor_after_grant(env):
    bot, member, _ = env
    member.nick = "Old"
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(ValueError, match="nickname_blocked"):
        await bot.sync(bot.store.get(7))
    assert role_ids(member) == {2, 5}


async def test_visitor_cleanup_failure_can_be_retried_by_resync(env):
    bot, member, _ = env
    remove = member.remove_roles.side_effect
    member.remove_roles.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await bot.sync(bot.store.get(7))
    assert bot.store.get(7)["synced_at"] == 0
    assert role_ids(member) == {2, 4, 5}
    member.remove_roles.side_effect = remove
    await bot.sync(bot.store.get(7))
    assert role_ids(member) == {2, 5}


async def test_disabled_switch_preserves_existing_roles(env):
    bot, member, _ = env
    bot.config = replace(bot.config, visitor_role_id=0)
    await bot.sync(bot.store.get(7))
    assert role_ids(member) == {2, 4, 5}
    member.remove_roles.assert_not_awaited()


async def test_visitor_failure_preserves_link_for_unlink_retry(env):
    bot, member, roles = env
    member.roles = [roles[2]]
    add = member.add_roles.side_effect
    member.add_roles.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await bot.remove(7)
    assert bot.store.get(7) is not None
    assert role_ids(member) == set()
    member.add_roles.side_effect = add
    await bot.remove(7)
    assert bot.store.get(7) is None
    assert role_ids(member) == {4}


async def test_visitor_failure_does_not_block_contact_cleanup(env):
    bot, member, roles = env
    bot.config = replace(bot.config, clan_role_id=3)
    bot.store.reserve_contact(7, "eu", 42)
    member.roles = [roles[3]]
    member.add_roles.side_effect = TimeoutError()
    await bot.on_member_update(SimpleNamespace(roles=[roles[2], roles[3]]), member)
    assert bot.store.contact(7) is None
    assert 3 not in role_ids(member)


@pytest.mark.parametrize("invalid", ["missing", "managed", "admin", "default"])
async def test_unsafe_visitor_role_never_granted(env, invalid):
    bot, member, roles = env
    if invalid == "missing":
        del roles[4]
    elif invalid == "managed":
        roles[4].managed = True
    elif invalid == "admin":
        roles[4].permissions = discord.Permissions(administrator=True)
    else:
        roles[4].is_default = lambda: True
    with pytest.raises(ValueError, match="discord_error"):
        await bot.sync_visitor_role(member, player=False)
    member.add_roles.assert_not_awaited()


@pytest.mark.parametrize("value", ["1", "2", "3", "-1", str(2**63)])
def test_config_rejects_invalid_visitor_ids(monkeypatch, value):
    for key, val in {
        "DISCORD_TOKEN": "token",
        "WG_APP_ID": "app",
        "STATE_SECRET": "x" * 32,
        "GUILD_ID": "1",
        "ROLE_ID": "2",
        "CLAN_CONTACT_ROLE_ID": "3",
        "PUBLIC_URL": "https://example.org",
        "VISITOR_ROLE_ID": value,
    }.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(ValueError, match="VISITOR_ROLE_ID"):
        Config.load()
