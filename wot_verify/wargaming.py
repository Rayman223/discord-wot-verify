import aiohttp


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
