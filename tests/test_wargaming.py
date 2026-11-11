import time
from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import PlayerProfile, Wargaming, public_timestamp


@pytest.mark.parametrize("value", [None, 0, -1, True, "123", 1.5, 10**100])
def test_public_profile_timestamp_rejects_invalid_values(value):
    assert public_timestamp(value) is None


def test_public_profile_timestamp_rejects_future_and_preserves_utc_seconds():
    assert public_timestamp(int(time.time()) + 3600) is None
    assert public_timestamp(1308919860) == 1308919860


async def test_public_player_profile_selects_only_public_fields():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "10": {
                "nickname": "Player",
                "created_at": 1308919860,
                "last_battle_time": 1700000000,
                "logout_at": 1700001000,
                "private": {"gold": 1000},
            }
        }
    )
    assert await wg.player_profile(10) == PlayerProfile(
        "Player", 1308919860, 1700000000, 1700001000
    )
    wg.request.assert_awaited_once_with(
        "account/info", account_id="10", fields="nickname,created_at,last_battle_time,logout_at"
    )


@pytest.mark.parametrize(
    "data", [{}, {"10": None}, {"10": []}, {"10": {}}, {"10": {"nickname": "x" * 33}}]
)
async def test_public_profile_rejects_missing_or_malformed_account(data):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="wg_error"):
        await wg.player_profile(10)


@pytest.mark.parametrize("date", [None, 0, -1, True, "2026-01-01", {}, 10**100])
async def test_public_profile_invalid_dates_are_independently_unavailable(date):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "10": {
                "nickname": "Player",
                "created_at": date,
                "last_battle_time": date,
                "logout_at": date,
            }
        }
    )
    assert await wg.player_profile(10) == PlayerProfile("Player", None, None, None)


@pytest.mark.parametrize(
    "locale,rank", [("fr", "Commandant en second"), ("en", "Executive Officer")]
)
async def test_profile_membership_has_localized_rank(locale, rank):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "10": {
                "clan": {"clan_id": 42, "tag": "TAG"},
                "role": "executive_officer",
                "role_i18n": rank,
            }
        }
    )
    membership = await wg.profile_membership(10, locale)
    assert (membership.clan_id, membership.tag, membership.role, membership.rank) == (
        42,
        "TAG",
        "executive_officer",
        rank,
    )
    wg.request.assert_awaited_once_with(
        "clans/accountinfo",
        account_id="10",
        language=locale,
        fields="clan.clan_id,clan.tag,role,role_i18n",
    )
    wg.request.return_value = {"10": None}
    assert await wg.profile_membership(10, locale) is None


@pytest.mark.parametrize("rank", [None, {}, "", "x" * 101])
async def test_invalid_localized_rank_is_unavailable(rank):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "10": {"clan": {"clan_id": 42, "tag": "TAG"}, "role": "new_role", "role_i18n": rank}
        }
    )
    assert (await wg.profile_membership(10, "fr")).rank is None


async def test_clan_public_profile():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "42": {
                "name": "Clan name",
                "created_at": 1308919860,
                "members_count": 99,
                "leader_id": 100,
                "leader_name": "Commander",
                "emblems": {"x195": {"portal": "https://example.org/clan.png"}},
            }
        }
    )
    clan = await wg.clan_profile(42)
    assert clan.name == "Clan name"
    assert clan.members_count == 99
    assert clan.created_at == 1308919860
    assert clan.leader_id == 100
    assert clan.leader_name == "Commander"
    assert clan.emblem == "https://example.org/clan.png"
    wg.request.assert_awaited_once_with(
        "clans/info",
        clan_id="42",
        fields="name,members_count,created_at,leader_id,leader_name,emblems.x195",
    )


@pytest.mark.parametrize(
    "data", [{}, {"42": None}, {"42": []}, {"42": {}}, {"42": {"name": "x" * 101}}]
)
async def test_clan_public_profile_rejects_malformed_data(data):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value=data)
    with pytest.raises(ValueError, match="wg_error"):
        await wg.clan_profile(42)


@pytest.mark.parametrize(
    "emblem",
    [
        None,
        {},
        "http://example.org/logo.png",
        "javascript:alert(1)",
        "https:///logo.png",
        "https://user:pass@example.org/logo.png",
        "https://@example.org/logo.png",
        "https://example.org:invalid/logo.png",
        "https://example.org:99999/logo.png",
        "https://[invalid",
        "https://example.org/has space.png",
    ],
)
async def test_optional_clan_fields_and_invalid_logos_do_not_block_profile(emblem):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "42": {
                "name": "Clan",
                "members_count": True,
                "created_at": 0,
                "leader_id": True,
                "leader_name": "@everyone",
                "emblems": {"x195": {"portal": emblem}},
            }
        }
    )
    clan = await wg.clan_profile(42)
    assert clan.members_count is clan.created_at is clan.leader_id is clan.leader_name is None
    assert clan.emblem is None


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
