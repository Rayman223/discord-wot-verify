from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from wot_verify.bot import LoginView, VerifyBot


@pytest.fixture
def bot(tmp_path):
    instance = VerifyBot(
        SimpleNamespace(
            database=str(tmp_path / "test.sqlite3"),
            state_secret="x" * 32,
            guild_id=123,
            role_id=456,
        )
    )
    yield instance
    instance.store.close()


def test_persistent_view_and_commands(bot):
    assert LoginView(bot).is_persistent()
    assert {c.name for c in bot.tree.get_commands()} == {
        "setup",
        "status",
        "diagnose",
        "resync",
        "unlink",
        "admin_unlink",
        "admin_contact_remove",
        "contacts",
    }


async def test_admin_permissions_enforced_at_runtime(bot):
    command = bot.tree.get_command("admin_unlink")
    interaction = SimpleNamespace(permissions=discord.Permissions.none())
    with pytest.raises(app_commands.MissingPermissions):
        await command.checks[0](interaction)


async def test_unlink_permission_failure_preserves_link(bot):
    bot.store.link(1, "eu", 10, "Player")
    member = SimpleNamespace(
        remove_roles=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
        )
    )
    bot.member = AsyncMock(return_value=member)
    bot.role = lambda _: "role"
    with pytest.raises(discord.Forbidden):
        await bot.remove(1)
    assert bot.store.get(1) is not None


async def test_admin_can_release_departed_member(bot):
    bot.store.link(1, "eu", 10, "Player")
    bot.member = AsyncMock(
        side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "")
    )
    await bot.remove(1)
    assert bot.store.get(1) is None


async def test_join_restores_role_without_renaming(bot):
    bot.store.link(1, "eu", 10, "Player")
    member = SimpleNamespace(
        id=1,
        guild=SimpleNamespace(id=123),
        add_roles=AsyncMock(),
        send=AsyncMock(),
        edit=AsyncMock(),
    )
    bot.role = lambda _: "role"
    await bot.on_member_join(member)
    member.add_roles.assert_awaited_once()
    member.send.assert_awaited_once()
    member.edit.assert_not_awaited()


async def test_rename_refusal_distinguished_from_role_failure(bot):
    bot.store.link(1, "eu", 10, "Player")
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
    member = SimpleNamespace(
        id=1, nick="OldName", add_roles=AsyncMock(), edit=AsyncMock(side_effect=forbidden)
    )
    bot.member = AsyncMock(return_value=member)
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    bot.role = lambda _: "role"
    with pytest.raises(ValueError, match="nickname_blocked"):
        await bot.sync(bot.store.get(1))
    member.add_roles.assert_awaited_once()
    member.add_roles.side_effect = forbidden
    with pytest.raises(discord.Forbidden):
        await bot.sync(bot.store.get(1))


async def test_matching_nickname_needs_no_edit(bot):
    bot.store.link(1, "eu", 10, "Player")
    member = SimpleNamespace(id=1, nick="Player", add_roles=AsyncMock(), edit=AsyncMock())
    bot.member = AsyncMock(return_value=member)
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    bot.role = lambda _: "role"
    await bot.sync(bot.store.get(1))
    member.edit.assert_not_awaited()
    assert bot.store.get(1)["synced_at"] > 0


async def test_failed_syncs_alert_once_and_success_resets_counter(bot):
    bot.store.link(1, "eu", 10, "Player")
    row = bot.store.get(1)
    error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
    bot.member = AsyncMock(side_effect=error)
    bot.audit.emit = AsyncMock()
    for _ in range(3):
        with pytest.raises(discord.Forbidden):
            await bot.sync(row)
    bot.audit.emit.assert_awaited_once_with("sync_failed", 1)
    bot.member.side_effect = None
    bot.member.return_value = SimpleNamespace(id=1, nick="Player", add_roles=AsyncMock())
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    bot.role = lambda _: "role"
    await bot.sync(row)
    bot.member.side_effect = error
    with pytest.raises(discord.Forbidden):
        await bot.sync(row)
    bot.audit.emit.assert_awaited_once_with("sync_failed", 1)


async def test_departed_member_does_not_alert_and_keeps_link(bot):
    bot.store.link(1, "eu", 10, "Player")
    bot.store.sync_failure_notice(1, 100000)
    bot.member = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason=""), ""))
    bot.audit.emit = AsyncMock()
    for _ in range(3):
        with pytest.raises(discord.NotFound):
            await bot.sync(bot.store.get(1))
    bot.audit.emit.assert_not_awaited()
    assert bot.store.get(1) is not None
    assert bot.store.db.execute("SELECT count(*) FROM audit_failures").fetchone()[0] == 0


async def test_not_found_during_role_update_still_alerts(bot):
    bot.store.link(1, "eu", 10, "Player")
    bot.member = AsyncMock(
        return_value=SimpleNamespace(
            id=1,
            nick="Player",
            add_roles=AsyncMock(
                side_effect=discord.NotFound(SimpleNamespace(status=404, reason=""), "")
            ),
        )
    )
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    bot.role = lambda _: "role"
    bot.audit.emit = AsyncMock()
    for _ in range(2):
        with pytest.raises(discord.NotFound):
            await bot.sync(bot.store.get(1))
    bot.audit.emit.assert_awaited_once_with("sync_failed", 1)
