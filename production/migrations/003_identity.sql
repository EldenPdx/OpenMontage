ALTER TABLE tasks ADD COLUMN workspace_kind text;
ALTER TABLE tasks ADD COLUMN owner_id text;
ALTER TABLE tasks ADD CONSTRAINT task_workspace_pair CHECK (
    (workspace_kind IS NULL AND owner_id IS NULL) OR
    (workspace_kind IN ('personal','organization') AND owner_id IS NOT NULL)
);
CREATE INDEX workspace_tasks ON tasks(workspace_kind,owner_id,task_id);

CREATE TABLE identity_binding (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    binding_id uuid NOT NULL DEFAULT gen_random_uuid()
);
INSERT INTO identity_binding DEFAULT VALUES;

CREATE TABLE identity_users (
    user_id text PRIMARY KEY,
    issuer text NOT NULL,
    sub text NOT NULL,
    UNIQUE(issuer,sub)
);
CREATE TABLE project_owners (
    project_id text PRIMARY KEY,
    workspace_kind text,
    owner_id text,
    ownership jsonb,
    CHECK ((workspace_kind IS NULL AND owner_id IS NULL AND ownership IS NULL) OR
           (workspace_kind IN ('personal','organization') AND owner_id IS NOT NULL AND ownership IS NOT NULL))
);
INSERT INTO project_owners(project_id) SELECT project_id FROM tasks;
CREATE INDEX workspace_projects ON project_owners(workspace_kind,owner_id,project_id);
CREATE TABLE identity_audit (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id text NOT NULL,
    workspace_kind text NOT NULL,
    owner_id text NOT NULL,
    record jsonb NOT NULL
);
CREATE INDEX workspace_audit ON identity_audit(workspace_kind,owner_id,operation_id,sequence);

ALTER TABLE commands ADD COLUMN ownership jsonb;
ALTER TABLE commands ADD COLUMN actor jsonb;
ALTER TABLE commands ADD COLUMN request_scope text NOT NULL DEFAULT '';
ALTER TABLE commands ADD CONSTRAINT command_identity_pair CHECK ((ownership IS NULL) = (actor IS NULL));
ALTER TABLE commands DROP CONSTRAINT commands_task_id_idempotency_key_key;
ALTER TABLE commands ADD CONSTRAINT scoped_command_key UNIQUE(task_id,request_scope,idempotency_key);
ALTER TABLE runs ADD COLUMN actor jsonb;
ALTER TABLE gates ADD COLUMN decision_actor jsonb;
