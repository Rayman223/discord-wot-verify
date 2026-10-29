CREATE TABLE IF NOT EXISTS clan_contact_notices (
    discord_id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    clan_id INTEGER NOT NULL,
    clan_tag TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
