import hashlib
import hmac
import logging
from html import escape
from importlib.resources import files

import discord
from aiohttp import web

from .i18n import t

log = logging.getLogger(__name__)


def page(key, status=200, *, guild_id, channel_id=0, locale="en"):
    def translate(key):
        return t(key, locale)

    destination = f"https://discord.com/channels/{int(guild_id)}"
    if channel_id:
        destination += f"/{int(channel_id)}"
    title_key = (
        "page_success"
        if key == "success"
        else "page_partial"
        if key in {"linked_incomplete", "nickname_blocked", "nickname_error"}
        else "page_error"
    )
    return web.Response(
        text=(
            f"<!doctype html><html lang='{escape(locale, quote=True)}'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width'>"
            f"<title>{escape(translate(title_key))} · WoT Verify</title>"
            "<link rel='stylesheet' href='/assets/return.css'></head><body>"
            f"<main><div class='brand'>WoT Verify</div><h1>{escape(translate(title_key))}</h1>"
            f"<p>{escape(translate(key))}</p><a class='button' href='{destination}' rel='noreferrer'>"
            f"{escape(translate('return_discord'))}</a><p class='hint'>{escape(translate('return_hint'))}</p>"
            "</main></body></html>"
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
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    app = web.Application(middlewares=[headers], client_max_size=4096)

    def render(key, status=200, channel_id=0):
        return page(
            key,
            status,
            guild_id=bot.config.guild_id,
            channel_id=channel_id,
            locale=getattr(bot.config, "locale", "en"),
        )

    async def login(request):
        if not bot.is_ready():
            return render("not_ready", 503)
        state = request.query.get("state", "")
        # Validate the MAC before issuing redirects; consumption happens only at callback.
        try:
            bot.states.inspect(state)
        except ValueError:
            return render("invalid_state", 400)
        response = web.HTTPFound(bot.openid.login_url(state))
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
            return render("not_ready", 503)
        state = request.query.get("state", "")
        cookie = request.cookies.get("wot_verification", "")
        if len(state) > 256 or not hmac.compare_digest(
            cookie, hashlib.sha256(state.encode()).hexdigest()
        ):
            return render("invalid_state", 400)
        channel_id = 0
        try:
            async with bot.lock:
                discord_id, channel_id = bot.states.consume_details(state)
                account_id = await bot.openid.verify(request.query, state)
                nickname = await bot.wg.nickname(account_id)
                # Callback account_id/nickname are deliberately ignored.
                await bot.member(discord_id)
                bot.store.link(discord_id, bot.config.region, account_id, nickname)
                try:
                    await bot.sync(bot.store.get(discord_id))
                except (discord.HTTPException, ValueError) as exc:
                    log.warning("Discord verification update failed for member %s", discord_id)
                    key = str(exc) if isinstance(exc, ValueError) else "linked_incomplete"
                    if key not in {"nickname_blocked", "nickname_error"}:
                        key = "linked_incomplete"
                    response = render(key, 409, channel_id)
                else:
                    response = render("success", channel_id=channel_id)
        except ValueError as exc:
            response = render(str(exc), 400, channel_id)
        except discord.HTTPException:
            log.warning("Discord member lookup failed during verification")
            response = render("discord_error", 409, channel_id)
        response.del_cookie("wot_verification", path="/callback")
        return response

    async def health(request):
        return web.json_response({"ready": bot.is_ready()}, status=200 if bot.is_ready() else 503)

    async def stylesheet(request):
        return web.Response(
            text=files("wot_verify").joinpath("static/return.css").read_text(),
            content_type="text/css",
        )

    app.router.add_get("/login", login)
    app.router.add_get("/callback", callback)
    app.router.add_get("/healthz", health)
    app.router.add_get("/assets/return.css", stylesheet)
    return app
