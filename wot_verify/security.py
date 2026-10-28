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

    def issue(self, discord_id, channel_id=0):
        expires = int(time.time()) + 600
        nonce = secrets.token_urlsafe(24)
        payload = f"{self.guild_id}.{discord_id}.{expires}.{nonce}.{channel_id}"
        self.store.challenge(nonce, discord_id, expires)
        return f"{payload}.{self.signature(payload)}"

    def consume(self, token):
        user, _ = self.consume_details(token)
        return user

    def inspect(self, token):
        """Validate signed navigation context; accept existing ten-minute links too."""
        try:
            payload, signature = token.rsplit(".", 1)
            fields = payload.split(".")
            if len(fields) == 4:
                fields.append("0")
            guild, user, expires, nonce, channel = fields
            now = int(time.time())
            if (
                len(token) > 256
                or not hmac.compare_digest(signature, self.signature(payload))
                or int(guild) != self.guild_id
                or int(expires) <= now
                or int(expires) > now + 600
                or not channel.isascii()
                or not channel.isdigit()
                or not 0 <= int(channel) < 2**64
            ):
                raise ValueError
            return int(user), int(channel), nonce
        except (ValueError, TypeError):
            raise ValueError("invalid_state") from None

    def consume_details(self, token):
        user, channel, nonce = self.inspect(token)
        if not self.store.consume(nonce, user, int(time.time())):
            raise ValueError("invalid_state")
        return user, channel
