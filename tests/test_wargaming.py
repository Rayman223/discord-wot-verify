from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


@pytest.mark.parametrize("account", [None, {}, {"nickname": ""}, {"nickname": "x" * 33}])
async def test_missing_account_or_invalid_nickname(account):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={"10": account})
    with pytest.raises(ValueError, match="wg_error"):
        await wg.nickname(10)


@pytest.mark.parametrize(
    "rank", ["commander", "executive_officer", "recruitment_officer", "private"]
)
async def test_clan_membership(rank):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={"10": {"clan": {"clan_id": 42}, "role": rank}})
    assert await wg.clan_membership(10) == (42, rank)
    assert wg.request.await_args.kwargs["fields"] == "clan.clan_id,role"


async def test_player_without_clan():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={"10": None})
    assert await wg.clan_membership(10) is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"10": {}},
        {"10": {"clan": None, "role": "commander"}},
        {"10": {"clan": {"clan_id": True}, "role": "commander"}},
    ],
)
async def test_malformed_clan_response_fails_closed(data):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="wg_error"):
        await wg.clan_membership(10)
