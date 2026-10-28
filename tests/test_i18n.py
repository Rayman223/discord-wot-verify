from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wot_verify.bot import LoginView, VerifyBot
from wot_verify.config import Config
from wot_verify.i18n import CATALOGS, t


def test_french_catalog_covers_all_messages():
    assert CATALOGS["fr"].keys() == CATALOGS["en"].keys()
    assert all(isinstance(value, str) and value.strip() for value in CATALOGS["fr"].values())
    for key in CATALOGS["en"]:
        if key.startswith("command_"):
            assert len(CATALOGS["fr"][key]) <= 100


def test_translation_fallback(monkeypatch):
    monkeypatch.delitem(CATALOGS["fr"], "success")
    assert t("success", "fr") == t("success", "en")
    assert t("success", "unknown") == t("success", "en")
    assert t("unknown", "fr") == t("error", "en")


@pytest.mark.parametrize("locale", [None, "en", "fr", "invalid"])
def test_config_locale(monkeypatch, locale):
    for key, value in {
        "DISCORD_TOKEN": "test",
        "WG_APP_ID": "test",
        "STATE_SECRET": "x" * 32,
        "GUILD_ID": "1",
        "ROLE_ID": "2",
        "PUBLIC_URL": "https://example.org",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LOCALE", raising=False)
    if locale is not None:
        monkeypatch.setenv("LOCALE", locale)
    if locale == "invalid":
        with pytest.raises(ValueError, match="LOCALE"):
            Config.load()
    else:
        assert Config.load().locale == (locale or "en")


async def test_instances_keep_their_language_and_translate_responses(tmp_path):
    bots = [
        VerifyBot(
            SimpleNamespace(
                locale=locale,
                database=str(tmp_path / f"{locale}.sqlite3"),
                state_secret="x" * 32,
                guild_id=1,
                role_id=2,
            )
        )
        for locale in ("fr", "en")
    ]
    try:
        french, english = bots
        for bot in bots:
            view = LoginView(bot)
            assert view.is_persistent()
            assert view.children[0].label == bot.t("login_button")
            assert view.children[1].label == bot.t("clan_button")
            assert bot.tree.get_command("setup").description == bot.t("command_setup")
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock())
        )
        await french.tree.get_command("unlink").callback(interaction)
        interaction.followup.send.assert_awaited_once_with(
            t("confirm_unlink", "fr"), ephemeral=True
        )
        assert french.t("success") != english.t("success")
    finally:
        for bot in bots:
            bot.store.close()
