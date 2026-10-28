import hashlib
import hmac
import secrets
import time


class States:
    def __init__(self, secret, guild_id, store):
        self.secret = secret.encode()
        self.guild_id = guild_id
        self.store = store

    def signature(self, payload):
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, discord_id):
        expires = int(time.time()) + 600
        nonce = secrets.token_urlsafe(24)
        payload = f"{self.guild_id}.{discord_id}.{expires}.{nonce}"
        self.store.challenge(nonce, discord_id, expires)
        return f"{payload}.{self.signature(payload)}"

    def consume(self, token):
        try:
            guild, user, expires, nonce, signature = token.split(".")
            payload = f"{guild}.{user}.{expires}.{nonce}"
            now = int(time.time())
            if (
                not hmac.compare_digest(signature, self.signature(payload))
                or int(guild) != self.guild_id
                or int(expires) <= now
                or int(expires) > now + 600
            ):
                raise ValueError
            if not self.store.consume(nonce, int(user), now):
                raise ValueError
            return int(user)
        except (ValueError, TypeError):
            raise ValueError("invalid_state") from None
