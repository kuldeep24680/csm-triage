-- Short-lived access tokens (15 min) + revocable refresh tokens.
--
-- The refresh token is a random 256-bit value sent only as an httpOnly cookie.
-- Only its sha256 is stored, so a database leak hands out no working tokens.
-- Every refresh rotates the token (old row revoked, replaced_by set). Reusing
-- a revoked token means it was stolen: the whole family (one sign-in) is revoked.
CREATE TABLE refresh_tokens (
    token_hash   text PRIMARY KEY,
    family_id    uuid NOT NULL,
    user_id      text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    tenant_id    text NOT NULL REFERENCES tenants(tenant_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    replaced_by  text
);
CREATE INDEX ON refresh_tokens (family_id);
CREATE INDEX ON refresh_tokens (user_id);

-- Read only by the owner role (sign-in happens before a tenant is known), like users.
ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_all ON refresh_tokens TO CURRENT_USER USING (true) WITH CHECK (true);
