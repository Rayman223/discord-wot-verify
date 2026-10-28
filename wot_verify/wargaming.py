import re

import aiohttp

CONTACT_RANKS = frozenset({"commander", "executive_officer", "recruitment_officer"})


class Wargaming:
    def __init__(self, session, app_id, region):
        self.session, self.app_id, self.region = session, app_id, region
        self.base = f"https://api.worldoftanks.{region}/wot"

    async def request(self, endpoint, **params):
        try:
            async with self.session.post(
                f"{self.base}/{endpoint}/",
                data={"application_id": self.app_id, **params},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("status") != "ok"
                or not isinstance(payload.get("data"), dict)
            ):
                raise ValueError
            return payload["data"]
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            # Never include the URL, response body or access token in exceptions/logs.
            raise ValueError("wg_error") from None

    async def nickname(self, account_id):
        data = await self.request("account/info", account_id=str(account_id), fields="nickname")
        account = data.get(str(account_id))
        nickname = account.get("nickname") if isinstance(account, dict) else None
        if not isinstance(nickname, str) or not 1 <= len(nickname) <= 32:
            raise ValueError("wg_error")
        return nickname

    async def clan_membership(self, account_id):
        data = await self.request(
            "clans/accountinfo", account_id=str(account_id), fields="clan.clan_id,clan.tag,role"
        )
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
