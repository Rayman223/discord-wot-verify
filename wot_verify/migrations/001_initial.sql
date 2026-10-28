CREATE TABLE IF NOT EXISTS links (
    discord_id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    synced_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(region, account_id)
);
CREATE TABLE IF NOT EXISTS challenges (
    nonce TEXT PRIMARY KEY,
    discord_id INTEGER NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS links_synced_at ON links(synced_at);
