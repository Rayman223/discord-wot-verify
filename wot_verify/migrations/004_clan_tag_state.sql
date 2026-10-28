CREATE TABLE IF NOT EXISTS clan_tag_state (
    discord_id INTEGER NOT NULL,
    suffix TEXT NOT NULL,
    PRIMARY KEY (discord_id, suffix)
);
