from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


async def test_lookup_groups_exact_names_and_deduplicates_accounts():
    wg = Wargaming(None, "app", "eu")

    async def request(endpoint, **params):
        if endpoint == "account/list":
            assert params["search"] == "player,missing"
            assert params["type"] == "exact"
            return [{"account_id": 10, "nickname": "Player"}]
        if endpoint == "account/info":
            assert params["account_id"] == "20"
            return {"20": {"nickname": "Linked"}}
        assert endpoint == "clans/accountinfo"
        assert set(params["account_id"].split(",")) == {"10", "20", "30"}
        return {
            "10": None,
            "20": {"clan": {"clan_id": 42, "tag": "TAG"}, "role": "commander"},
            "30": None,
        }

    wg.request = AsyncMock(side_effect=request)
    lookup = await wg.lookup(["Player", "PLAYER", "missing", "Bad [TAG]"], [20, 20], clan_ids=[30])
    assert wg.request.await_count == 3
    assert await lookup.find_exact("PLAYER") == (10, "Player")
    assert await lookup.find_exact("missing") is None
    assert await lookup.find_exact("Bad [TAG]") is None
    assert await lookup.nickname(20) == "Linked"
    assert await lookup.clan_membership(10) is None
    assert await lookup.clan_membership(20) == (42, "commander", "TAG")
    assert await lookup.clan_membership(30) is None


async def test_batches_never_exceed_100_and_missing_data_remains_unknown():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={})
    lookup = await wg.lookup([], range(1, 202))
    assert wg.request.await_count == 6
    for call in wg.request.await_args_list:
        assert 1 <= len(call.kwargs["account_id"].split(",")) <= 100
    for method in (lookup.nickname, lookup.clan_membership):
        with pytest.raises(ValueError, match="wg_error"):
            await method(1)
    with pytest.raises(ValueError, match="wg_error"):
        await lookup.find_exact("not_requested")


async def test_failed_name_batch_does_not_block_next_batch_or_become_no_match():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(side_effect=[ValueError("wg_error"), []])
    names = [f"Player_{i}" for i in range(101)]
    lookup = await wg.lookup(names, [])
    assert wg.request.await_count == 2
    with pytest.raises(ValueError, match="wg_error"):
        await lookup.find_exact(names[0])
    assert await lookup.find_exact(names[-1]) is None


@pytest.mark.parametrize("error", ["missing", "malformed", "rejected"])
async def test_clan_errors_are_isolated_per_account_and_never_mean_no_clan(error):
    wg = Wargaming(None, "app", "eu")
    clan_data = {"10": None}
    if error == "malformed":
        clan_data["20"] = {"clan": {}, "role": "commander"}
    wg.request = AsyncMock(
        side_effect=[
            {"10": {"nickname": "First"}, "20": {"nickname": "Second"}},
            ValueError("wg_error") if error == "rejected" else clan_data,
        ]
    )
    lookup = await wg.lookup([], [10, 20])
    assert await lookup.nickname(20) == "Second"
    if error != "rejected":
        assert await lookup.clan_membership(10) is None
    with pytest.raises(ValueError, match="wg_error"):
        await lookup.clan_membership(20)


@pytest.mark.parametrize(
    "entries",
    [
        [None],
        [{"nickname": "Player", "account_id": True}],
        [{"nickname": "Player", "account_id": 10}, {"nickname": "PLAYER", "account_id": 20}],
    ],
)
async def test_malformed_or_ambiguous_search_is_unknown(entries):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=entries)
    lookup = await wg.lookup(["Player"], [])
    with pytest.raises(ValueError, match="wg_error"):
        await lookup.find_exact("Player")
    assert wg.request.await_count == 1


async def test_second_lookup_fetches_new_data_instead_of_reusing_scan_cache():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        side_effect=[
            [{"nickname": "Player", "account_id": 10}],
            {"10": None},
            [{"nickname": "Player", "account_id": 20}],
            {"20": None},
        ]
    )
    first = await wg.lookup(["Player"], [])
    second = await wg.lookup(["Player"], [])
    assert (await first.find_exact("Player"))[0] == 10
    assert (await second.find_exact("Player"))[0] == 20
