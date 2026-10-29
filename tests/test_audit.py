from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from wot_verify.audit import AuditJournal
from wot_verify.i18n import t
from wot_verify.store import Store


@pytest.fixture
def env():
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=1, default_role=object(), me=SimpleNamespace(id=99))
    channel.permissions_for.return_value = discord.Permissions.none()
    channel.overwrites = {}
    channel.send = AsyncMock()
    bot = SimpleNamespace(
        config=SimpleNamespace(guild_id=1, log_channel_id=20),
        get_channel=lambda _: channel,
        fetch_channel=AsyncMock(),
        t=t,
    )
    return bot, channel


async def test_journal_sends_only_fixed_event_and_ids(env):
    bot, channel = env
    await AuditJournal(bot).emit("linked", 123)
    content = channel.send.call_args.args[0]
    assert "123" in content
    assert t("audit_linked") in content
    assert not channel.send.call_args.kwargs["allowed_mentions"].everyone
    channel.send.reset_mock()
    await AuditJournal(bot).emit("https://secret.example/token", 123)
    channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    ["disabled", "public", "wrong_guild", "unprivileged_role", "unprivileged_member", "failure"],
)
async def test_journal_privacy_and_failure_isolation(env, kind):
    bot, channel = env
    if kind == "disabled":
        bot.config.log_channel_id = 0
    elif kind == "public":
        channel.permissions_for.return_value.view_channel = True
    elif kind == "wrong_guild":
        channel.guild.id = 2
    elif kind == "unprivileged_role":
        role = Mock(spec=discord.Role)
        role.permissions = discord.Permissions.none()
        role.is_bot_managed.return_value = False
        channel.overwrites = {role: discord.PermissionOverwrite(view_channel=True)}
    elif kind == "unprivileged_member":
        member = Mock(spec=discord.Member)
        member.guild_permissions = discord.Permissions.none()
        member.id = 123
        channel.overwrites = {member: discord.PermissionOverwrite(view_channel=True)}
    else:
        channel.send.side_effect = TimeoutError()
    await AuditJournal(bot).emit("unlinked", 7)
    if kind != "failure":
        channel.send.assert_not_awaited()


def test_repeated_failures_are_throttled_across_restart(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    db = Store(path)
    assert not db.sync_failure_notice(7, 100000)
    assert db.sync_failure_notice(7, 100100)
    db.close()
    db = Store(path)
    assert not db.sync_failure_notice(7, 100200)
    assert db.sync_failure_notice(7, 200000)
    db.clear_sync_failures(7)
    assert not db.sync_failure_notice(7, 200100)
    db.unlink(7)
    assert db.db.execute("SELECT count(*) FROM audit_failures").fetchone()[0] == 0
    db.close()
