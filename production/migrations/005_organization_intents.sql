CREATE TABLE organization_intents (
    scope text PRIMARY KEY,
    operation_id text NOT NULL UNIQUE,
    organization_id text,
    record jsonb NOT NULL
);
CREATE TABLE organization_operation_owners (
    organization_id text PRIMARY KEY,
    operation_id text NOT NULL REFERENCES organization_intents(operation_id)
);
REVOKE ALL ON organization_intents,organization_operation_owners FROM PUBLIC;

CREATE TABLE identity_invitations (
    invitation_id text PRIMARY KEY,
    organization_id text NOT NULL,
    ticket_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','accepted','cancelled','expired')),
    record jsonb NOT NULL
);
REVOKE ALL ON identity_invitations FROM PUBLIC;

CREATE TABLE ownership_import_audit (
    import_id text PRIMARY KEY,
    record jsonb NOT NULL
);
