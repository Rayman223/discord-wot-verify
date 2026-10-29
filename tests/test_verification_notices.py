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


@pytest.fixture
def contact_env(env):
    bot, member, channel = env
    bot.config = Config(
        "token",
        "app",
        "x" * 32,
        1,
        2,
        "https://example.org",
        locale=bot.config.locale,
        database=bot.config.database,
        clan_role_id=3,
    )
    member.roles = [SimpleNamespace(id=2)]
    contact_role = SimpleNamespace(
        id=3, managed=False, permissions=discord.Permissions.none(), is_default=lambda: False
    )
    member.guild = SimpleNamespace(get_role=lambda _: contact_role)

    async def add(role, **kwargs):
        if role.id not in {r.id for r in member.roles}:
            member.roles.append(role)

    member.add_roles.side_effect = add

    async def remove(role, **kwargs):
        member.roles = [r for r in member.roles if r.id != role.id]

    member.remove_roles = AsyncMock(side_effect=remove)
    bot.wg.clan_membership = AsyncMock(return_value=(42, "commander", "TAG"))
    bot.is_ready = lambda: True
    return bot, member, channel


async def test_clan_confirmation_once_per_reservation_and_only_member_ping(contact_env):
    bot, _, channel = contact_env
    await bot.claim_contact(7, channel_id=20)
    content = channel.send.call_args.args[0]
    assert content == bot.t("contact_role_confirmed").format(
        member="<@7>", role="<@&3>", clan_tag="TAG"
    )
    mentions = channel.send.call_args.kwargs["allowed_mentions"].to_dict()
    assert mentions["users"] == [7]
    assert "roles" not in mentions["parse"] and "everyone" not in mentions["parse"]
    await bot.claim_contact(7, channel_id=20)
    assert channel.send.await_count == 1
    assert bot.store.contact_notice(7) is None
    assert bot.store.verification_notice(7) is None
    bot.store.close()
    bot.store = Store(bot.config.database)
    await bot.claim_contact(7, channel_id=20)
    assert channel.send.await_count == 1
    await bot.remove_contact(7)
    await bot.claim_contact(7, channel_id=20)
    assert channel.send.await_count == 2


@pytest.mark.parametrize("failure", ["occupied", "role", "nickname", "timeout"])
async def test_contact_message_reflects_only_confirmed_role_assignment(contact_env, failure):
    bot, member, channel = contact_env
    if failure == "occupied":
        bot.store.reserve_contact(99, "eu", 42)
    elif failure == "role":
        member.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    elif failure == "nickname":
        member.nick = "Old"
        member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    else:
        member.add_roles.side_effect = TimeoutError()
    with pytest.raises((ValueError, discord.Forbidden, TimeoutError)):
        await bot.claim_contact(7, channel_id=20)
    if failure == "nickname":
        assert "/resync" in channel.send.call_args.args[0]
        assert "<@&3>" in channel.send.call_args.args[0]
    else:
        channel.send.assert_not_awaited()
        assert bot.store.contact_notice(7) is None


async def test_failed_contact_post_retried_by_sync_with_latest_clan_tag(contact_env):
    bot, _, channel = contact_env
    channel.send.side_effect = TimeoutError()
    await bot.claim_contact(7, channel_id=20)
    assert bot.store.contact_notice(7) is not None
    channel.send.side_effect = None
    channel.send.reset_mock()
    bot.wg.clan_membership.return_value = (42, "commander", "NEW")
    await bot.sync(bot.store.get(7))
    channel.send.assert_awaited_once()
    assert "[NEW]" in channel.send.call_args.args[0]
    assert bot.store.contact_notice(7) is None


async def test_contact_withdrawal_cancels_pending_confirmation(contact_env):
    bot, _, channel = contact_env
    channel.send.side_effect = TimeoutError()
    await bot.claim_contact(7, channel_id=20)
    assert bot.store.contact_notice(7) is not None
    await bot.remove_contact(7)
    assert bot.store.contact_notice(7) is None
    channel.send.reset_mock()
    channel.send.side_effect = None
    await bot.sync(bot.store.get(7))
    channel.send.assert_not_awaited()


async def test_unlink_cancels_pending_contact_confirmation(contact_env):
    bot, _, channel = contact_env
    channel.send.side_effect = TimeoutError()
    await bot.claim_contact(7, channel_id=20)
    bot.store.unlink(7)
    assert bot.store.contact_notice(7) is None


async def test_existing_contact_without_pending_notice_is_not_announced(contact_env):
    bot, member, channel = contact_env
    bot.store.reserve_contact(7, "eu", 42)
    member.roles.append(SimpleNamespace(id=3))
    await bot.sync(bot.store.get(7))
    channel.send.assert_not_awaited()
