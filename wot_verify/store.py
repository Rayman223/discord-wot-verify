import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
        root = Path(__file__).resolve().parent / "migrations"
        for migration in sorted(root.glob("*.sql")):
            if not self.db.execute(
                "SELECT 1 FROM migrations WHERE name=?", (migration.name,)
            ).fetchone():
                # Schema SQL is idempotent, including recovery after an interrupted migration.
                self.db.executescript(migration.read_text())
                with self.db:
                    self.db.execute("INSERT INTO migrations VALUES (?)", (migration.name,))

    def challenge(self, nonce, discord_id, expires_at):
        with self.db:
            self.db.execute(
                "DELETE FROM challenges WHERE expires_at<=? OR discord_id=?",
                (int(time.time()), discord_id),
            )
            self.db.execute(
                "INSERT INTO challenges VALUES (?, ?, ?)", (nonce, discord_id, expires_at)
            )

    def consume(self, nonce, discord_id, now):
        with self.db:
            return (
                self.db.execute(
                    "DELETE FROM challenges WHERE nonce=? AND discord_id=? AND expires_at>?",
                    (nonce, discord_id, now),
                ).rowcount
                == 1
            )

    def get(self, discord_id):
        return self.db.execute("SELECT * FROM links WHERE discord_id=?", (discord_id,)).fetchone()

    def use_openid_nonce(self, nonce):
        try:
            with self.db:
                self.db.execute(
                    "DELETE FROM openid_nonces WHERE used_at<?", (int(time.time()) - 1200,)
                )
                self.db.execute(
                    "INSERT INTO openid_nonces VALUES (?, ?)", (nonce, int(time.time()))
                )
        except sqlite3.IntegrityError:
            raise ValueError("wg_error") from None

    def by_account(self, region, account_id):
        return self.db.execute(
            "SELECT * FROM links WHERE region=? AND account_id=?", (region, account_id)
        ).fetchone()

    def link(self, discord_id, region, account_id, nickname):
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO links VALUES (?, ?, ?, ?, 0)",
                    (discord_id, region, account_id, nickname),
                )
        except sqlite3.IntegrityError:
            existing = self.get(discord_id)
            if not existing or (existing["region"], existing["account_id"]) != (region, account_id):
                raise ValueError("already_linked") from None

    def mark_synced(self, discord_id, nickname):
        with self.db:
            self.db.execute(
                "UPDATE links SET nickname=?, synced_at=? WHERE discord_id=?",
                (nickname, int(time.time()), discord_id),
            )

    def unlink(self, discord_id):
        with self.db:
            self.db.execute("DELETE FROM clan_contacts WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM challenges WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM links WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM audit_failures WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM retry_sessions WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM verification_notices WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM clan_contact_notices WHERE discord_id=?", (discord_id,))

    def contact(self, discord_id):
        return self.db.execute(
            "SELECT * FROM clan_contacts WHERE discord_id=?", (discord_id,)
        ).fetchone()

    def clan_contact(self, region, clan_id):
        return self.db.execute(
            "SELECT * FROM clan_contacts WHERE region=? AND clan_id=?", (region, clan_id)
        ).fetchone()

    def reserve_contact(self, discord_id, region, clan_id):
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO clan_contacts VALUES (?, ?, ?)", (discord_id, region, clan_id)
                )
        except sqlite3.IntegrityError:
            row = self.contact(discord_id)
            if not row or (row["region"], row["clan_id"]) != (region, clan_id):
                raise ValueError("clan_taken") from None

    def release_contact(self, discord_id):
        with self.db:
            self.db.execute("DELETE FROM clan_contacts WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM clan_contact_notices WHERE discord_id=?", (discord_id,))

    def remember_tag(self, discord_id, suffix):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO clan_tag_state VALUES (?, ?)", (discord_id, suffix)
            )

    def contact_tags(self, discord_id):
        return [
            row[0]
            for row in self.db.execute(
                "SELECT suffix FROM clan_tag_state WHERE discord_id=?", (discord_id,)
            )
        ]

    def clear_tags(self, discord_id):
        with self.db:
            self.db.execute("DELETE FROM clan_tag_state WHERE discord_id=?", (discord_id,))

    def confirm_tag(self, discord_id, suffix):
        """Only the most recently confirmed Discord nickname remains managed."""
        with self.db:
            self.db.execute("DELETE FROM clan_tag_state WHERE discord_id=?", (discord_id,))
            self.db.execute("INSERT INTO clan_tag_state VALUES (?, ?)", (discord_id, suffix))

    def due(self):
        return self.db.execute(
            "SELECT * FROM links WHERE synced_at<=?", (int(time.time()) - 30 * 86400,)
        ).fetchall()

    def queue_contact_notice(self, discord_id, region, account_id, clan_id, clan_tag, channel_id):
        with self.db:
            self.db.execute(
                "INSERT INTO clan_contact_notices "
                "(discord_id, region, account_id, clan_id, clan_tag, channel_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET clan_tag=excluded.clan_tag, "
                "channel_id=CASE WHEN excluded.channel_id=0 THEN clan_contact_notices.channel_id ELSE excluded.channel_id END "
                "WHERE clan_contact_notices.delivered=0",
                (discord_id, region, account_id, clan_id, clan_tag, channel_id),
            )

    def contact_notice(self, discord_id):
        return self.db.execute(
            "SELECT * FROM clan_contact_notices WHERE discord_id=? AND delivered=0", (discord_id,)
        ).fetchone()

    def mark_contact_notice_delivered(self, discord_id):
        with self.db:
            self.db.execute(
                "UPDATE clan_contact_notices SET delivered=1 WHERE discord_id=?", (discord_id,)
            )

    def queue_verification_notice(self, discord_id, region, account_id, channel_id):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO verification_notices "
                "(discord_id, region, account_id, channel_id) VALUES (?, ?, ?, ?)",
                (discord_id, region, account_id, channel_id),
            )

    def verification_notice(self, discord_id):
        return self.db.execute(
            "SELECT * FROM verification_notices WHERE discord_id=? AND delivered=0", (discord_id,)
        ).fetchone()

    def mark_notice_delivered(self, discord_id):
        with self.db:
            self.db.execute(
                "UPDATE verification_notices SET delivered=1 WHERE discord_id=?", (discord_id,)
            )

    def issue_retry(
        self, discord_id, region, account_id, channel_id, *, expires_at=None, attempts=0
    ):
        now = int(time.time())
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires_at = expires_at if expires_at is not None else now + 600
        with self.db:
            self.db.execute(
                "DELETE FROM retry_sessions WHERE expires_at<=? OR discord_id=?", (now, discord_id)
            )
            self.db.execute(
                "INSERT INTO retry_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(cookie.encode()).hexdigest(),
                    hashlib.sha256(csrf.encode()).hexdigest(),
                    discord_id,
                    region,
                    account_id,
                    channel_id,
                    expires_at,
                    now + 30 if attempts else now,
                    attempts,
                ),
            )
        return cookie, csrf, max(0, expires_at - now)

    def consume_retry(self, cookie, csrf):
        now = int(time.time())
        if not 1 <= len(cookie) <= 128 or not 1 <= len(csrf) <= 128:
            raise ValueError("invalid_state")
        with self.db:
            row = self.db.execute(
                "SELECT * FROM retry_sessions WHERE cookie_hash=?",
                (hashlib.sha256(cookie.encode()).hexdigest(),),
            ).fetchone()
            if (
                row is None
                or row["expires_at"] <= now
                or row["attempts"] >= 3
                or not hmac.compare_digest(
                    row["csrf_hash"], hashlib.sha256(csrf.encode()).hexdigest()
                )
            ):
                raise ValueError("invalid_state")
            if row["next_attempt"] > now:
                raise ValueError("retry_wait")
            self.db.execute("DELETE FROM retry_sessions WHERE cookie_hash=?", (row["cookie_hash"],))
            return row

    def sync_failure_notice(self, discord_id, now):
        with self.db:
            self.db.execute(
                "INSERT INTO audit_failures VALUES (?, 1, 0) "
                "ON CONFLICT(discord_id) DO UPDATE SET failures=failures+1",
                (discord_id,),
            )
            return (
                self.db.execute(
                    "UPDATE audit_failures SET notified_at=? WHERE discord_id=? "
                    "AND failures>=2 AND (notified_at=0 OR notified_at<=?)",
                    (now, discord_id, now - 86400),
                ).rowcount
                == 1
            )

    def clear_sync_failures(self, discord_id):
        with self.db:
            self.db.execute("DELETE FROM audit_failures WHERE discord_id=?", (discord_id,))

    def contacts_page(self, page, size=5):
        total = self.db.execute("SELECT count(*) FROM clan_contacts").fetchone()[0]
        rows = self.db.execute(
            "SELECT c.*, l.account_id FROM clan_contacts c LEFT JOIN links l "
            "ON l.discord_id=c.discord_id AND l.region=c.region "
            "ORDER BY c.discord_id LIMIT ? OFFSET ?",
            (size, (page - 1) * size),
        ).fetchall()
        return total, rows

    def panel(self, guild_id):
        return self.db.execute(
            "SELECT * FROM verification_panel WHERE guild_id=?", (guild_id,)
        ).fetchone()

    def save_panel(self, guild_id, channel_id, message_id):
        with self.db:
            self.db.execute(
                "INSERT INTO verification_panel VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
                (guild_id, channel_id, message_id),
            )

    def close(self):
        self.db.close()
