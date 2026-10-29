import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .i18n import SUPPORTED_LOCALES


@dataclass(frozen=True)
class Config:
    discord_token: str
    wg_app_id: str
    state_secret: str
    guild_id: int
    role_id: int
    public_url: str
    region: str = "eu"
    database: str = "/data/verify.sqlite3"
    port: int = 8080
    clan_role_id: int = 0
    locale: str = "en"
    log_channel_id: int = 0

    @classmethod
    def load(cls):
        config = cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            wg_app_id=os.environ["WG_APP_ID"],
            state_secret=os.environ["STATE_SECRET"],
            guild_id=int(os.environ["GUILD_ID"]),
            role_id=int(os.environ["ROLE_ID"]),
            public_url=os.environ["PUBLIC_URL"].rstrip("/"),
            log_channel_id=int(os.getenv("LOG_CHANNEL_ID", "0")),
            locale=os.getenv("LOCALE", "en"),
            region=os.getenv("WG_REGION", "eu"),
            database=os.getenv("DATABASE_PATH", "/data/verify.sqlite3"),
            port=int(os.getenv("PORT", "8080")),
            clan_role_id=int(os.getenv("CLAN_CONTACT_ROLE_ID", "0")),
        )
        if not 0 <= config.log_channel_id < 2**63:
            raise ValueError("LOG_CHANNEL_ID must be a positive channel ID, or 0 to disable")
        if config.locale not in SUPPORTED_LOCALES:
            raise ValueError("LOCALE must be en or fr")
        url = urlsplit(config.public_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("PUBLIC_URL must be an HTTPS origin")
        if len(config.state_secret.encode()) < 32:
            raise ValueError("STATE_SECRET must contain at least 32 bytes")
        if config.region not in {"eu", "com", "asia"}:
            raise ValueError("WG_REGION must be eu, com or asia")
        if min(config.guild_id, config.role_id) <= 0:
            raise ValueError("Discord IDs must be positive")
        if config.clan_role_id < 0 or config.clan_role_id in {config.role_id, config.guild_id}:
            raise ValueError(
                "CLAN_CONTACT_ROLE_ID must be a separate positive role ID, or 0 to disable"
            )
        return config
