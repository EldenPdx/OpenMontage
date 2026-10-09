CREATE TABLE calls (
    call_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES tasks(task_id),
    run_id text NOT NULL REFERENCES runs(run_id),
    request_sha256 text NOT NULL,
    record jsonb NOT NULL,
    reserved_usd_micros bigint NOT NULL DEFAULT 0 CHECK (reserved_usd_micros >= 0),
    actual_usd_micros bigint CHECK (actual_usd_micros >= 0),
    status text NOT NULL CHECK (status IN ('reserved','prepared','submitted','receipted','settled','failed','outcome_unknown'))
);
CREATE INDEX task_calls ON calls(task_id);
CREATE TABLE file_intents (
    intent_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES tasks(task_id),
    record jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('prepared','applied','reconciled','conflict'))
);
