CREATE TABLE tasks (
    task_id text PRIMARY KEY,
    project_id text NOT NULL UNIQUE,
    run_id text NOT NULL UNIQUE,
    version bigint NOT NULL CHECK (version > 0),
    state text NOT NULL CHECK (state IN ('queued','running','awaiting_approval','blocked','cancel_requested','cancelled','succeeded','failed','recovery_required')),
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE runs (
    run_id text PRIMARY KEY REFERENCES tasks(run_id),
    task_id text NOT NULL REFERENCES tasks(task_id),
    project_id text NOT NULL,
    attempt bigint NOT NULL DEFAULT 1 CHECK (attempt > 0),
    fence bigint NOT NULL DEFAULT 1 CHECK (fence > 0),
    session jsonb NOT NULL,
    lease_worker text,
    lease_expires_at timestamptz,
    active boolean NOT NULL DEFAULT true,
    CHECK ((lease_worker IS NULL) = (lease_expires_at IS NULL))
);
CREATE UNIQUE INDEX one_active_project_run ON runs(project_id) WHERE active;
CREATE TABLE requests (
    scope text NOT NULL,
    idempotency_key text NOT NULL,
    fingerprint text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE TABLE commands (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command_id text NOT NULL UNIQUE,
    task_id text NOT NULL REFERENCES tasks(task_id),
    run_id text NOT NULL REFERENCES runs(run_id),
    kind text NOT NULL CHECK (kind IN ('start','continue','revise','cancel','resume')),
    expected_version bigint NOT NULL CHECK (expected_version > 0),
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','claimed','completed','failed')),
    fingerprint text NOT NULL,
    UNIQUE (task_id, idempotency_key),
    UNIQUE (run_id, kind, expected_version)
);
CREATE INDEX pending_commands ON commands(sequence) WHERE status = 'pending';
CREATE TABLE events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id text NOT NULL REFERENCES tasks(task_id),
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX task_events ON events(task_id, event_id);
CREATE TABLE gates (
    task_id text NOT NULL REFERENCES tasks(task_id),
    run_id text NOT NULL REFERENCES runs(run_id),
    gate_id text NOT NULL,
    request jsonb NOT NULL,
    decision jsonb,
    status text NOT NULL CHECK (status IN ('pending','approved','revised','rejected','aborted','superseded')),
    consumed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (task_id, run_id, gate_id)
);
