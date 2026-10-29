CREATE TABLE IF NOT EXISTS retry_sessions (
    cookie_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL UNIQUE,
    discord_id INTEGER NOT NULL UNIQUE,
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    next_attempt INTEGER NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS retry_sessions_expires_at ON retry_sessions(expires_at);
