"""Read-only statistics derived from a consistent SQLite snapshot."""

from contextlib import contextmanager


@contextmanager
def _snapshot(db):
    db.execute("SAVEPOINT statistics_snapshot")
    try:
        yield
    finally:
        db.execute("RELEASE SAVEPOINT statistics_snapshot")


def local_statistics(store, now):
    db = store.db
    with _snapshot(db):

        def scalar(sql, params=()):
            return db.execute(sql, params).fetchone()[0]

        stats = {}
        queries = {
            "links": ("SELECT count(*) FROM links", ()),
            "contacts": ("SELECT count(*) FROM clan_contacts", ()),
            "never_synced": ("SELECT count(*) FROM links WHERE synced_at=0", ()),
            "due": ("SELECT count(*) FROM links WHERE synced_at<=?", (now - 30 * 86400,)),
            "recent": ("SELECT count(*) FROM links WHERE synced_at>?", (now - 30 * 86400,)),
            "oldest": ("SELECT min(synced_at) FROM links WHERE synced_at>0", ()),
            "newest": ("SELECT max(synced_at) FROM links WHERE synced_at>0", ()),
            "failures": ("SELECT count(*) FROM audit_failures", ()),
            "repeated_failures": ("SELECT count(*) FROM audit_failures WHERE failures>=2", ()),
            "failure_total": ("SELECT coalesce(sum(failures), 0) FROM audit_failures", ()),
            "failure_max": ("SELECT coalesce(max(failures), 0) FROM audit_failures", ()),
            "alerts_24h": (
                "SELECT count(*) FROM audit_failures WHERE notified_at>0 AND notified_at>?",
                (now - 86400,),
            ),
            "challenges_valid": ("SELECT count(*) FROM challenges WHERE expires_at>?", (now,)),
            "challenges_expired": ("SELECT count(*) FROM challenges WHERE expires_at<=?", (now,)),
            "retries_valid": ("SELECT count(*) FROM retry_sessions WHERE expires_at>?", (now,)),
            "retries_expired": ("SELECT count(*) FROM retry_sessions WHERE expires_at<=?", (now,)),
            "retries_ready": (
                "SELECT count(*) FROM retry_sessions WHERE expires_at>? AND attempts<3 AND next_attempt<=?",
                (now, now),
            ),
            "retries_waiting": (
                "SELECT count(*) FROM retry_sessions WHERE expires_at>? AND attempts<3 AND next_attempt>?",
                (now, now),
            ),
            "retries_exhausted": (
                "SELECT count(*) FROM retry_sessions WHERE expires_at>? AND attempts>=3",
                (now,),
            ),
            "nonces": ("SELECT count(*) FROM openid_nonces", ()),
            "panels": ("SELECT count(*) FROM verification_panel", ()),
            "tags": ("SELECT count(*) FROM clan_tag_state", ()),
            "tag_members": ("SELECT count(DISTINCT discord_id) FROM clan_tag_state", ()),
            "tags_pending": (
                (
                    "SELECT count(DISTINCT t.discord_id) FROM clan_tag_state t "
                    "LEFT JOIN clan_contacts c ON c.discord_id=t.discord_id WHERE c.discord_id IS NULL"
                ),
                (),
            ),
            "orphan_contacts": (
                (
                    "SELECT count(*) FROM clan_contacts c LEFT JOIN links l "
                    "ON c.discord_id=l.discord_id AND c.region=l.region WHERE l.discord_id IS NULL"
                ),
                (),
            ),
            "linked_contacts": (
                (
                    "SELECT count(*) FROM clan_contacts c JOIN links l "
                    "ON c.discord_id=l.discord_id AND c.region=l.region"
                ),
                (),
            ),
            "migrations": ("SELECT count(*) FROM migrations", ()),
        }
        for key, (sql, params) in queries.items():
            stats[key] = scalar(sql, params)
        for table in ("verification_notices", "clan_contact_notices"):
            for delivered in (0, 1):
                stats[f"{table}_{delivered}"] = scalar(
                    f"SELECT count(*) FROM {table} WHERE delivered=?", (delivered,)
                )
        stats["database_bytes"] = scalar("PRAGMA page_count") * scalar("PRAGMA page_size")
        return stats
