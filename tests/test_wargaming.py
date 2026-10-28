from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


async def test_identity_comes_from_validated_token():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(side_effect=[{"account_id": 10}, {"10": {"nickname": "RealName"}}])
    assert await wg.verify("token") == (10, "RealName")
    assert wg.request.await_args_list[0].args == ("auth/prolongate",)
    assert wg.request.await_args_list[0].kwargs == {"access_token": "token"}
    assert wg.request.await_args_list[1].kwargs["account_id"] == "10"


@pytest.mark.parametrize("data", [{}, {"account_id": None}, {"account_id": -1}])
async def test_missing_or_invalid_identity_is_rejected(data):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="wg_error"):
        await wg.verify("private-token")


@pytest.mark.parametrize("account", [None, {}, {"nickname": ""}, {"nickname": "x" * 33}])
async def test_missing_account_or_invalid_nickname(account):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={"10": account})
    with pytest.raises(ValueError, match="wg_error"):
        await wg.nickname(10)
