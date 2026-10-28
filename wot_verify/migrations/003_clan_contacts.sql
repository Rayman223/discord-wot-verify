CREATE TABLE IF NOT EXISTS clan_contacts (
    discord_id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    clan_id INTEGER NOT NULL,
    UNIQUE(region, clan_id)
);
