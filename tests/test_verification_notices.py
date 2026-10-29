from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.store import Store


@pytest.fixture(params=["fr", "en"])
def env(tmp_path, request):
    bot = VerifyBot(
        Config(
            "token",
            "app",
            "x" * 32,
            1,
            2,
            "https://example.org",
            locale=request.param,
            database=str(tmp_path / "notice.sqlite3"),
        )
    )
    bot.store.link(7, "eu", 100, "Player")
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=1)
    channel.send = AsyncMock()
    bot.get_channel = lambda _: channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.wg = SimpleNamespace(nickname=AsyncMock(return_value="Player"))
    member = SimpleNamespace(id=7, nick="Player", add_roles=AsyncMock(), edit=AsyncMock())
    bot.member = AsyncMock(return_value=member)
    bot.role = lambda _: SimpleNamespace(id=2)
    yield bot, member, channel
    bot.store.close()


async def test_confirmation_once_per_link_and_only_member_ping(env):
    bot, member, channel = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    await bot.sync(bot.store.get(7))
    member.add_roles.assert_awaited_once()
    channel.send.assert_awaited_once()
    text = channel.send.call_args.args[0]
    assert text == bot.t("verification_role_confirmed").format(member="<@7>", role="<@&2>")
    mentions = channel.send.call_args.kwargs["allowed_mentions"].to_dict()
    assert mentions["users"] == [7]
    assert "roles" not in mentions["parse"] and "everyone" not in mentions["parse"]
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    await bot.sync(bot.store.get(7))
    assert channel.send.await_count == 1
    assert bot.store.verification_notice(7) is None


async def test_no_notification_for_existing_links_without_web_verification(env):
    bot, _, channel = env
    await bot.sync(bot.store.get(7))
    channel.send.assert_not_awaited()


async def test_no_confirmation_if_role_assignment_fails_then_resync_sends_it(env):
    bot, member, channel = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    member.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(discord.Forbidden):
        await bot.sync(bot.store.get(7))
    channel.send.assert_not_awaited()
    assert bot.store.verification_notice(7) is not None
    member.add_roles.side_effect = None
    await bot.sync(bot.store.get(7))
    channel.send.assert_awaited_once()


async def test_role_is_confirmed_even_when_nickname_is_blocked(env):
    bot, member, channel = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    member.nick = "Old"
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(ValueError, match="nickname_blocked"):
        await bot.sync(bot.store.get(7))
    assert "/resync" in channel.send.call_args.args[0]
    member.edit.side_effect = None
    await bot.sync(bot.store.get(7))
    assert channel.send.await_count == 1


@pytest.mark.parametrize("failure", ["permission", "timeout", "wrong_guild"])
async def test_notice_failure_never_cancels_verification_and_stays_pending(env, failure):
    bot, _, channel = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    if failure == "permission":
        channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    elif failure == "timeout":
        channel.send.side_effect = TimeoutError()
    else:
        channel.guild.id = 99
    await bot.sync(bot.store.get(7))
    assert bot.store.get(7)["synced_at"] > 0
    assert bot.store.verification_notice(7) is not None
    if failure == "wrong_guild":
        channel.send.assert_not_awaited()
    channel.send.side_effect = None
    channel.guild.id = 1
    await bot.sync(bot.store.get(7))
    assert bot.store.verification_notice(7) is None


async def test_saved_panel_channel_takes_precedence_over_login_channel(env):
    bot, _, _ = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    bot.store.save_panel(1, 30, 40)
    bot.get_channel = Mock(return_value=None)
    await bot.sync(bot.store.get(7))
    bot.fetch_channel.assert_awaited_once_with(30)


async def test_notification_state_survives_restart_and_unlink_resets_it(env):
    bot, _, channel = env
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    await bot.sync(bot.store.get(7))
    bot.store.close()
    bot.store = Store(bot.config.database)
    await bot.sync(bot.store.get(7))
    assert channel.send.await_count == 1
    bot.store.unlink(7)
    bot.store.link(7, "eu", 100, "Player")
    bot.store.queue_verification_notice(7, "eu", 100, 20)
    await bot.sync(bot.store.get(7))
    assert channel.send.await_count == 2
