import asyncio
import logging
import re
import time
from itertools import batched

import aiohttp

CONTACT_RANKS = frozenset({"commander", "executive_officer", "recruitment_officer"})
log = logging.getLogger(__name__)
UNKNOWN = object()
BATCH_SIZE = 100


class AccountLookup:
    """Operation-local WG snapshot; absent API data must remain unknown."""

    def __init__(self):
        self.searches = {}
        self.nicknames = {}
        self.clans = {}

    @staticmethod
    def known(value):
        if value is UNKNOWN:
            raise ValueError("wg_error")
        return value

    async def find_exact(self, nickname):
        if not valid_nickname(nickname):
            return None
        return self.known(self.searches.get(nickname.casefold(), UNKNOWN))

    async def nickname(self, account_id):
        return self.known(self.nicknames.get(account_id, UNKNOWN))

    async def clan_membership(self, account_id):
        return self.known(self.clans.get(account_id, UNKNOWN))


def valid_nickname(nickname):
    return isinstance(nickname, str) and re.fullmatch(r"[A-Za-z0-9_]{3,24}", nickname)


class RateLimited(Exception):
    pass


class Wargaming:
    def __init__(self, session, app_id, region):
        self.session, self.app_id, self.region = session, app_id, region
        self.base = f"https://api.worldoftanks.{region}/wot"
        # Shared by verification, commands, imports and periodic synchronization.
        self._rate_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._next_call = 0.0

    async def request(self, endpoint, *, data_type=dict, **params):
        for attempt in range(3):
            async with self._slots:
                async with self._rate_lock:
                    await asyncio.sleep(max(0, self._next_call - time.monotonic()))
                    self._next_call = time.monotonic() + 0.5
                try:
                    return await self._request_once(endpoint, data_type=data_type, **params)
                except RateLimited:
                    # Pause all callers, rather than retrying in a tight loop.
                    async with self._rate_lock:
                        self._next_call = max(self._next_call, time.monotonic() + 2**attempt)
                    log.warning("WG request throttled: attempt=%s", attempt + 1)
        raise ValueError("wg_error")

    async def _request_once(self, endpoint, *, data_type=dict, **params):
        try:
            async with self.session.post(
                f"{self.base}/{endpoint}/",
                data={"application_id": self.app_id, **params},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if getattr(response, "status", None) == 429:
                    raise RateLimited
                response.raise_for_status()
                payload = await response.json()
            if isinstance(payload, dict) and payload.get("status") == "error":
                error = payload.get("error")
                if isinstance(error, dict):
                    if error.get("message") in {
                        "REQUEST_LIMIT_EXCEEDED",
                        "REQUEST_LIMIT_EXCEEDED_PER_IP",
                        "REQUEST_LIMIT_EXCEEDED_PER_APPLICATION",
                    }:
                        raise RateLimited
                    # Only numeric codes are safe; never log URLs or API values.
                    code = error.get("code")
                    log.warning(
                        "WG API rejected request: code=%s", code if type(code) is int else "unknown"
                    )
            if (
                not isinstance(payload, dict)
                or payload.get("status") != "ok"
                or not isinstance(payload.get("data"), data_type)
            ):
                raise ValueError
            return payload["data"]
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            # Never include the URL, response body or access token in exceptions/logs.
            log.warning("WG request failed: %s", type(exc).__name__)
            raise ValueError("wg_error") from None

    async def find_exact(self, nickname):
        """Look up one WoT account by its exact nickname, without guessing."""
        if not valid_nickname(nickname):
            return None
        data = await self.request(
            "account/list", data_type=list, search=nickname, type="exact", limit=100
        )
        matches = []
        for entry in data:
            if not isinstance(entry, dict):
                raise ValueError("wg_error")  # noqa: TRY004 - normalized API failure
            name, account_id = entry.get("nickname"), entry.get("account_id")
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 24
                or type(account_id) is not int
                or not 0 < account_id < 2**63
            ):
                raise ValueError("wg_error")
            if name.casefold() == nickname.casefold():
                matches.append((account_id, name))
        if len(matches) > 1:
            raise ValueError("wg_error")
        return matches[0] if matches else None

    async def nickname(self, account_id):
        data = await self.request("account/info", account_id=str(account_id), fields="nickname")
        return self.parse_nickname(data, account_id)

    @staticmethod
    def parse_nickname(data, account_id):
        account = data.get(str(account_id))
        nickname = account.get("nickname") if isinstance(account, dict) else None
        if not isinstance(nickname, str) or not 1 <= len(nickname) <= 32:
            raise ValueError("wg_error")
        return nickname

    async def clan_membership(self, account_id):
        data = await self.request(
            "clans/accountinfo", account_id=str(account_id), fields="clan.clan_id,clan.tag,role"
        )
        return self.parse_clan(data, account_id)

    @staticmethod
    def parse_clan(data, account_id):
        if str(account_id) not in data:
            raise ValueError("wg_error")
        entry = data[str(account_id)]
        if entry is None:
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str):
            raise ValueError("wg_error")  # noqa: TRY004 - normalized external API failure
        clan = entry.get("clan")
        if not isinstance(clan, dict):
            raise ValueError("wg_error")  # noqa: TRY004 - normalized external API failure
        clan_id = clan.get("clan_id")
        if type(clan_id) is not int or not 0 < clan_id < 2**63:
            raise ValueError("wg_error")
        tag = clan.get("tag")
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_-]{2,5}", tag):
            raise ValueError("wg_error")
        return clan_id, entry["role"], tag

    async def lookup(self, names, account_ids, *, clan_ids=()):
        """Group public lookups, with the same shared request limiter as single calls.

        https://developers.wargaming.net/reference/all/wot/account/list/
        https://developers.wargaming.net/reference/all/wot/clans/accountinfo/
        Each parameter list is limited to 100. Nothing is cached across operations.
        """
        result = AccountLookup()
        names = list(dict.fromkeys(name.casefold() for name in names if valid_nickname(name)))
        linked_ids = list(dict.fromkeys(account_ids))
        for group in batched(names, BATCH_SIZE):
            # A rejected/malformed batch is unavailable, never a negative search.
            result.searches.update(dict.fromkeys(group, UNKNOWN))
            try:
                data = await self.request(
                    "account/list", data_type=list, search=",".join(group), type="exact", limit=100
                )
                matches = dict.fromkeys(group)
                for entry in data:
                    if not isinstance(entry, dict):
                        raise ValueError("wg_error")  # noqa: TRY004
                    name, account_id = entry.get("nickname"), entry.get("account_id")
                    if (
                        not valid_nickname(name)
                        or type(account_id) is not int
                        or not 0 < account_id < 2**63
                    ):
                        raise ValueError("wg_error")
                    key = name.casefold()
                    if key in matches:
                        matches[key] = (account_id, name) if matches[key] is None else UNKNOWN
                result.searches.update(matches)
            except ValueError:
                pass
        for group in batched(linked_ids, BATCH_SIZE):
            try:
                data = await self.request(
                    "account/info", account_id=",".join(map(str, group)), fields="nickname"
                )
            except ValueError:
                continue
            for account_id in group:
                try:
                    result.nicknames[account_id] = self.parse_nickname(data, account_id)
                except ValueError:
                    pass
        ids = list(
            dict.fromkeys(
                [
                    *linked_ids,
                    *clan_ids,
                    *(match[0] for match in result.searches.values() if isinstance(match, tuple)),
                ]
            )
        )
        for group in batched(ids, BATCH_SIZE):
            try:
                data = await self.request(
                    "clans/accountinfo",
                    account_id=",".join(map(str, group)),
                    fields="clan.clan_id,clan.tag,role",
                )
            except ValueError:
                continue
            for account_id in group:
                try:
                    result.clans[account_id] = self.parse_clan(data, account_id)
                except ValueError:
                    pass
        return result
