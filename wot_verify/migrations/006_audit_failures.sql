CREATE TABLE IF NOT EXISTS audit_failures (
    discord_id INTEGER PRIMARY KEY,
    failures INTEGER NOT NULL,
    notified_at INTEGER NOT NULL DEFAULT 0
);
