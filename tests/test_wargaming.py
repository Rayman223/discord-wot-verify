from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


async def test_exact_account_search():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=[{"account_id": 42, "nickname": "Player"}])
    assert await wg.find_exact("player") == (42, "Player")
    wg.request.assert_awaited_once_with(
        "account/list", data_type=list, search="player", type="exact", limit=100
    )
    wg.request.return_value = []
    assert await wg.find_exact("missing") is None
    wg.request.return_value = [{"account_id": True, "nickname": "Player"}]
    with pytest.raises(ValueError, match="wg_error"):
        await wg.find_exact("Player")


async def test_exact_account_search_rejects_invalid_names_without_request():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock()
    assert await wg.find_exact("Player [TAG]") is None
    wg.request.assert_not_awaited()


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
    wg.request = AsyncMock(
        return_value={"10": {"clan": {"clan_id": 42, "tag": "TAG"}, "role": rank}}
    )
    assert await wg.clan_membership(10) == (42, rank, "TAG")
    assert wg.request.await_args.kwargs["fields"] == "clan.clan_id,clan.tag,role"


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
