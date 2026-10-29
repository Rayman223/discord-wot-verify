CREATE TABLE IF NOT EXISTS verification_notices (
    discord_id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
