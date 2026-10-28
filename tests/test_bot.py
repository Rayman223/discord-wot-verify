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
        "resync",
        "unlink",
        "admin_unlink",
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
