import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from wot_verify.security import States
from wot_verify.store import Store
from wot_verify.web import application


@pytest.fixture
async def env(tmp_path):
    store = Store(str(tmp_path / "test.sqlite3"))
    bot = SimpleNamespace(
        config=SimpleNamespace(region="eu", guild_id=1),
        store=store,
        states=States("x" * 32, 1, store),
        is_ready=lambda: True,
        lock=asyncio.Lock(),
        member=AsyncMock(),
        wg=SimpleNamespace(nickname=AsyncMock(return_value="RealName")),
        openid=SimpleNamespace(verify=AsyncMock(return_value=10)),
        sync=AsyncMock(),
    )
    async with TestClient(TestServer(application(bot))) as client:
        yield bot, client
    store.close()


async def invoke(bot, client, token=None, **extra):
    state = token or bot.states.issue(7)
    return await client.get(
        "/callback",
        params={"state": state, "status": "ok", "access_token": "secret", **extra},
        cookies={"wot_verification": hashlib.sha256(state.encode()).hexdigest()},
    )


async def test_callback_uses_verified_identity_and_rejects_replay(env):
    bot, client = env
    token = bot.states.issue(7)
    response = await invoke(bot, client, token, account_id="999", nickname="Forged")
    assert response.status == 200
    assert bot.store.get(7)["account_id"] == 10
    assert bot.store.get(7)["nickname"] == "RealName"
    assert response.headers["Cache-Control"] == "no-store"
    assert "secret" not in await response.text()
    assert (await invoke(bot, client, token)).status == 400
    assert bot.openid.verify.await_count == 1


async def test_bad_assertion_and_missing_browser_cookie(env):
    bot, client = env
    bot.openid.verify.side_effect = ValueError("wg_error")
    assert (await invoke(bot, client)).status == 400
    assert bot.store.get(7) is None
    response = await client.get("/callback", params={"state": bot.states.issue(7)})
    assert response.status == 400


async def test_discord_failure_preserves_link_for_retry(env):
    bot, client = env
    bot.sync.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
    assert (await invoke(bot, client)).status == 409
    assert bot.store.get(7)["account_id"] == 10


async def test_concurrent_duplicate_callbacks(env):
    bot, client = env
    token = bot.states.issue(7)
    results = await asyncio.gather(invoke(bot, client, token), invoke(bot, client, token))
    assert sorted(r.status for r in results) == [200, 400]
    assert bot.openid.verify.await_count == 1


async def test_return_targets_signed_channel_and_never_forwards_token(env):
    bot, client = env
    response = await invoke(
        bot, client, bot.states.issue(7, 42), next="https://attacker.example", channel_id="999"
    )
    body = await response.text()
    assert "href='https://discord.com/channels/1/42'" in body
    assert "Return to Discord" in body
    assert "attacker.example" not in body
    assert "secret" not in body
    assert "style-src 'self'" in response.headers["Content-Security-Policy"]
    css = await client.get("/assets/return.css")
    assert css.status == 200
    assert css.content_type == "text/css"


async def test_partial_success_explains_hierarchy_and_clears_cookie(env):
    bot, client = env
    bot.sync.side_effect = ValueError("nickname_blocked")
    response = await invoke(bot, client, bot.states.issue(7, 42))
    body = await response.text()
    assert response.status == 409
    assert "Account linked" in body
    assert "your player role is in place" in body
    assert "no role hierarchy change is required" in body
    assert "https://discord.com/channels/1/42" in body
    assert response.cookies["wot_verification"]["max-age"] == "0"


async def test_invalid_state_returns_to_server_without_trusting_channel(env):
    bot, client = env
    token = bot.states.issue(7, 42).replace(".42.", ".999.")
    response = await invoke(bot, client, token)
    body = await response.text()
    assert response.status == 400
    assert "href='https://discord.com/channels/1'" in body
    assert "channels/1/999" not in body


@pytest.mark.parametrize("key", ["success", "nickname_blocked", "invalid_state"])
async def test_french_return_pages(env, key):
    from wot_verify.i18n import t

    bot, client = env
    bot.config.locale = "fr"
    if key == "nickname_blocked":
        bot.sync.side_effect = ValueError(key)
    if key == "invalid_state":
        response = await client.get("/callback")
    else:
        response = await invoke(bot, client, bot.states.issue(7, 42))
    body = await response.text()
    assert "lang='fr'" in body
    assert t(key, "fr") in body
    assert "Retourner sur Discord" in body
    assert "https://discord.com/channels/1" in body
