import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from wot_verify.bot import LoginView, VerifyBot
from wot_verify.store import Store


@pytest.fixture
def bot(tmp_path):
    client = VerifyBot(
        SimpleNamespace(
            database=str(tmp_path / "bot.sqlite3"),
            guild_id=1,
            role_id=2,
            clan_role_id=3,
            state_secret="x" * 32,
        )
    )
    role = SimpleNamespace(
        id=3,
        managed=False,
        permissions=SimpleNamespace(administrator=False),
        is_default=lambda: False,
    )
    member = SimpleNamespace(
        id=10,
        nick="Old",
        guild=SimpleNamespace(get_role=lambda _: role),
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
        edit=AsyncMock(),
    )
    client.member = AsyncMock(return_value=member)
    client.wg = SimpleNamespace(
        clan_membership=AsyncMock(return_value=(42, "commander", "TAG")),
        nickname=AsyncMock(return_value="Player"),
    )
    client.is_ready = lambda: True
    client.store.link(10, "eu", 100, "Player")
    yield client, member
    client.store.close()


@pytest.mark.parametrize("rank", ["commander", "executive_officer", "recruitment_officer"])
async def test_contact_role_and_tag_for_allowed_grades(bot, rank):
    client, member = bot
    client.wg.clan_membership.return_value = (42, rank, "TAG")
    async with client.lock:
        await client.claim_contact(10)
    member.add_roles.assert_awaited_once()
    assert member.edit.await_args.kwargs["nick"] == "Player [TAG]"
    assert client.store.contact(10)["clan_id"] == 42
    assert LoginView(client).is_persistent()


@pytest.mark.parametrize(
    "membership", [None, (42, "private", "TAG"), (42, "personnel_officer", "TAG")]
)
async def test_ineligible_does_not_receive_role(bot, membership):
    client, member = bot
    client.wg.clan_membership.return_value = membership
    with pytest.raises(ValueError, match="clan_ineligible"):
        await client.claim_contact(10)
    member.add_roles.assert_not_awaited()
    assert client.store.contact(10) is None


async def test_unlinked_disabled_and_api_failure(bot):
    client, member = bot
    with pytest.raises(ValueError, match="clan_not_linked"):
        await client.claim_contact(999)
    client.wg.clan_membership.side_effect = ValueError("wg_error")
    with pytest.raises(ValueError, match="wg_error"):
        await client.claim_contact(10)
    client.config.clan_role_id = 0
    with pytest.raises(ValueError, match="clan_disabled"):
        await client.claim_contact(10)
    member.add_roles.assert_not_awaited()


async def test_concurrent_claims_require_manual_review_for_loser(bot):
    client, member = bot
    client.store.link(11, "eu", 101, "Other")

    async def claim(user):
        async with client.lock:
            await client.claim_contact(user)

    results = await asyncio.gather(claim(10), claim(11), return_exceptions=True)
    assert sum(isinstance(r, ValueError) and str(r) == "clan_taken" for r in results) == 1
    assert member.add_roles.await_count == 1


async def test_nickname_failure_keeps_slot_and_resync_keeps_suffix(bot):
    client, member = bot
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
    with pytest.raises(ValueError, match="clan_nickname_error"):
        await client.claim_contact(10)
    assert client.store.contact(10)
    member.edit.side_effect = None
    client.role = lambda _: "player-role"
    await client.sync(client.store.get(10))
    assert member.edit.await_args.kwargs["nick"] == "Player [TAG]"


async def test_forbidden_grant_releases_new_slot_and_name_limit(bot):
    client, member = bot
    client.wg.nickname.return_value = "x" * 32
    with pytest.raises(ValueError, match="clan_name_long"):
        await client.claim_contact(10)
    assert client.store.contact(10) is None
    client.wg.nickname.return_value = "Player"
    member.add_roles.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), ""
    )
    with pytest.raises(discord.Forbidden):
        await client.claim_contact(10)
    assert client.store.contact(10) is None


async def test_manual_release_and_changed_clan_require_review(bot):
    client, member = bot
    await client.claim_contact(10)
    client.wg.clan_membership.return_value = (43, "commander", "NEW")
    with pytest.raises(ValueError, match="clan_review"):
        await client.claim_contact(10)
    with pytest.raises(ValueError, match="clan_review"):
        await client.contact_nickname(client.store.get(10), "Player")
    member.remove_roles.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), ""
    )
    with pytest.raises(discord.Forbidden):
        await client.remove_contact(10)
    assert client.store.contact(10)
    member.remove_roles.side_effect = None
    await client.remove_contact(10)
    assert client.store.contact(10) is None
    assert client.store.get(10) is not None


async def test_moderator_permission_is_enforced(bot):
    client, _ = bot
    command = client.tree.get_command("admin_contact_remove")
    with pytest.raises(app_commands.MissingPermissions):
        await command.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))


async def test_unlink_removes_contact_role_and_reservation(bot):
    client, member = bot
    await client.claim_contact(10)
    client.role = lambda _: "player-role"
    await client.remove(10)
    assert member.remove_roles.await_count == 2
    assert client.store.contact(10) is None
    assert client.store.get(10) is None


def test_unique_contact_survives_restart_and_can_be_released(tmp_path):
    path = str(tmp_path / "contacts.sqlite3")
    first = Store(path)
    second = Store(path)
    try:
        first.link(1, "eu", 10, "Player")
        first.reserve_contact(1, "eu", 42)
        with pytest.raises(ValueError, match="clan_taken"):
            second.reserve_contact(2, "eu", 42)
        with pytest.raises(ValueError, match="clan_taken"):
            second.reserve_contact(1, "eu", 43)
        second.reserve_contact(1, "eu", 42)  # Safe retry of the same reservation.
        first.close()
        first = Store(path)
        assert first.clan_contact("eu", 42)["discord_id"] == 1
        first.unlink(1)
        second.reserve_contact(2, "eu", 42)
        assert second.contact(2)["clan_id"] == 42
    finally:
        first.close()
        second.close()
