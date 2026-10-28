import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from multidict import MultiDict

from wot_verify.openid import ENDPOINT, NS, REQUIRED, OpenID
from wot_verify.security import States
from wot_verify.store import Store
from wot_verify.web import application


@pytest.fixture
def protocol(tmp_path):
    store = Store(str(tmp_path / "openid.sqlite3"))
    response = SimpleNamespace(
        status=200,
        content=SimpleNamespace(
            readexactly=AsyncMock(
                side_effect=asyncio.IncompleteReadError(f"ns:{NS}\nis_valid:true\n".encode(), 4097)
            )
        ),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = SimpleNamespace(post=MagicMock(return_value=context))
    client = OpenID(session, "https://verify.example", "eu", store)
    params = MultiDict(
        {
            "openid.ns": NS,
            "openid.mode": "id_res",
            "openid.op_endpoint": ENDPOINT,
            "openid.return_to": client.callback_url("state"),
            "openid.claimed_id": "https://eu.wargaming.net/id/10-Player/",
            "openid.identity": "https://eu.wargaming.net/id/10-Player/",
            "openid.response_nonce": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "unique",
            "openid.assoc_handle": "handle",
            "openid.sig": "signature+with/slash=",
            "openid.signed": ",".join(sorted(REQUIRED)),
        }
    )
    yield client, params, response
    store.close()


async def test_verified_identity_and_nonce_replay(protocol):
    client, params, _ = protocol
    assert await client.verify(params, "state") == 10
    call = client.session.post.call_args
    assert call.args == (ENDPOINT,)
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["data"]["openid.mode"] == "check_authentication"
    assert call.kwargs["data"]["openid.sig"] == "signature+with/slash="
    with pytest.raises(ValueError):
        await client.verify(params, "state")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("openid.mode", "cancel"),
        ("openid.ns", "other"),
        ("openid.op_endpoint", "https://attacker.example/"),
        ("openid.return_to", "https://verify.example/callback?state=other"),
        ("openid.identity", "https://eu.wargaming.net/id/99/"),
        ("openid.signed", "return_to,response_nonce"),
        ("openid.sig", ""),
        ("openid.claimed_id", "https://attacker.example/id/10/"),
    ],
)
async def test_invalid_assertion_rejected_before_network(protocol, key, value):
    client, params, _ = protocol
    params[key] = value
    with pytest.raises(ValueError, match="wg_error"):
        await client.verify(params, "state")
    client.session.post.assert_not_called()


async def test_wrong_region_and_expired_nonce(protocol):
    client, params, _ = protocol
    params["openid.claimed_id"] = params["openid.identity"] = "https://na.wargaming.net/id/10/"
    with pytest.raises(ValueError):
        await client.verify(params, "state")
    params["openid.claimed_id"] = params["openid.identity"] = "https://wargaming.net/id/10/"
    params["openid.response_nonce"] = (datetime.now(UTC) - timedelta(minutes=6)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ) + "old"
    with pytest.raises(ValueError):
        await client.verify(params, "state")
    client.session.post.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        b"is_valid:false\n",
        b"is_valid:true\n",
        b"x" * 4097,
        f"ns:{NS}\nis_valid:true\nis_valid:false\n".encode(),
    ],
)
async def test_invalid_provider_response(protocol, body):
    client, params, response = protocol
    response.content.readexactly.side_effect = asyncio.IncompleteReadError(body, 4097)
    with pytest.raises(ValueError):
        await client.verify(params, "state")


async def test_duplicates_and_legacy_token_rejected(protocol):
    client, params, _ = protocol
    params.add("openid.identity", params["openid.identity"])
    with pytest.raises(ValueError):
        await client.verify(params, "state")
    with pytest.raises(ValueError):
        await client.verify(MultiDict(status="ok", access_token="legacy"), "state")
    client.session.post.assert_not_called()


def test_login_has_no_token_request(protocol):
    client, _, _ = protocol
    query = parse_qs(urlsplit(client.login_url("state")).query)
    assert query["openid.mode"] == ["checkid_setup"]
    assert query["openid.return_to"] == [client.callback_url("state")]
    assert query["game_realm"] == ["eu"]
    assert "application_id" not in query


def test_nonce_persists_across_restart(protocol):
    client, _, _ = protocol
    path = client.store.db.execute("PRAGMA database_list").fetchone()[2]
    client.store.use_openid_nonce("nonce")
    second = Store(path)
    try:
        with pytest.raises(ValueError):
            second.use_openid_nonce("nonce")
    finally:
        second.close()


async def test_callback_with_real_verifier_and_browser_state(protocol):
    client, params, _ = protocol
    states = States("x" * 32, 1, client.store)
    state = states.issue(7, 42)
    params["state"] = state
    params["openid.return_to"] = client.callback_url(state)
    bot = SimpleNamespace(
        config=SimpleNamespace(region="eu", guild_id=1),
        is_ready=lambda: True,
        store=client.store,
        states=states,
        lock=asyncio.Lock(),
        openid=client,
        member=AsyncMock(),
        wg=SimpleNamespace(nickname=AsyncMock(return_value="Player")),
        sync=AsyncMock(),
    )
    async with TestClient(TestServer(application(bot))) as browser:
        response = await browser.get(
            "/callback",
            params=params,
            cookies={"wot_verification": hashlib.sha256(state.encode()).hexdigest()},
        )
        assert response.status == 200
        assert client.store.get(7)["account_id"] == 10
        assert "https://discord.com/channels/1/42" in await response.text()


async def test_login_redirect_and_legacy_callback(protocol):
    client, _, _ = protocol
    states = States("x" * 32, 1, client.store)
    state = states.issue(7)
    bot = SimpleNamespace(
        config=SimpleNamespace(guild_id=1),
        is_ready=lambda: True,
        store=client.store,
        states=states,
        lock=asyncio.Lock(),
        openid=client,
    )
    async with TestClient(TestServer(application(bot))) as browser:
        response = await browser.get("/login", params={"state": state}, allow_redirects=False)
        assert response.status == 302
        assert response.headers["Location"].startswith(ENDPOINT)
        assert "access_token" not in response.headers["Location"]
        assert response.cookies["wot_verification"]["httponly"]
        response = await browser.get(
            "/callback",
            params={"state": state, "status": "ok", "access_token": "legacy"},
            cookies={"wot_verification": hashlib.sha256(state.encode()).hexdigest()},
        )
        assert response.status == 400
        assert client.store.get(7) is None
