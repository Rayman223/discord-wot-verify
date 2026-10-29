from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from wot_verify.i18n import t
from wot_verify.panel import update_panel
from wot_verify.store import Store


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_panel_survives_restart_and_updates_in_place(tmp_path, locale):
    path = str(tmp_path / "test.sqlite3")
    db = Store(path)
    channel = SimpleNamespace(
        id=20, guild=SimpleNamespace(id=1), send=AsyncMock(return_value=SimpleNamespace(id=30))
    )
    message = SimpleNamespace(edit=AsyncMock())
    channel.fetch_message = AsyncMock(return_value=message)
    bot = SimpleNamespace(
        config=SimpleNamespace(guild_id=1),
        store=db,
        t=lambda k: t(k, locale),
        get_channel=lambda _: channel,
    )
    await update_panel(bot, channel, None)
    db.close()
    bot.store = Store(path)
    await update_panel(bot, channel, None)
    channel.send.assert_awaited_once()
    message.edit.assert_awaited_once_with(content=t("panel", locale), view=None)
    other = SimpleNamespace(id=40, send=AsyncMock())
    assert "20" in await update_panel(bot, other, None)
    other.send.assert_not_awaited()
    channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason=""), "")
    other.send.return_value = SimpleNamespace(id=50)
    await update_panel(bot, other, None)
    assert bot.store.panel(1)["channel_id"] == 40
    bot.store.close()


async def test_inaccessible_panel_never_creates_a_duplicate(tmp_path):
    db = Store(str(tmp_path / "test.sqlite3"))
    db.save_panel(1, 20, 30)
    channel = SimpleNamespace(
        id=20,
        fetch_message=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
        ),
        send=AsyncMock(),
        guild=SimpleNamespace(id=1),
    )
    bot = SimpleNamespace(
        config=SimpleNamespace(guild_id=1), store=db, t=t, get_channel=lambda _: channel
    )
    with pytest.raises(ValueError, match="panel_inaccessible"):
        await update_panel(bot, channel, None)
    channel.send.assert_not_awaited()
    assert db.panel(1)["message_id"] == 30
    db.close()
