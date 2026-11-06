"""Account anniversaries: Brussels calendar, durable nicknames and safe announcements."""

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from itertools import batched
from zoneinfo import ZoneInfo

import aiohttp
import discord

from .birthday_store import BirthdayStore
from .nicknames import without_birthday_candles
from .wargaming import BATCH_SIZE

log = logging.getLogger(__name__)
BRUSSELS = ZoneInfo("Europe/Brussels")
IO_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError, ValueError)


def birthday_age(created_at, day):
    if created_at is None:
        return 0
    created = datetime.fromtimestamp(created_at, UTC).astimezone(BRUSSELS).date()
    try:
        anniversary = created.replace(year=day.year)
    except ValueError:  # February 29 in a non-leap year.
        anniversary = date(day.year, 3, 1)
    age = day.year - created.year
    return age if age > 0 and day == anniversary else 0


def expires_at(day):
    return int(datetime.combine(day + timedelta(days=1), time(7), BRUSSELS).timestamp())


class Birthdays:
    def __init__(self, bot):
        self.bot = bot
        self.store = BirthdayStore(bot.store.db)
        self._next_check = 0
        self._day = None
        self._warnings = set()
        self._reconciled = set()

    @property
    def enabled(self):
        return bool(getattr(self.bot.config, "birthday_channel_id", 0))

    def warn(self, reason, discord_id, now):
        key = (reason, discord_id, now.astimezone(BRUSSELS).date())
        if key not in self._warnings:
            self._warnings.add(key)
            log.warning("Birthday %s: Discord member %s", reason, discord_id)

    async def collect(self, rows, *, now=None):
        """Best effort only: unknown dates never invalidate a verified identity."""
        now = int((now or datetime.now(UTC)).timestamp())
        rows = self.store.missing(rows, now)
        for group in batched(rows, BATCH_SIZE):
            region = self.bot.config.region
            ids = list(dict.fromkeys(r["account_id"] for r in group if r["region"] == region))
            if not ids:
                continue
            try:
                dates = await self.bot.wg.creation_dates(ids)
            except (aiohttp.ClientError, OSError, ValueError):
                dates = {}
                log.warning("Birthday dates unavailable: region=%s accounts=%s", region, len(ids))
            for account_id in ids:
                self.store.record_date(region, account_id, dates.get(account_id), now)
            if dates:
                self._next_check = 0
            if len(dates) < len(ids):
                log.warning(
                    "Birthday dates unknown: region=%s accounts=%s", region, len(ids) - len(dates)
                )
            await asyncio.sleep(0)

    def prepare_nickname(self, row, base, *, current=None, now=None):
        """Caller has established player eligibility and holds the bot lock.

        Track intent before editing Discord, including a potentially timed-out edit.
        Before 7 AM, only preserve an already managed anniversary from yesterday.
        """
        if not self.enabled:
            return base
        now = now or datetime.now(UTC)
        local = now.astimezone(BRUSSELS)
        day = local.date()
        if local.hour < 7:
            managed = self.store.decoration(self.bot.config.guild_id, row["discord_id"])
            yesterday = day - timedelta(days=1)
            if (
                managed is None
                and current != without_birthday_candles(current)
                and birthday_age(self.store.created_at(row["region"], row["account_id"]), yesterday)
            ):
                self.store.track(self.bot.config.guild_id, row["discord_id"], expires_at(yesterday))
                managed = self.store.decoration(self.bot.config.guild_id, row["discord_id"])
            if managed is None or managed["expires_at"] <= now.timestamp():
                return base
            day -= timedelta(days=1)
        age = birthday_age(self.store.created_at(row["region"], row["account_id"]), day)
        if not age:
            return base
        decorated = f"{base} {'🕯' * age}"
        if len(decorated) > 32:
            self.warn("nickname_too_long", row["discord_id"], now)
            return base
        self.store.track(self.bot.config.guild_id, row["discord_id"], expires_at(day))
        return decorated

    async def cleanup(self, member, *, refresh=False):
        """Explicit role/link withdrawal also removes managed or legacy candles."""
        guild_id = self.bot.config.guild_id
        if not self.enabled and self.store.decoration(guild_id, member.id) is None:
            return
        if refresh:
            # Member.edit returns a new object; a previous tag edit does not update
            # the caller's member snapshot. Never restore that obsolete tag here.
            self.store.track(guild_id, member.id, 0)
            try:
                member = await self.bot.member(member.id)
            except discord.NotFound:
                self.store.forget(guild_id, member.id)
                return
        nickname = member.nick
        base = without_birthday_candles(nickname)
        if base != nickname:
            self.store.track(guild_id, member.id, 0)
            await member.edit(nick=base or None, reason="WoT birthday decoration removed")
        self.store.forget(guild_id, member.id)

    async def tick(self, *, now=None):
        fixed_now = now
        now = now or datetime.now(UTC)
        local = now.astimezone(BRUSSELS)
        guild_id = self.bot.config.guild_id
        # Disabling the feature stops new changes, while persisted cleanup stays recoverable.
        if not self.enabled:
            return
        day = local.date().isoformat() if local.hour >= 7 else None
        if self._next_check <= now.timestamp() or day != self._day:
            self._next_check = now.timestamp() + 900
            self._day = day
            self._warnings = {key for key in self._warnings if key[2] == local.date()}
            linked = {
                row["discord_id"]
                for row in self.bot.store.db.execute("SELECT discord_id FROM links")
            }
            tracked = {row["discord_id"] for row in self.store.decorations(guild_id)}
            celebrants = []
            for discord_id in sorted(linked | tracked):
                async with self.bot.lock:
                    try:
                        result = await self.process_member(discord_id, now)
                        if result:
                            celebrants.append(result)
                    except IO_ERRORS as exc:
                        self.warn(type(exc).__name__, discord_id, now)
                await asyncio.sleep(0)
            if day:
                self.queue(celebrants, local.date(), now)
        await self.deliver(fixed_now)

    async def process_member(self, discord_id, now):
        guild_id = self.bot.config.guild_id
        row = self.bot.store.get(discord_id)
        managed = self.store.decoration(guild_id, discord_id)
        local = now.astimezone(BRUSSELS)
        try:
            member = await self.bot.member(discord_id)
        except discord.NotFound:
            self.store.forget(guild_id, discord_id)
            return None
        player = row is not None and any(r.id == self.bot.config.role_id for r in member.roles)
        if not player:
            if managed or row is not None:
                await self.cleanup(member)
            return None
        if local.hour < 7:
            if managed is not None and managed["expires_at"] > now.timestamp():
                return None
            if (
                managed is None
                and birthday_age(
                    self.store.created_at(row["region"], row["account_id"]),
                    local.date() - timedelta(days=1),
                )
                and without_birthday_candles(member.nick) != member.nick
            ):
                # Adopt yesterday's legacy candles until their normal 7 AM expiry.
                self.store.track(guild_id, discord_id, expires_at(local.date() - timedelta(days=1)))
                return None
            await self.cleanup(member)
            return None
        base = without_birthday_candles(member.nick or member.name)
        target = self.prepare_nickname(row, base, current=member.nick, now=now)
        if target == base and without_birthday_candles(member.nick) != member.nick:
            # Adopt legacy decorations for cleanup; preserve identity and clan tag.
            self.store.track(guild_id, discord_id, 0)
        cleaned = target == (member.nick or member.name)
        if not cleaned:
            try:
                await member.edit(nick=target, reason="WoT account anniversary")
                cleaned = True
            except IO_ERRORS as exc:
                self.warn(type(exc).__name__, discord_id, now)
                # An uneditable nickname must not suppress this player's announcement.
        if target == base and cleaned:
            self.store.forget(guild_id, discord_id)
        if local.hour >= 7:
            age = birthday_age(
                self.store.created_at(row["region"], row["account_id"]), local.date()
            )
            if age:
                return row, age
        return None

    def queue(self, celebrants, day, now):
        guild_id = self.bot.config.guild_id
        heading = self.bot.t("birthday_heading")
        content, rows = heading, []
        for row, age in celebrants:
            if self.store.reserved(guild_id, day.isoformat(), row):
                continue
            line = self.bot.t("birthday_member_one" if age == 1 else "birthday_member").format(
                member=f"<@{row['discord_id']}>", candles="🕯" * age, years=age
            )
            # Keep room below Discord's 2000-character limit, also for astral emoji.
            if rows and (len((content + "\n" + line).encode("utf-16-le")) // 2 > 1900):
                self.store.queue(
                    guild_id,
                    self.bot.config.birthday_channel_id,
                    day.isoformat(),
                    content,
                    rows,
                    int(now.timestamp()),
                )
                content, rows = heading, []
            content += "\n" + line
            rows.append(row)
        if rows:
            self.store.queue(
                guild_id,
                self.bot.config.birthday_channel_id,
                day.isoformat(),
                content,
                rows,
                int(now.timestamp()),
            )

    async def channel(self, channel_id):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.bot.config.guild_id
        ):
            raise ValueError("Invalid birthday channel")
        permissions = channel.permissions_for(channel.guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.read_message_history
        ):
            raise ValueError("Birthday channel permissions missing")
        return channel

    async def deliver(self, now=None):
        fixed_now = now
        for pending in self.store.pending(self.bot.config.guild_id):
            now = fixed_now or datetime.now(UTC)
            try:
                if pending["state"] == "queued" and (
                    now.astimezone(BRUSSELS).hour < 7
                    or pending["day"] != now.astimezone(BRUSSELS).date().isoformat()
                ):
                    self.store.state(pending["id"], "cancelled")
                    continue
                if pending["id"] in self._reconciled:
                    continue
                if pending["state"] == "queued":
                    pending = dict(pending)
                    pending["channel_id"] = self.bot.config.birthday_channel_id
                    self.store.destination(pending["id"], pending["channel_id"])
                async with asyncio.timeout(20):
                    channel = await self.channel(pending["channel_id"])
                if pending["state"] != "queued":
                    # A crash/timeout may have happened after Discord accepted the message.
                    # Search complete history for the persisted payload, never blindly re-ping.
                    after = datetime.fromtimestamp(pending["attempted_at"], UTC) - timedelta(
                        seconds=5
                    )
                    before = min(now + timedelta(seconds=1), after + timedelta(days=1))
                    async with asyncio.timeout(20):
                        async for message in channel.history(
                            limit=None, after=after, before=before, oldest_first=True
                        ):
                            if (
                                message.author.id == self.bot.user.id
                                and message.content == pending["content"]
                            ):
                                self.store.state(pending["id"], "sent", message_id=message.id)
                                break
                        else:
                            if pending["state"] != "uncertain":
                                log.warning(
                                    "Birthday announcement delivery uncertain: id=%s", pending["id"]
                                )
                            self.store.state(pending["id"], "uncertain")
                    self._reconciled.add(pending["id"])
                    continue
                async with self.bot.lock:
                    members = self.store.celebrants(pending["id"])
                    async with asyncio.timeout(20):
                        eligible = await self.still_eligible(members)
                    if not eligible:
                        self.store.state(pending["id"], "cancelled")
                        # Nothing was sent: the other eligible players can be regrouped.
                        with self.bot.store.db:
                            self.bot.store.db.execute(
                                "DELETE FROM birthday_celebrants WHERE announcement_id=?",
                                (pending["id"],),
                            )
                        self._next_check = 0
                        continue
                    self.store.state(pending["id"], "sending", now=int(now.timestamp()))
                    try:
                        async with asyncio.timeout(20):
                            message = await channel.send(
                                pending["content"],
                                nonce=pending["id"],
                                allowed_mentions=discord.AllowedMentions(
                                    users=[discord.Object(id=r["discord_id"]) for r in members],
                                    roles=False,
                                    everyone=False,
                                    replied_user=False,
                                ),
                            )
                    except discord.HTTPException as exc:
                        if 400 <= exc.status < 500 and exc.status != 429:
                            # An explicit rejection confirms there was no public message.
                            self.store.state(pending["id"], "queued")
                        raise
                    self.store.state(pending["id"], "sent", message_id=message.id)
            except IO_ERRORS as exc:
                self.warn("announcement_" + type(exc).__name__, pending["channel_id"], now)

    async def still_eligible(self, members):
        for saved in members:
            row = self.bot.store.get(saved["discord_id"])
            if row is None or (row["region"], row["account_id"]) != (
                saved["region"],
                saved["account_id"],
            ):
                return False
            try:
                member = await self.bot.member(saved["discord_id"])
            except discord.NotFound:
                return False
            if not any(r.id == self.bot.config.role_id for r in member.roles):
                return False
        return True
