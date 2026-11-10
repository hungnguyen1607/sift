-- API keys for auth. Only sha256 hashes are stored; keys are high-entropy
-- random bytes so no bcrypt is needed. prefix is the first 12 chars, for display.
CREATE TABLE api_keys (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,
    prefix       text NOT NULL UNIQUE,
    key_hash     text NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,
    rpm_limit    int NOT NULL DEFAULT 60,
    monthly_limit int
);
