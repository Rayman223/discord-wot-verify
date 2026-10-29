import hashlib
import hmac
import logging
import time
from html import escape
from importlib.resources import files

import aiohttp
import discord
from aiohttp import web

from .i18n import t

log = logging.getLogger(__name__)


def page(key, status=200, *, guild_id, channel_id=0, locale="en", retry_token=""):
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
    form = ""
    if retry_token:
        form = (
            "<form method='post' action='/resync'>"
            f"<input type='hidden' name='csrf' value='{escape(retry_token, quote=True)}'>"
            f"<button class='button' type='submit'>{escape(translate('retry_button'))}</button>"
            f"<p class='hint'>{escape(translate('retry_hint'))}</p></form>"
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
            f"{form}</main></body></html>"
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
                    "default-src 'none'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    app = web.Application(middlewares=[headers], client_max_size=4096)

    def render(key, status=200, channel_id=0, retry_token=""):
        return page(
            key,
            status,
            guild_id=bot.config.guild_id,
            channel_id=channel_id,
            locale=getattr(bot.config, "locale", "en"),
            retry_token=retry_token,
        )

    def partial(key, discord_id, channel_id, *, previous=None):
        attempts = previous["attempts"] + 1 if previous is not None else 0
        expires_at = previous["expires_at"] if previous is not None else int(time.time()) + 600
        row = bot.store.get(discord_id)
        if row is None or attempts >= 3 or expires_at <= int(time.time()):
            return render("retry_exhausted", 409, channel_id)
        cookie, csrf, lifetime = bot.store.issue_retry(
            discord_id,
            row["region"],
            row["account_id"],
            channel_id,
            expires_at=expires_at,
            attempts=attempts,
        )
        response = render(key, 409, channel_id, csrf)
        response.set_cookie(
            "wot_retry",
            cookie,
            max_age=lifetime,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/resync",
        )
        return response

    async def retry(request):
        if request.headers.get("Origin") != bot.config.public_url:
            return render("retry_expired", 403)
        if request.content_type != "application/x-www-form-urlencoded":
            return render("retry_expired", 400)
        form = await request.post()
        if len(form.getall("csrf", [])) != 1:
            return render("retry_expired", 400)
        csrf = form.get("csrf", "")
        cookie = request.cookies.get("wot_retry", "")
        if not bot.is_ready():
            return render("not_ready", 503)
        async with bot.lock:
            try:
                session = bot.store.consume_retry(cookie, csrf)
            except ValueError as exc:
                # Too-early retries keep the still-valid form and browser cookie.
                if str(exc) == "retry_wait":
                    return render("retry_wait", 429, retry_token=csrf)
                return render("retry_expired", 400)
            row = bot.store.get(session["discord_id"])
            if (
                row is None
                or row["region"] != bot.config.region
                or (row["region"], row["account_id"])
                != (
                    session["region"],
                    session["account_id"],
                )
            ):
                response = render("retry_expired", 400)
            else:
                try:
                    await bot.sync(row)
                except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError) as exc:
                    key = str(exc) if isinstance(exc, ValueError) else "linked_incomplete"
                    if key not in {"nickname_blocked", "nickname_error"}:
                        key = "linked_incomplete"
                    response = partial(
                        key, session["discord_id"], session["channel_id"], previous=session
                    )
                else:
                    response = render("success", channel_id=session["channel_id"])
            if "wot_retry" not in response.cookies:
                response.del_cookie("wot_retry", path="/resync")
            return response

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
                new_link = bot.store.get(discord_id) is None
                bot.store.link(discord_id, bot.config.region, account_id, nickname)
                if new_link:
                    await bot.audit.emit("linked", discord_id)
                try:
                    await bot.sync(bot.store.get(discord_id))
                except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError) as exc:
                    log.warning("Discord verification update failed for member %s", discord_id)
                    key = str(exc) if isinstance(exc, ValueError) else "linked_incomplete"
                    if key not in {"nickname_blocked", "nickname_error"}:
                        key = "linked_incomplete"
                    response = partial(key, discord_id, channel_id)
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
    app.router.add_post("/resync", retry)
    app.router.add_get("/healthz", health)
    app.router.add_get("/assets/return.css", stylesheet)
    return app
