CREATE TABLE IF NOT EXISTS account_birthdays (
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    created_at INTEGER CHECK (created_at > 0),
    retry_after INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (region, account_id)
);

-- Retained after unlinking so failed Discord cleanup remains recoverable.
CREATE TABLE IF NOT EXISTS birthday_decorations (
    guild_id INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS birthday_announcements (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    attempted_at INTEGER,
    message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'sending', 'sent', 'uncertain', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS birthday_celebrants (
    guild_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    discord_id INTEGER NOT NULL,
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    announcement_id TEXT NOT NULL REFERENCES birthday_announcements(id),
    PRIMARY KEY (guild_id, day, discord_id),
    UNIQUE (guild_id, day, region, account_id)
);
CREATE INDEX IF NOT EXISTS birthday_announcements_pending
    ON birthday_announcements (guild_id, state, day);
