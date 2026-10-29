from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.contacts import contacts_report


@pytest.fixture(params=["fr", "en"])
def bot(tmp_path, request):
    bot = VerifyBot(
        Config(
            "token",
            "app",
            "x" * 32,
            1,
            2,
            "https://example.org",
            database=str(tmp_path / "test.sqlite3"),
            clan_role_id=3,
            locale=request.param,
        )
    )
    bot.member = AsyncMock(
        return_value=SimpleNamespace(roles=[SimpleNamespace(id=2), SimpleNamespace(id=3)])
    )
    bot.wg = SimpleNamespace(clan_membership=AsyncMock(return_value=(42, "commander", "TAG")))
    yield bot
    bot.store.close()


@pytest.mark.parametrize("state", ["ok", "left", "clan", "rank", "wg", "discord", "roles"])
async def test_reviews_are_read_only_and_distinguish_unknown(bot, state):
    bot.store.link(7, "eu", 100, "Player")
    bot.store.reserve_contact(7, "eu", 42)
    key = "contacts_ok"
    if state == "left":
        bot.member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        key = "contacts_left"
    elif state == "clan":
        bot.wg.clan_membership.return_value = None
        key = "contacts_clan_changed"
    elif state == "rank":
        bot.wg.clan_membership.return_value = (42, "private", "TAG")
        key = "contacts_rank_lost"
    elif state == "wg":
        bot.wg.clan_membership.side_effect = ValueError("wg_error")
        key = "contacts_wg_unknown"
    elif state == "discord":
        bot.member.side_effect = TimeoutError()
        key = "contacts_discord_unknown"
    elif state == "roles":
        bot.member.return_value.roles = []
        key = "contacts_roles_missing"
    before = list(bot.store.db.iterdump())
    report = await contacts_report(bot, 1)
    assert bot.t(key) in report.fields[0].value
    assert list(bot.store.db.iterdump()) == before
    assert len(report) < 6000


async def test_pagination_and_permissions(bot):
    for i in range(12):
        bot.store.link(i + 1, "eu", i + 100, "Player")
        bot.store.reserve_contact(i + 1, "eu", i + 42)
    first = await contacts_report(bot, 1)
    last = await contacts_report(bot, 3)
    assert len(first.fields) == 5
    assert len(last.fields) == 2
    assert {f.name for f in first.fields}.isdisjoint(f.name for f in last.fields)
    with pytest.raises(ValueError, match="contacts_bad_page"):
        await contacts_report(bot, 4)
    command = bot.tree.get_command("contacts")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        permissions=discord.Permissions.none(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    with pytest.raises(app_commands.MissingPermissions):
        command.checks[1](interaction)
    await command.callback(interaction)
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
