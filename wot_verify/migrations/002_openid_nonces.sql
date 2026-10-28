CREATE TABLE IF NOT EXISTS openid_nonces (
    nonce TEXT PRIMARY KEY,
    used_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS openid_nonces_used_at ON openid_nonces(used_at);
