CREATE TABLE known_organizations (
    organization_id text PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE auth_sessions (
    session_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES identity_users(user_id),
    identity jsonb NOT NULL,
    workspace jsonb NOT NULL,
    context_id text NOT NULL,
    csrf_hash text NOT NULL,
    storage jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    absolute_expires_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL,
    idle_seconds integer NOT NULL CHECK (idle_seconds>0 AND idle_seconds<=28800),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    revoked_at timestamptz,
    authority_verified_at timestamptz,
    CHECK (idle_expires_at<=absolute_expires_at)
);
CREATE INDEX user_sessions ON auth_sessions(user_id);
REVOKE ALL ON auth_sessions FROM PUBLIC;

CREATE TABLE login_interactions (
    reference text PRIMARY KEY,
    binding_hash text NOT NULL,
    state_hash text NOT NULL,
    storage jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    previous_session_hash text,
    consumed_at timestamptz,
    completion_hash text,
    completed_at timestamptz,
    UNIQUE(binding_hash,state_hash)
);
REVOKE ALL ON login_interactions FROM PUBLIC;
