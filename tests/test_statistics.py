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
