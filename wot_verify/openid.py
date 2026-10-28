import asyncio
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import aiohttp

NS = "http://specs.openid.net/auth/2.0"
# Wargaming's published XRDS provider; never take a verification URL from the callback.
ENDPOINT = "https://wargaming.net/id/openid/"
REQUIRED = {"op_endpoint", "return_to", "response_nonce", "assoc_handle", "claimed_id", "identity"}
IDENTITY = re.compile(r"https?://(?:(eu|na|asia)\.)?wargaming\.net/id/([0-9]+)(?:-[^/?#]+)?/?")


class OpenID:
    def __init__(self, session, public_url, region, store):
        self.session, self.public_url, self.region, self.store = session, public_url, region, store

    def callback_url(self, state):
        return f"{self.public_url}/callback?{urlencode({'state': state})}"

    def login_url(self, state):
        return (
            ENDPOINT
            + "?"
            + urlencode(
                {
                    "openid.ns": NS,
                    "openid.mode": "checkid_setup",
                    "openid.return_to": self.callback_url(state),
                    "openid.realm": self.public_url + "/",
                    "openid.claimed_id": NS + "/identifier_select",
                    "openid.identity": NS + "/identifier_select",
                    "game_realm": "na" if self.region == "com" else self.region,
                }
            )
        )

    async def verify(self, params, state):
        try:
            if any(len(params.getall(k)) != 1 for k in params):
                raise ValueError
            if (
                params.get("openid.ns") != NS
                or params.get("openid.mode") != "id_res"
                or params.get("openid.op_endpoint") != ENDPOINT
                or params.get("openid.return_to") != self.callback_url(state)
            ):
                raise ValueError
            signed = set(params.get("openid.signed", "").split(","))
            if not REQUIRED <= signed or any(not params.get("openid." + k) for k in REQUIRED):
                raise ValueError
            claimed = params.get("openid.claimed_id", "")
            identity = IDENTITY.fullmatch(claimed)
            if (
                not identity
                or claimed != params.get("openid.identity")
                or not params.get("openid.sig")
            ):
                raise ValueError
            realm, account = identity.groups()
            if realm and {"na": "com"}.get(realm, realm) != self.region:
                raise ValueError
            account_id = int(account)
            if not 0 < account_id < 2**63:
                raise ValueError
            nonce = params["openid.response_nonce"]
            if (
                not 21 <= len(nonce) <= 255
                or abs(
                    datetime.strptime(nonce[:20], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=UTC)
                    .timestamp()
                    - time.time()
                )
                > 300
            ):
                raise ValueError
            data = {k: v for k, v in params.items() if k.startswith("openid.")}
            data["openid.mode"] = "check_authentication"
            async with self.session.post(
                ENDPOINT, data=data, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                if response.status != 200:
                    raise ValueError
                try:
                    body = await response.content.readexactly(4097)
                except asyncio.IncompleteReadError as exc:
                    body = exc.partial
                if len(body) > 4096:
                    raise ValueError
            fields = {}
            for line in body.decode("utf-8").splitlines():
                key, value = line.split(":", 1)
                if key in fields:
                    raise ValueError
                fields[key] = value
            if fields.get("ns") != NS or fields.get("is_valid") != "true":
                raise ValueError
            self.store.use_openid_nonce(nonce)
            return account_id
        except (ValueError, KeyError, TypeError, aiohttp.ClientError, TimeoutError):
            raise ValueError("wg_error") from None
