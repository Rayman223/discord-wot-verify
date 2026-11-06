import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.http import handle_message_parameters

from wot_verify.birthday_store import BirthdayStore
from wot_verify.birthdays import BRUSSELS, Birthdays, birthday_age, expires_at
from wot_verify.bot import VerifyBot
from wot_verify.config import Config
from wot_verify.store import Store
from wot_verify.wargaming import Wargaming


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


NOW = utc("2026-11-06T06:00:00")  # 7 AM Brussels in winter.
CREATED = int(utc("2012-11-06T12:00:00").timestamp())


@pytest.fixture
def env(tmp_path):
    bot = VerifyBot(
        Config(
            discord_token="secret",
            wg_app_id="secret",
            state_secret="x" * 32,
            guild_id=1,
            role_id=2,
            clan_role_id=3,
            region="eu",
            locale="fr",
            public_url="https://example.org",
            database=str(tmp_path / "birthday.sqlite3"),
            birthday_channel_id=525037740690112527,
        )
    )
    members = {}
    posted = []

    def add(identifier=7, nickname="Player [TAG]", *, created=CREATED, player=True, linked=True):
        member = SimpleNamespace(
            id=identifier,
            name="DiscordName",
            nick=nickname,
            roles=[SimpleNamespace(id=2)] if player else [],
            guild=SimpleNamespace(id=1),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )

        async def edit(**kwargs):
            member.nick = kwargs["nick"]

        member.edit = AsyncMock(side_effect=edit)
        members[identifier] = member
        if linked:
            bot.store.link(identifier, "eu", identifier + 100, "Player")
        if created is not None:
            bot.birthdays.store.record_date("eu", identifier + 100, created, int(NOW.timestamp()))
        return member

    async def fetch(identifier):
        if identifier not in members:
            raise discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        return members[identifier]

    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )

    async def send(content, **kwargs):
        message = SimpleNamespace(
            id=len(posted) + 1, author=SimpleNamespace(id=999), content=content
        )
        posted.append(message)
        return message

    async def history(**kwargs):
        for message in posted:
            yield message

    channel.send = AsyncMock(side_effect=send)
    channel.history = history
    bot.get_channel = lambda identifier: channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.member = AsyncMock(side_effect=fetch)
    bot.wg = SimpleNamespace(
        creation_dates=AsyncMock(return_value={}),
        nickname=AsyncMock(return_value="Player"),
        clan_membership=AsyncMock(return_value=(42, "commander", "TAG")),
    )
    bot._connection.user = SimpleNamespace(id=999)
    yield SimpleNamespace(
        bot=bot, service=bot.birthdays, add=add, members=members, channel=channel, posted=posted
    )
    bot.store.close()


@pytest.mark.parametrize(
    "created,day,age",
    [
        ("2012-11-06T12:00:00", "2026-11-06", 14),
        ("2026-11-06T00:00:00", "2026-11-06", 0),
        ("2027-11-06T00:00:00", "2026-11-06", 0),
        ("2012-11-06T12:00:00", "2026-11-07", 0),
        ("2012-02-29T12:00:00", "2025-03-01", 13),
        ("2012-02-29T12:00:00", "2025-02-28", 0),
        ("2012-02-29T12:00:00", "2024-02-29", 12),
        ("2012-02-29T12:00:00", "2024-03-01", 0),
        ("2012-12-31T23:30:00", "2026-01-01", 13),
        ("2012-06-05T22:30:00", "2026-06-06", 14),
    ],
)
def test_brussels_calendar(created, day, age):
    assert birthday_age(int(utc(created).timestamp()), date.fromisoformat(day)) == age
    assert birthday_age(None, date.fromisoformat(day)) == 0


@pytest.mark.parametrize("day,hours", [("2026-03-28", 23), ("2026-10-24", 25), ("2026-12-31", 24)])
def test_expiry_is_next_local_7_am_across_clock_changes(day, hours):
    day = date.fromisoformat(day)
    start = datetime.combine(day, datetime.min.time(), BRUSSELS).replace(hour=7)
    expiry = datetime.fromtimestamp(expires_at(day), UTC).astimezone(BRUSSELS)
    assert expiry.hour == 7
    assert expiry.date() == day + timedelta(days=1)
    assert expiry.timestamp() - start.timestamp() == hours * 3600


async def test_daily_job_7_am_real_ping_and_restart_without_duplicates(env):
    member = env.add()
    await env.service.tick(now=NOW - timedelta(seconds=1))
    member.edit.assert_not_awaited()
    env.channel.send.assert_not_awaited()
    await env.service.tick(now=NOW)
    assert member.nick == "Player [TAG] " + "🕯" * 14
    assert "<@7> " + "🕯" * 14 in env.posted[0].content
    assert "14 ans" in env.posted[0].content
    mentions = env.channel.send.await_args.kwargs["allowed_mentions"].to_dict()
    assert mentions["users"] == [7] and mentions["parse"] == []
    kwargs = env.channel.send.await_args.kwargs
    with handle_message_parameters(
        content=env.posted[0].content,
        nonce=kwargs["nonce"],
        allowed_mentions=kwargs["allowed_mentions"],
    ) as parameters:
        assert parameters.payload["enforce_nonce"] is True
    restarted = Birthdays(env.bot)
    await restarted.tick(now=NOW + timedelta(hours=3))
    member.edit.assert_awaited_once()
    env.channel.send.assert_awaited_once()
    assert env.service.store.decoration(1, 7)["expires_at"] == expires_at(date(2026, 11, 6))


async def test_next_day_keeps_candles_until_7_then_removes_all(env):
    member = env.add(nickname="🕯Player 🕯️[TAG]🕯︎")
    await env.service.tick(now=NOW)
    await env.service.tick(now=NOW + timedelta(days=1) - timedelta(seconds=1))
    assert member.nick.endswith("🕯" * 14)
    await env.service.tick(now=NOW + timedelta(days=1))
    assert member.nick == "Player [TAG]"
    assert env.service.store.decoration(1, 7) is None
    env.channel.send.assert_awaited_once()


async def test_restart_after_days_cleans_all_remaining_candles_even_before_7(env):
    member = env.add()
    await env.service.tick(now=NOW)
    await Birthdays(env.bot).tick(now=NOW + timedelta(days=3, hours=-2))
    assert member.nick == "Player [TAG]"
    env.channel.send.assert_awaited_once()


async def test_legacy_candles_before_7_adopt_yesterday_only(env):
    yesterday = env.add(nickname="🕯Player [TAG]🕯️")
    old = env.add(8, nickname="Old 🕯🕯", created=int(utc("2010-11-04T12:00:00").timestamp()))
    await env.service.tick(now=NOW + timedelta(days=1, seconds=-1))
    assert yesterday.nick == "🕯Player [TAG]🕯️"
    assert old.nick == "Old"
    env.channel.send.assert_not_awaited()
    await env.service.tick(now=NOW + timedelta(days=1))
    assert yesterday.nick == "Player [TAG]"


async def test_no_missed_day_announcements_unknown_future_or_non_players(env):
    env.add(7, created=None)
    missing_role = env.add(8, nickname="Other 🕯", player=False)
    env.add(9, linked=False)
    env.add(10, created=int(utc("2026-11-06T01:00:00").timestamp()))
    env.add(11)
    del env.members[11]
    old = env.add(12, nickname="Old 🕯", created=int(utc("2010-11-05T12:00:00").timestamp()))
    await env.service.tick(now=NOW)
    assert old.nick == "Old"
    assert missing_role.nick == "Other"
    env.channel.send.assert_not_awaited()


async def test_overlong_nickname_preserves_identity_but_announces(env, caplog):
    member = env.add(nickname="PlayerWithLongName [TAG] 🕯🕯")
    await env.service.tick(now=NOW)
    assert member.nick == "PlayerWithLongName [TAG]"
    assert "nickname_too_long" in caplog.text and "7" in caplog.text
    env.channel.send.assert_awaited_once()


@pytest.mark.parametrize("length", [16, 17, 18])
async def test_nickname_limit_never_truncates_identity_or_candle_count(env, length):
    base = "P" * length
    member = env.add(nickname=base)
    await env.service.tick(now=NOW)
    expected = base + " " + "🕯" * 14 if length <= 17 else base
    assert member.nick == expected
    assert len(member.nick) <= 32


async def test_discord_refusal_does_not_stop_others_or_announcement(env):
    failed = env.add(7)
    failed.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "SECRET")
    good = env.add(8)
    await env.service.tick(now=NOW)
    assert failed.nick == "Player [TAG]"
    assert good.nick.endswith("🕯" * 14)
    assert "<@7>" in env.posted[0].content and "<@8>" in env.posted[0].content
    failed.edit.side_effect = None
    await env.service.tick(now=NOW + timedelta(minutes=15))
    assert failed.edit.await_count == 2
    env.channel.send.assert_awaited_once()


async def test_uncertain_send_reconciles_history_after_restart(env):
    env.add()
    original = env.channel.send.side_effect

    async def accepted_but_timeout(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("PRIVATE_TOKEN")

    env.channel.send.side_effect = accepted_but_timeout
    await env.service.tick(now=NOW)
    pending = env.service.store.pending(1)[0]
    assert pending["state"] == "sending"
    await Birthdays(env.bot).tick(now=NOW + timedelta(hours=2))
    assert not env.service.store.pending(1)
    env.channel.send.assert_awaited_once()


async def test_unknown_delivery_never_blindly_repings(env, caplog):
    env.add()
    env.channel.send.side_effect = TimeoutError("PRIVATE_TOKEN")
    await env.service.tick(now=NOW)
    await Birthdays(env.bot).tick(now=NOW + timedelta(hours=2))
    assert env.service.store.pending(1)[0]["state"] == "uncertain"
    await env.service.tick(now=NOW + timedelta(hours=4))
    env.channel.send.assert_awaited_once()
    assert "PRIVATE_TOKEN" not in caplog.text


async def test_wrong_channel_or_missing_permissions_keeps_queued_job(env):
    env.add()
    env.channel.guild.id = 999
    await env.service.tick(now=NOW)
    env.channel.send.assert_not_awaited()
    assert env.service.store.pending(1)[0]["state"] == "queued"
    env.channel.guild.id = 1
    env.channel.permissions_for.return_value.read_message_history = False
    await env.service.tick(now=NOW + timedelta(minutes=1))
    env.channel.send.assert_not_awaited()
    env.channel.permissions_for.return_value.read_message_history = True
    await env.service.tick(now=NOW + timedelta(minutes=2))
    env.channel.send.assert_awaited_once()


async def test_no_late_ping_for_yesterday_pending_job(env):
    env.add()
    env.channel.guild.id = 99
    await env.service.tick(now=NOW)
    env.channel.guild.id = 1
    await env.service.tick(now=NOW + timedelta(days=1))
    env.channel.send.assert_not_awaited()
    assert not env.service.store.pending(1)


async def test_rechecks_eligibility_before_send_then_regroups_remaining_players(env):
    env.add(7)
    member = env.add(8)
    env.channel.guild.id = 99
    await env.service.tick(now=NOW)
    env.channel.guild.id = 1
    member.roles = []
    await env.service.tick(now=NOW + timedelta(minutes=1))
    env.channel.send.assert_not_awaited()
    await env.service.tick(now=NOW + timedelta(minutes=2))
    assert "<@7>" in env.posted[0].content
    assert "<@8>" not in env.posted[0].content


async def test_many_birthdays_are_split_and_all_mentions_are_restricted(env):
    for identifier in range(100, 180):
        env.add(identifier, nickname="Player")
    await env.service.tick(now=NOW)
    assert len(env.posted) > 1
    mentions = []
    for call in env.channel.send.await_args_list:
        assert len(call.args[0].encode("utf-16-le")) // 2 <= 2000
        allowed = call.kwargs["allowed_mentions"].to_dict()
        assert allowed["parse"] == []
        mentions.extend(allowed["users"])
    assert sorted(mentions) == list(range(100, 180))
    await Birthdays(env.bot).tick(now=NOW + timedelta(hours=1))
    assert env.channel.send.await_count == len(env.posted)


async def test_new_eligible_member_later_same_day_is_announced_once(env):
    env.add(7)
    await env.service.tick(now=NOW)
    env.add(8)
    await env.service.tick(now=NOW + timedelta(minutes=15))
    assert len(env.posted) == 2
    assert "<@8>" in env.posted[1].content and "<@7>" not in env.posted[1].content


async def test_rejected_send_can_resume_after_permissions_are_fixed(env):
    env.add()
    original = env.channel.send.side_effect
    env.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    await env.service.tick(now=NOW)
    assert env.service.store.pending(1)[0]["state"] == "queued"
    env.channel.send.side_effect = original
    await env.service.tick(now=NOW + timedelta(minutes=1))
    assert len(env.posted) == 1
    assert not env.service.store.pending(1)


async def test_unsent_announcement_uses_corrected_configured_channel(env):
    env.add()
    env.channel.guild.id = 99
    await env.service.tick(now=NOW)
    env.bot.config = replace(env.bot.config, birthday_channel_id=777)
    env.channel.guild.id = 1
    destinations = []

    def channel(identifier):
        destinations.append(identifier)
        return env.channel

    env.bot.get_channel = channel
    await env.service.tick(now=NOW + timedelta(minutes=1))
    assert destinations == [777]
    saved = env.bot.store.db.execute("SELECT * FROM birthday_announcements").fetchone()
    assert saved["channel_id"] == 777 and saved["state"] == "sent"


async def test_first_anniversary_uses_one_candle_and_singular_translation(env):
    member = env.add(created=int(utc("2025-11-06T12:00:00").timestamp()))
    await env.service.tick(now=NOW)
    assert member.nick == "Player [TAG] 🕯"
    assert "première année" in env.posted[0].content


def test_resync_before_7_preserves_yesterdays_legacy_candles_only(env):
    member = env.add(nickname="Player 🕯🕯")
    row = env.bot.store.get(7)
    morning = NOW + timedelta(days=1, hours=-1)
    assert (
        env.service.prepare_nickname(row, "Player", current=member.nick, now=morning)
        == "Player " + "🕯" * 14
    )
    env.service.store.forget(1, 7)
    assert env.service.prepare_nickname(row, "Player", current="Player", now=morning) == "Player"


async def test_disabled_feature_collects_dates_without_discord_mutations(env):
    member = env.add(created=None)
    env.bot.config = replace(env.bot.config, birthday_channel_id=0)
    env.bot.wg.creation_dates.return_value = {107: CREATED}
    await env.service.collect([env.bot.store.get(7)], now=NOW)
    assert env.service.store.created_at("eu", 107) == CREATED
    await env.service.tick(now=NOW)
    member.edit.assert_not_awaited()
    env.channel.send.assert_not_awaited()


async def test_collection_batches_retries_and_preserves_known_dates(env):
    rows = []
    for identifier in range(1, 202):
        env.bot.store.link(identifier, "eu", identifier + 100, "Player")
        rows.append(env.bot.store.get(identifier))
    env.bot.wg.creation_dates.side_effect = [ValueError("wg_error"), {201: CREATED}, {}]
    await env.service.collect(rows, now=NOW)
    assert [len(c.args[0]) for c in env.bot.wg.creation_dates.await_args_list] == [100, 100, 1]
    assert env.service.store.created_at("eu", 101) is None
    assert env.service.store.created_at("eu", 201) == CREATED
    await env.service.collect(rows, now=NOW + timedelta(minutes=14))
    assert env.bot.wg.creation_dates.await_count == 3
    env.bot.wg.creation_dates.side_effect = None
    env.bot.wg.creation_dates.return_value = {101: CREATED}
    await env.service.collect(rows, now=NOW + timedelta(minutes=15))
    assert env.service.store.created_at("eu", 101) == CREATED
    assert all(201 not in c.args[0] for c in env.bot.wg.creation_dates.await_args_list[3:])


@pytest.mark.parametrize("invalid", [None, False, True, "123", 0, -1, 2**63, 10**100])
async def test_wg_date_validation_independent_of_nickname(invalid):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(
        return_value={
            "10": {"nickname": "Player", "created_at": invalid},
            "11": {"nickname": None, "created_at": CREATED},
        }
    )
    assert await wg.creation_dates([10, 11]) == {11: CREATED}
    assert await wg.nickname(10) == "Player"


async def test_creation_date_requests_share_limiter_and_bound_batches():
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(side_effect=[ValueError("wg_error"), {"101": {"created_at": CREATED}}])
    assert await wg.creation_dates([*range(1, 102), 101]) == {101: CREATED}
    assert [len(c.kwargs["account_id"].split(",")) for c in wg.request.await_args_list] == [100, 1]
    assert all(c.args == ("account/info",) for c in wg.request.await_args_list)


async def test_cleanup_survives_unlink_restart_and_permission_failure(env):
    member = env.add(nickname="Player 🕯️ [TAG]🕯")
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    with pytest.raises(discord.Forbidden):
        await env.service.cleanup(member)
    env.bot.store.unlink(7)
    assert env.service.store.decoration(1, 7) is not None
    member.edit.side_effect = None
    await Birthdays(env.bot).tick(now=NOW + timedelta(days=2))
    assert env.service.store.decoration(1, 7) is None
    assert member.edit.await_args.kwargs["nick"] == "Player [TAG]"


def test_versioned_migration_replay_and_metadata_persistence(tmp_path):
    path = str(tmp_path / "upgrade.sqlite3")
    store = Store(path)
    store.link(7, "eu", 107, "Player")
    birthdays = BirthdayStore(store.db)
    birthdays.record_date("eu", 107, CREATED, int(NOW.timestamp()))
    birthdays.record_date("eu", 107, None, int(NOW.timestamp()))
    birthdays.track(1, 7, 123)
    migration = Path(__file__).parents[1] / "wot_verify/migrations/010_birthdays.sql"
    for _ in range(2):
        store.db.executescript(migration.read_text())
    store.close()
    store = Store(path)
    birthdays = BirthdayStore(store.db)
    assert birthdays.created_at("eu", 107) == CREATED
    assert birthdays.decoration(1, 7)["expires_at"] == 123
    assert store.get(7)["nickname"] == "Player"
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO account_birthdays VALUES ('eu', 1, -1, 0)")
    store.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        ("0", 0),
        ("525037740690112527", 525037740690112527),
        ("-1", None),
        (str(2**63), None),
        ("abc", None),
    ],
)
def test_config_activation(monkeypatch, value, expected):
    for key, item in {
        "DISCORD_TOKEN": "test",
        "WG_APP_ID": "test",
        "STATE_SECRET": "x" * 32,
        "GUILD_ID": "1",
        "ROLE_ID": "2",
        "PUBLIC_URL": "https://example.org",
    }.items():
        monkeypatch.setenv(key, item)
    monkeypatch.delenv("BIRTHDAY_CHANNEL_ID", raising=False)
    if value is not None:
        monkeypatch.setenv("BIRTHDAY_CHANNEL_ID", value)
    if expected is None:
        with pytest.raises(ValueError):
            Config.load()
    else:
        assert Config.load().birthday_channel_id == expected


def freeze_calendar(monkeypatch, now=NOW):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr("wot_verify.birthdays.datetime", Frozen)


async def test_resync_and_contact_claim_recompose_candles_without_changing_stored_identity(
    env, monkeypatch
):
    freeze_calendar(monkeypatch)
    member = env.add(nickname="OldName 🕯️")
    env.bot.role = lambda _: SimpleNamespace(id=2)
    env.bot.contact_role = lambda _: SimpleNamespace(id=3)
    env.bot.is_ready = lambda: True
    async with env.bot.lock:
        await env.bot.sync(env.bot.store.get(7))
        assert member.nick == "Player " + "🕯" * 14
        assert env.bot.store.get(7)["nickname"] == "Player"
        await env.bot.claim_contact(7)
    assert member.nick == "Player [TAG] " + "🕯" * 14
    assert env.bot.store.contact_tags(7) == [" [TAG]"]
    await env.bot.remove_contact(7)
    assert member.nick == "Player " + "🕯" * 14
    assert env.bot.store.get(7) is not None


@pytest.mark.parametrize("manual", [False, True])
async def test_chained_contact_and_birthday_cleanup_never_restores_stale_tag(env, manual):
    initial = env.add(nickname="Player [TAG] " + "🕯" * 14)
    env.bot.store.reserve_contact(7, "eu", 42)
    env.bot.store.remember_tag(7, " [TAG]")
    server = {"nick": initial.nick, "roles": [2, 3]}

    async def fetch(_):
        # Like Discord, edit returns a different object and leaves this snapshot unchanged.
        snapshot = SimpleNamespace(
            id=7,
            nick=server["nick"],
            roles=[SimpleNamespace(id=r) for r in server["roles"]],
            guild=SimpleNamespace(id=1),
        )

        async def edit(**kwargs):
            server["nick"] = kwargs["nick"]
            return await fetch(7)

        async def remove(role, **kwargs):
            server["roles"].remove(role.id)

        snapshot.edit = AsyncMock(side_effect=edit)
        snapshot.remove_roles = AsyncMock(side_effect=remove)
        return snapshot

    env.bot.member = AsyncMock(side_effect=fetch)
    env.bot.role = lambda _: SimpleNamespace(id=2)
    env.bot.contact_role = lambda _: SimpleNamespace(id=3)
    if manual:
        before = SimpleNamespace(roles=[SimpleNamespace(id=r) for r in server["roles"]])
        server["roles"] = []
        await env.bot.on_member_update(before, await fetch(7))
    else:
        assert await env.bot.remove(7)
    assert server == {"nick": "Player", "roles": []}
    assert (env.bot.store.get(7) is None) is (not manual)
    assert env.service.store.decoration(1, 7) is None


async def test_manual_player_role_removal_cleans_candles_with_feature_only(env):
    member = env.add(nickname="Player 🕯🕯")
    env.bot.config = replace(env.bot.config, clan_role_id=0, visitor_role_id=0)
    before = SimpleNamespace(roles=list(member.roles))
    member.roles = []
    await env.bot.on_member_update(before, member)
    assert member.nick == "Player"
    assert env.bot.store.get(7) is not None
    member.add_roles.assert_not_awaited()


async def test_birthday_jobs_are_cancelled_before_database_closes(env):
    started_dates, started_jobs = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def waiting(event):
        event.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Both tasks must stop while their database is still open.
            env.bot.store.db.execute("SELECT 1")
            cancelled.append(event)

    env.bot.wait_until_ready = AsyncMock()

    async def dates(rows):
        await waiting(started_dates)

    async def jobs():
        await waiting(started_jobs)

    env.service.collect = AsyncMock(side_effect=dates)
    env.service.tick = AsyncMock(side_effect=jobs)
    env.bot.birthday_dates.start()
    env.bot.birthday_jobs.start()
    await asyncio.wait_for(asyncio.gather(started_dates.wait(), started_jobs.wait()), timeout=2)
    await env.bot.close()
    assert len(cancelled) == 2
    assert env.bot.birthday_dates.get_task().done()
    assert env.bot.birthday_jobs.get_task().done()


def test_brussels_zone_available_without_system_tzdata():
    import zoneinfo

    original = zoneinfo.TZPATH
    try:
        zoneinfo.reset_tzpath(())
        zone = zoneinfo.ZoneInfo.no_cache("Europe/Brussels")
        assert NOW.astimezone(zone).hour == 7
    finally:
        zoneinfo.reset_tzpath(original)


async def test_no_lock_held_while_collecting_metadata(env):
    env.add(created=None)

    async def dates(ids):
        assert not env.bot.lock.locked()
        async with env.bot.lock:
            await asyncio.sleep(0)
        return {107: CREATED}

    env.bot.wg.creation_dates.side_effect = dates
    await env.service.collect([env.bot.store.get(7)], now=NOW)
