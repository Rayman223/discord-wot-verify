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
    "rank",
    ["commander", "executive_officer", "personnel_officer", "recruitment_officer", "private"],
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


@pytest.mark.parametrize(
    "data",
    [
        [None],
        [{"account_id": 0, "nickname": "Player"}],
        [{"account_id": 42, "nickname": "Player"}, {"account_id": 43, "nickname": "Player"}],
    ],
)
async def test_exact_search_does_not_accept_malformed_or_ambiguous_results(data):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="wg_error"):
        await wg.find_exact("Player")


@pytest.mark.parametrize(
    "data,expected_type,ok",
    [([], list, True), ({}, dict, True), ({}, list, False), ([], dict, False), (None, list, False)],
)
async def test_request_validates_endpoint_response_shape(data, expected_type, ok):
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {"status": "ok", "data": data}

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    wg = Wargaming(Session(), "app", "eu")
    if ok:
        assert await wg.request("account/list", data_type=expected_type) == data
    else:
        with pytest.raises(ValueError, match="wg_error"):
            await wg.request("account/list", data_type=expected_type)
