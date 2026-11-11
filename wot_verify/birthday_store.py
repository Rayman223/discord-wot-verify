"""Durable birthday metadata, cleanup and announcement outbox."""

import uuid


class BirthdayStore:
    def __init__(self, db):
        self.db = db

    def created_at(self, region, account_id):
        row = self.db.execute(
            "SELECT created_at FROM account_birthdays WHERE region=? AND account_id=?",
            (region, account_id),
        ).fetchone()
        return row[0] if row else None

    def missing(self, rows, now):
        result = []
        for row in rows:
            saved = self.db.execute(
                "SELECT created_at, retry_after FROM account_birthdays "
                "WHERE region=? AND account_id=?",
                (row["region"], row["account_id"]),
            ).fetchone()
            if saved is None or (saved["created_at"] is None and saved["retry_after"] <= now):
                result.append(row)
        return result

    def record_date(self, region, account_id, created_at, now):
        with self.db:
            self.db.execute(
                "INSERT INTO account_birthdays VALUES (?, ?, ?, ?) "
                "ON CONFLICT(region, account_id) DO UPDATE SET "
                "created_at=coalesce(account_birthdays.created_at, excluded.created_at), "
                "retry_after=excluded.retry_after",
                (region, account_id, created_at, now + 900),
            )

    def decorations(self, guild_id):
        return self.db.execute(
            "SELECT * FROM birthday_decorations WHERE guild_id=?", (guild_id,)
        ).fetchall()

    def decoration(self, guild_id, discord_id):
        return self.db.execute(
            "SELECT * FROM birthday_decorations WHERE guild_id=? AND discord_id=?",
            (guild_id, discord_id),
        ).fetchone()

    def track(self, guild_id, discord_id, expires_at):
        with self.db:
            self.db.execute(
                "INSERT INTO birthday_decorations VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, discord_id) DO UPDATE SET expires_at=excluded.expires_at",
                (guild_id, discord_id, expires_at),
            )

    def forget(self, guild_id, discord_id):
        with self.db:
            self.db.execute(
                "DELETE FROM birthday_decorations WHERE guild_id=? AND discord_id=?",
                (guild_id, discord_id),
            )

    def reserved(self, guild_id, day, row):
        return self.db.execute(
            "SELECT 1 FROM birthday_celebrants WHERE guild_id=? AND day=? "
            "AND (discord_id=? OR (region=? AND account_id=?))",
            (guild_id, day, row["discord_id"], row["region"], row["account_id"]),
        ).fetchone()

    def queue(self, guild_id, channel_id, day, content, rows, now):
        identifier = uuid.uuid4().hex[:24]  # Also a Discord nonce (maximum 25 characters).
        with self.db:
            self.db.execute(
                "INSERT INTO birthday_announcements "
                "(id, guild_id, channel_id, day, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (identifier, guild_id, channel_id, day, content, now),
            )
            for row in rows:
                self.db.execute(
                    "INSERT INTO birthday_celebrants VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        day,
                        row["discord_id"],
                        row["region"],
                        row["account_id"],
                        identifier,
                    ),
                )

    def pending(self, guild_id):
        return self.db.execute(
            "SELECT * FROM birthday_announcements WHERE guild_id=? "
            "AND state IN ('queued', 'sending', 'uncertain') ORDER BY created_at, id",
            (guild_id,),
        ).fetchall()

    def celebrants(self, identifier):
        return self.db.execute(
            "SELECT * FROM birthday_celebrants WHERE announcement_id=? ORDER BY discord_id",
            (identifier,),
        ).fetchall()

    def content(self, identifier, content):
        with self.db:
            self.db.execute(
                "UPDATE birthday_announcements SET content=? WHERE id=? AND state='queued'",
                (content, identifier),
            )

    def destination(self, identifier, channel_id):
        with self.db:
            self.db.execute(
                "UPDATE birthday_announcements SET channel_id=? WHERE id=? AND state='queued'",
                (channel_id, identifier),
            )

    def state(self, identifier, state, *, now=None, message_id=None):
        with self.db:
            self.db.execute(
                "UPDATE birthday_announcements SET state=?, "
                "attempted_at=coalesce(attempted_at, ?), message_id=coalesce(?, message_id) "
                "WHERE id=?",
                (state, now, message_id, identifier),
            )
