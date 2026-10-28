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
        config=SimpleNamespace(region="eu"),
        store=store,
        states=States("x" * 32, 1, store),
        is_ready=lambda: True,
        lock=asyncio.Lock(),
        member=AsyncMock(),
        wg=SimpleNamespace(verify=AsyncMock(return_value=(10, "RealName"))),
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
    assert bot.wg.verify.await_count == 1


async def test_bad_token_and_missing_browser_cookie(env):
    bot, client = env
    bot.wg.verify.side_effect = ValueError("wg_error")
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
    assert bot.wg.verify.await_count == 1
