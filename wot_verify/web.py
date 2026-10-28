import hashlib
import hmac
import logging
from html import escape

import discord
from aiohttp import web

from .i18n import t

log = logging.getLogger(__name__)


def page(key, status=200):
    return web.Response(
        text=(
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width'>"
            f"<title>WoT Verify</title><main><h1>WoT Verify</h1><p>{escape(t(key))}"
            "</p></main></html>"
        ),
        content_type="text/html",
        status=status,
    )


def application(bot):
    @web.middleware
    async def headers(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    app = web.Application(middlewares=[headers], client_max_size=4096)

    async def login(request):
        if not bot.is_ready():
            return page("not_ready", 503)
        state = request.query.get("state", "")
        # Validate the MAC before issuing redirects; consumption happens only at callback.
        try:
            payload, signature = state.rsplit(".", 1)
            if len(state) > 256 or not hmac.compare_digest(
                signature, bot.states.signature(payload)
            ):
                raise ValueError
        except ValueError:
            return page("invalid_state", 400)
        from urllib.parse import urlencode

        callback_url = f"{bot.config.public_url}/callback?{urlencode({'state': state})}"
        response = web.HTTPFound(bot.wg.login_url(callback_url))
        response.set_cookie(
            "wot_verification",
            hashlib.sha256(state.encode()).hexdigest(),
            max_age=600,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/callback",
        )
        return response

    async def callback(request):
        if not bot.is_ready():
            return page("not_ready", 503)
        state = request.query.get("state", "")
        cookie = request.cookies.get("wot_verification", "")
        if len(state) > 256 or not hmac.compare_digest(
            cookie, hashlib.sha256(state.encode()).hexdigest()
        ):
            return page("invalid_state", 400)
        try:
            async with bot.lock:
                discord_id = bot.states.consume(state)
                token = request.query.get("access_token", "")
                if request.query.get("status") != "ok" or not token or len(token) > 2048:
                    raise ValueError("wg_error")
                account_id, nickname = await bot.wg.verify(token)
                # Callback account_id/nickname are deliberately ignored.
                await bot.member(discord_id)
                bot.store.link(discord_id, bot.config.region, account_id, nickname)
                try:
                    await bot.sync(bot.store.get(discord_id))
                except (discord.HTTPException, ValueError):
                    log.warning("Discord verification update failed for member %s", discord_id)
                    return page("discord_error", 409)
            response = page("success")
        except ValueError as exc:
            response = page(str(exc), 400)
        except discord.HTTPException:
            log.warning("Discord member lookup failed during verification")
            response = page("discord_error", 409)
        response.del_cookie("wot_verification", path="/callback")
        return response

    async def health(request):
        return web.json_response({"ready": bot.is_ready()}, status=200 if bot.is_ready() else 503)

    app.router.add_get("/login", login)
    app.router.add_get("/callback", callback)
    app.router.add_get("/healthz", health)
    return app
