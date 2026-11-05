from wot_verify.statistics import local_statistics
from wot_verify.store import Store


def test_local_statistics_are_read_only_and_use_periodic_sync_boundary(tmp_path):
    store = Store(str(tmp_path / "statistics.sqlite3"))
    now = 4_000_000
    try:
        store.link(1, "eu", 10, "One")
        store.link(2, "eu", 20, "Two")
        store.link(3, "eu", 30, "Three")
        store.db.execute("UPDATE links SET synced_at=? WHERE discord_id=2", (now - 30 * 86400,))
        store.db.execute("UPDATE links SET synced_at=? WHERE discord_id=3", (now - 1,))
        store.db.commit()
        store.reserve_contact(1, "eu", 42)
        store.reserve_contact(4, "eu", 43)
        store.sync_failure_notice(1, now)
        store.sync_failure_notice(1, now)
        before = list(store.db.iterdump())
        stats = local_statistics(store, now)
        assert (stats["links"], stats["contacts"], stats["linked_contacts"]) == (3, 2, 1)
        assert (stats["never_synced"], stats["due"], stats["recent"]) == (1, 2, 1)
        assert (stats["failures"], stats["repeated_failures"], stats["failure_total"]) == (1, 1, 2)
        assert stats["orphan_contacts"] == 1
        assert stats["alerts_24h"] == 1
        assert list(store.db.iterdump()) == before
    finally:
        store.close()


def test_retry_and_confirmation_counts_distinguish_expired_and_exhausted(tmp_path):
    store = Store(str(tmp_path / "sessions.sqlite3"))
    now = 4_000_000
    try:
        for index, (expires, attempts, next_attempt) in enumerate(
            [
                (now, 0, now),
                (now + 1, 0, now),
                (now + 1, 1, now + 1),
                (now + 1, 3, now),
            ],
            start=1,
        ):
            store.db.execute(
                "INSERT INTO retry_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(index), str(index), index, "eu", index, 1, expires, next_attempt, attempts),
            )
        store.db.execute("INSERT INTO challenges VALUES ('a', 1, ?)", (now,))
        store.db.execute("INSERT INTO challenges VALUES ('b', 2, ?)", (now + 1,))
        store.db.commit()
        store.queue_verification_notice(1, "eu", 1, 1)
        store.queue_verification_notice(2, "eu", 2, 1)
        store.mark_notice_delivered(2)
        before = list(store.db.iterdump())
        stats = local_statistics(store, now)
        assert (stats["retries_expired"], stats["retries_valid"]) == (1, 3)
        assert (stats["retries_ready"], stats["retries_waiting"], stats["retries_exhausted"]) == (
            1,
            1,
            1,
        )
        assert (stats["challenges_valid"], stats["challenges_expired"]) == (1, 1)
        assert (stats["verification_notices_0"], stats["verification_notices_1"]) == (1, 1)
        assert stats["oldest"] is None
        assert stats["newest"] is None
        assert stats["database_bytes"] > 0
        assert list(store.db.iterdump()) == before
    finally:
        store.close()
