import asyncio
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


async def test_all_endpoints_share_rate_limit_and_cancelled_wait_releases_slot():
    wg = Wargaming(None, "PRIVATE_APP", "eu")
    starts = []

    async def request(*args, **kwargs):
        starts.append(asyncio.get_running_loop().time())
        return {}

    wg._request_once = AsyncMock(side_effect=request)
    await wg.request("account/info")
    waiting = asyncio.create_task(wg.request("clans/accountinfo"))
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.gather(wg.request("account/list"), wg.request("clans/accountinfo"))
    assert len(starts) == 3
    assert all(b - a >= 0.48 for a, b in pairwise(starts))


@pytest.mark.parametrize("http", [False, True])
async def test_throttle_retries_with_shared_pause_and_safe_logs(http, caplog):
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            self.status = 429 if http and len(calls) == 1 else 200
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            if len(calls) == 1:
                return {
                    "status": "error",
                    "error": {
                        "code": 407,
                        "message": "REQUEST_LIMIT_EXCEEDED",
                        "value": "PRIVATE_APP",
                    },
                }
            return {"status": "ok", "data": {}}

    class Session:
        def post(self, *args, **kwargs):
            calls.append(asyncio.get_running_loop().time())
            return Response()

    wg = Wargaming(Session(), "PRIVATE_APP", "eu")
    assert await wg.request("account/info") == {}
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.98
    assert "throttled" in caplog.text
    assert "PRIVATE_APP" not in caplog.text
