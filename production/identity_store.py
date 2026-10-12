"""Private PostgreSQL identity state; no protocol, permissions, or browser routing."""

import base64
import json
import secrets
from hmac import compare_digest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.types.json import Jsonb

from production.contracts import (
    Actor, AuthSession, ConsumedLoginInteraction, ContractViolation, ErrorDTO, IntentAcceptance, InvitationDTO,
    InvitationCreationMaterial, InvitationRecord, InvitationTransition, MemberDTO, OperationScope, OrganizationDTO,
    OrganizationIntent, OrganizationOperationResult, OrganizationOperationTarget, SafeRemoteRefs,
    SessionHandle, StoredSession, UserIdentity, WorkspaceContext,
)
from production.repository import PostgresRepository, _json, _now


SDK_KEYS = frozenset({"idToken", "accessTokenMap", "refreshToken", "signInSession", "accessToken"})


def _hash(value):
    return sha256(value.encode("utf-8")).hexdigest()


def _sdk(values):
    if not isinstance(values, dict) or values.keys() - SDK_KEYS or any(value is not None and not isinstance(value, str) for value in values.values()):
        raise ContractViolation("Invalid private SDK storage")
    if len(json.dumps(values).encode()) > 1_048_576:
        raise ContractViolation("Private SDK storage is too large")
    return values


class PostgresIdentityStore:
    def __init__(self, dsn, *, schema="studio", encryption_keys, active_key_id):
        if not encryption_keys or active_key_id not in encryption_keys or any(not isinstance(key, bytes) or len(key) != 32 for key in encryption_keys.values()):
            raise ContractViolation("Session encryption keys unavailable", "identity_unavailable")
        self.repository = PostgresRepository(dsn, schema=schema)
        self._keys = dict(encryption_keys)
        self._active_key_id = active_key_id

    def _seal(self, value, purpose, reference):
        nonce = secrets.token_bytes(12)
        key_id = self._active_key_id
        aad = json.dumps([self.repository.schema, purpose, reference, 1, key_id]).encode()
        ciphertext = AESGCM(self._keys[key_id]).encrypt(nonce, json.dumps(value, ensure_ascii=False).encode("utf-8"), aad)
        return Jsonb({"version": 1, "key_id": key_id, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()})

    def _open(self, envelope, purpose, reference):
        try:
            if envelope["version"] != 1:
                raise ValueError
            key_id = envelope["key_id"]
            aad = json.dumps([self.repository.schema, purpose, reference, 1, key_id]).encode()
            return json.loads(AESGCM(self._keys[key_id]).decrypt(base64.b64decode(envelope["nonce"], validate=True),
                              base64.b64decode(envelope["ciphertext"], validate=True), aad))
        except (InvalidTag, ValueError, KeyError, TypeError):
            raise ContractViolation("Protected identity state unavailable", "identity_unavailable") from None

    def resolve_user(self, issuer, sub):
        identity = UserIdentity(user_id="user-" + uuid4().hex, issuer=issuer, sub=sub)
        with self.repository._connection() as connection:
            connection.execute("INSERT INTO identity_users VALUES (%s,%s,%s) ON CONFLICT(issuer,sub) DO NOTHING", (identity.user_id, issuer, sub))
            return UserIdentity.model_validate(connection.execute("SELECT * FROM identity_users WHERE issuer=%s AND sub=%s", (issuer, sub)).fetchone())

    def get_user(self, user_id):
        with self.repository._connection() as connection:
            row = connection.execute("SELECT * FROM identity_users WHERE user_id=%s", (user_id,)).fetchone()
            if row is None:
                raise ContractViolation("Identity not found", "not_found")
            return UserIdentity.model_validate(row)

    def remember_verified_organization(self, organization_id, name):
        workspace = WorkspaceContext(kind="organization", owner_id=organization_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ContractViolation("Invalid organization name")
        with self.repository._connection() as connection:
            connection.execute("INSERT INTO known_organizations VALUES (%s,%s) ON CONFLICT(organization_id) DO UPDATE SET name=excluded.name", (workspace.owner_id, name))

    def _identity(self, connection, identity, workspace):
        identity, workspace = UserIdentity.model_validate(identity), WorkspaceContext.model_validate(workspace)
        Actor(identity=identity, workspace=workspace)
        row = connection.execute("SELECT * FROM identity_users WHERE user_id=%s", (identity.user_id,)).fetchone()
        if row is None or UserIdentity.model_validate(row) != identity:
            raise ContractViolation("Identity is unknown or changed", "forbidden")
        if workspace.kind == "organization" and connection.execute("SELECT 1 FROM known_organizations WHERE organization_id=%s", (workspace.owner_id,)).fetchone() is None:
            raise ContractViolation("Organization not found", "not_found")
        return identity, workspace

    def _materialize(self, row):
        value = self._open(row["storage"], "session", row["session_hash"])
        metadata = AuthSession(session_ref=row["session_hash"], identity=row["identity"], workspace=row["workspace"],
                               context_id=row["context_id"], csrf_hash=row["csrf_hash"], created_at=row["created_at"],
                               absolute_expires_at=row["absolute_expires_at"], idle_expires_at=row["idle_expires_at"],
                               revision=row["revision"], authority_verified_at=row["authority_verified_at"])
        return StoredSession(metadata, _sdk(value["sdk_storage"]), value["csrf_token"])

    def _session(self, connection, cookie, *, lock=False):
        if not isinstance(cookie, str) or not 43 <= len(cookie) <= 128 or not all(character.isascii() and (character.isalnum() or character in "_-") for character in cookie):
            return None
        if lock:
            # Materialization keeps expiry checks after row-lock waits.
            query = "WITH locked_session AS MATERIALIZED (SELECT * FROM auth_sessions WHERE session_hash=%s FOR UPDATE) SELECT * FROM locked_session WHERE "
        else:
            query = "SELECT * FROM auth_sessions WHERE session_hash=%s AND "
        return connection.execute(query + "revoked_at IS NULL AND absolute_expires_at>clock_timestamp() AND idle_expires_at>clock_timestamp()", (_hash(cookie),)).fetchone()

    def _insert_session(self, connection, identity, workspace, sdk_storage, *, absolute_seconds=86400, idle_seconds=28800, absolute_expires_at=None, created_at=None):
        if isinstance(absolute_seconds, bool) or isinstance(idle_seconds, bool) or not isinstance(absolute_seconds, int) or not isinstance(idle_seconds, int) or not 1 <= absolute_seconds <= 86400 or not 1 <= idle_seconds <= 28800:
            raise ContractViolation("Invalid session lifetime")
        cookie, csrf, context = [secrets.token_urlsafe(32) for _ in range(3)]
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        absolute = absolute_expires_at or now + timedelta(seconds=absolute_seconds)
        idle = min(absolute, now + timedelta(seconds=idle_seconds))
        payload = self._seal({"sdk_storage": _sdk(sdk_storage), "csrf_token": csrf}, "session", _hash(cookie))
        row = connection.execute("INSERT INTO auth_sessions(session_hash,user_id,identity,workspace,context_id,csrf_hash,storage,absolute_expires_at,idle_expires_at,idle_seconds) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                                 (_hash(cookie), identity.user_id, _json(identity), _json(workspace), context, _hash(csrf), payload, absolute, idle, idle_seconds)).fetchone()
        if created_at is not None:
            connection.execute("UPDATE auth_sessions SET created_at=%s WHERE session_hash=%s", (created_at, row["session_hash"]))
            row["created_at"] = created_at
        return SessionHandle(cookie, self._materialize(row))

    def create_session(self, identity, workspace, *, sdk_storage, absolute_seconds=86400, idle_seconds=28800):
        with self.repository._connection() as connection:
            identity, workspace = self._identity(connection, identity, workspace)
            return self._insert_session(connection, identity, workspace, sdk_storage, absolute_seconds=absolute_seconds, idle_seconds=idle_seconds)

    def get_session(self, cookie):
        with self.repository._connection() as connection:
            row = self._session(connection, cookie)
            return self._materialize(row) if row else None

    def _require_session(self, connection, cookie):
        row = self._session(connection, cookie, lock=True)
        if row is None:
            raise ContractViolation("Session unavailable", "unauthenticated")
        return row

    def validate_context(self, cookie, csrf_token, context_id):
        session = self.get_session(cookie)
        if session is None:
            raise ContractViolation("Session unavailable", "unauthenticated")
        try:
            matches = isinstance(context_id, str) and compare_digest(context_id.encode("utf-8"), session.metadata.context_id.encode("utf-8"))
            if not matches:
                raise ContractViolation("Session workspace context changed", "auth_context_conflict")
            if not isinstance(csrf_token, str) or not compare_digest(_hash(csrf_token), session.metadata.csrf_hash):
                raise ContractViolation("Invalid CSRF token", "csrf_invalid")
        except UnicodeError:
            raise ContractViolation("Invalid CSRF or context token", "csrf_invalid") from None
        return session

    def touch_session(self, cookie):
        with self.repository._connection() as connection:
            row = self._require_session(connection, cookie)
            row = connection.execute("UPDATE auth_sessions SET idle_expires_at=LEAST(absolute_expires_at,clock_timestamp()+(idle_seconds * interval '1 second')) WHERE session_hash=%s RETURNING *", (row["session_hash"],)).fetchone()
            return self._materialize(row)

    def update_session(self, cookie, *, expected_revision, sdk_storage):
        with self.repository._connection() as connection:
            row = self._require_session(connection, cookie)
            if row["revision"] != expected_revision:
                raise ContractViolation("Session storage changed", "state_conflict")
            payload = self._open(row["storage"], "session", row["session_hash"])
            payload["sdk_storage"] = _sdk(sdk_storage)
            row = connection.execute("UPDATE auth_sessions SET storage=%s,revision=revision+1 WHERE session_hash=%s RETURNING *", (self._seal(payload, "session", row["session_hash"]), row["session_hash"])).fetchone()
            return self._materialize(row)

    def rotate_session(self, cookie, *, expected_revision, workspace):
        with self.repository._connection() as connection:
            row = self._require_session(connection, cookie)
            if row["revision"] != expected_revision:
                raise ContractViolation("Session storage changed", "state_conflict")
            identity, workspace = self._identity(connection, row["identity"], workspace)
            payload = self._open(row["storage"], "session", row["session_hash"])
            result = self._insert_session(connection, identity, workspace, payload["sdk_storage"], idle_seconds=row["idle_seconds"],
                                          absolute_expires_at=row["absolute_expires_at"], created_at=row["created_at"])
            connection.execute("UPDATE auth_sessions SET revoked_at=clock_timestamp(),revision=revision+1 WHERE session_hash=%s", (row["session_hash"],))
            return result

    def revoke_session(self, cookie, *, expected_revision=None):
        if expected_revision is None and not isinstance(cookie, str):
            return
        with self.repository._connection() as connection:
            if expected_revision is not None:
                row = self._session(connection, cookie, lock=True)
                if row is None or row["revision"] != expected_revision:
                    raise ContractViolation("Session context changed", "auth_context_conflict")
            connection.execute("UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,clock_timestamp()),revision=revision+1 WHERE session_hash=%s AND revoked_at IS NULL", (_hash(cookie),))

    def revoke_user_sessions(self, user_id):
        with self.repository._connection() as connection:
            return connection.execute("UPDATE auth_sessions SET revoked_at=clock_timestamp(),revision=revision+1 WHERE user_id=%s AND revoked_at IS NULL", (user_id,)).rowcount

    def record_authority_verification(self, cookie, *, expected_revision, verified_at):
        if not isinstance(verified_at, datetime) or verified_at.tzinfo is None:
            raise ContractViolation("Invalid authority verification timestamp")
        with self.repository._connection() as connection:
            row = self._require_session(connection, cookie)
            if row["revision"] != expected_revision:
                raise ContractViolation("Session authority changed", "auth_context_conflict")
            if verified_at > connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]:
                raise ContractViolation("Invalid authority verification timestamp")
            row = connection.execute("UPDATE auth_sessions SET authority_verified_at=GREATEST(authority_verified_at,%s) WHERE session_hash=%s RETURNING *", (verified_at, row["session_hash"])).fetchone()
            return self._materialize(row)

    @staticmethod
    def _invalidate_authority(connection, user_ids, workspace):
        return connection.execute("UPDATE auth_sessions SET authority_verified_at=NULL,revision=revision+1 WHERE user_id=ANY(%s) AND workspace->>'kind'=%s AND workspace->>'owner_id'=%s AND revoked_at IS NULL",
                                  (user_ids, workspace.kind, workspace.owner_id)).rowcount

    def invalidate_authority(self, user_ids, workspace):
        workspace = WorkspaceContext.model_validate(workspace)
        with self.repository._connection() as connection:
            return self._invalidate_authority(connection, user_ids, workspace)

    def create_login_interaction(self, binding_cookie, *, state, nonce, code_verifier, return_to, organization_hint,
                                 sdk_storage, previous_session_cookie=None, lifetime_seconds=600):
        if isinstance(lifetime_seconds, bool) or not isinstance(lifetime_seconds, int) or not 1 <= lifetime_seconds <= 600:
            raise ContractViolation("Invalid interaction lifetime")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 2048 for value in (binding_cookie, state, nonce, code_verifier, return_to)) or not 43 <= len(binding_cookie) <= 128 or not 43 <= len(code_verifier) <= 128:
            raise ContractViolation("Invalid private login interaction")
        if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to or any(ord(character) < 32 for character in return_to):
            raise ContractViolation("Invalid private return path")
        reference = "interaction-" + uuid4().hex
        payload = {"state": state, "nonce": nonce, "code_verifier": code_verifier, "return_to": return_to,
                   "organization_hint": organization_hint, "sdk_storage": _sdk(sdk_storage)}
        with self.repository._connection() as connection:
            connection.execute("INSERT INTO login_interactions(reference,binding_hash,state_hash,storage,expires_at,previous_session_hash) VALUES (%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),%s)",
                               (reference, _hash(binding_cookie), _hash(state), self._seal(payload, "interaction", reference), lifetime_seconds,
                                _hash(previous_session_cookie) if previous_session_cookie else None))
        return reference

    def consume_login_interaction(self, binding_cookie, state):
        if not isinstance(binding_cookie, str) or not isinstance(state, str):
            raise ContractViolation("Login interaction unavailable", "unauthenticated")
        completion = secrets.token_urlsafe(32)
        with self.repository._connection() as connection:
            row = connection.execute("""WITH locked_interaction AS MATERIALIZED (
                SELECT * FROM login_interactions WHERE binding_hash=%s AND state_hash=%s FOR UPDATE)
                UPDATE login_interactions SET consumed_at=clock_timestamp(),completion_hash=%s
                WHERE reference=(SELECT reference FROM locked_interaction WHERE consumed_at IS NULL AND expires_at>clock_timestamp())
                RETURNING *""", (_hash(binding_cookie), _hash(state), _hash(completion))).fetchone()
            if row is None:
                raise ContractViolation("Login interaction unavailable", "unauthenticated")
            payload = self._open(row["storage"], "interaction", row["reference"])
            return ConsumedLoginInteraction(row["reference"], completion, **payload)

    def complete_login(self, interaction, identity, workspace, *, sdk_storage, absolute_seconds=86400, idle_seconds=28800):
        if not isinstance(interaction, ConsumedLoginInteraction):
            raise ContractViolation("Login interaction unavailable", "unauthenticated")
        with self.repository._connection() as connection:
            row = connection.execute("""WITH locked_interaction AS MATERIALIZED (
                SELECT * FROM login_interactions WHERE reference=%s FOR UPDATE)
                SELECT * FROM locked_interaction WHERE consumed_at IS NOT NULL AND completed_at IS NULL AND expires_at>clock_timestamp()""",
                                     (interaction.reference,)).fetchone()
            if row is None or not compare_digest(row["completion_hash"], _hash(interaction.completion_token)):
                raise ContractViolation("Login interaction unavailable", "unauthenticated")
            self._open(row["storage"], "interaction", row["reference"])
            identity, workspace = self._identity(connection, identity, workspace)
            result = self._insert_session(connection, identity, workspace, sdk_storage, absolute_seconds=absolute_seconds, idle_seconds=idle_seconds)
            connection.execute("UPDATE login_interactions SET completed_at=clock_timestamp() WHERE reference=%s", (row["reference"],))
            if row["previous_session_hash"]:
                connection.execute("UPDATE auth_sessions SET revoked_at=clock_timestamp(),revision=revision+1 WHERE session_hash=%s AND revoked_at IS NULL", (row["previous_session_hash"],))
            self.repository._audit(connection, Actor(identity=identity, workspace=workspace), "login", result.session.metadata.session_ref, "accepted")
            return result

    def _operation_scope(self, scope):
        return self.repository._scope(scope.operation, scope.resource_id, scope.actor) + ":" + scope.idempotency_key

    @staticmethod
    def _target(scope, target):
        target = OrganizationOperationTarget.model_validate(target)
        required = {
            "organization:create": {"name"}, "invitation:create": {"email", "role", "organization_id"},
            "invitation:cancel": {"invitation_id", "organization_id"}, "invitation:accept": {"invitation_id", "user_id", "organization_id"},
            "member:role": {"user_id", "role", "organization_id"}, "member:remove": {"user_id", "organization_id"},
        }[scope.operation]
        if set(target.model_dump(exclude_none=True)) != required:
            raise ContractViolation("Invalid organization operation target")
        if scope.operation == "organization:create" and scope.actor.workspace.kind != "personal":
            raise ContractViolation("Organization creation requires personal space", "forbidden")
        if target.organization_id and scope.actor.workspace.kind == "organization" and scope.actor.workspace.owner_id != target.organization_id:
            raise ContractViolation("Organization not found", "not_found")
        if target.organization_id and scope.actor.workspace.kind == "personal" and scope.operation != "invitation:accept":
            raise ContractViolation("Organization not found", "not_found")
        if scope.operation == "invitation:accept" and target.user_id != scope.actor.identity.user_id:
            raise ContractViolation("Invitation unavailable", "invitation_invalid")
        return target

    @staticmethod
    def _own_organization(connection, organization_id, operation_id):
        row = connection.execute("INSERT INTO organization_operation_owners VALUES (%s,%s) ON CONFLICT(organization_id) DO NOTHING RETURNING operation_id", (organization_id, operation_id)).fetchone()
        if row is None and connection.execute("SELECT operation_id FROM organization_operation_owners WHERE organization_id=%s", (organization_id,)).fetchone()["operation_id"] != operation_id:
            raise ContractViolation("Organization operation requires reconciliation", "state_conflict")

    def begin_organization_intent(self, scope, *, request_sha256, target):
        scope = OperationScope.model_validate(scope)
        target = self._target(scope, target)
        key = self._operation_scope(scope)
        with self.repository._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
            row = connection.execute("SELECT record FROM organization_intents WHERE scope=%s", (key,)).fetchone()
            if row:
                intent = OrganizationIntent.model_validate(row["record"])
                if intent.request_sha256 != request_sha256 or intent.target != target:
                    raise ContractViolation("Conflicting organization operation key", "idempotency_conflict")
                return IntentAcceptance(intent=intent, disposition="replay" if intent.status in {"committed", "failed"} else "pending")
            self.repository._remember_actor(connection, scope.actor)
            if target.organization_id and connection.execute("SELECT 1 FROM known_organizations WHERE organization_id=%s", (target.organization_id,)).fetchone() is None:
                raise ContractViolation("Organization not found", "not_found")
            intent = OrganizationIntent(operation_id="operation-" + uuid4().hex, scope=scope, target=target,
                                        organization_id=target.organization_id, request_sha256=request_sha256,
                                        created_at=_now(), updated_at=_now())
            connection.execute("INSERT INTO organization_intents(scope,operation_id,organization_id,record) VALUES (%s,%s,%s,%s)", (key, intent.operation_id, intent.organization_id, _json(intent)))
            if intent.organization_id:
                self._own_organization(connection, intent.organization_id, intent.operation_id)
            self.repository._audit(connection, scope.actor, scope.operation, scope.resource_id, "accepted", operation_id=intent.operation_id)
            return IntentAcceptance(intent=intent, disposition="new")

    def get_organization_intent(self, scope):
        scope = OperationScope.model_validate(scope)
        with self.repository._connection() as connection:
            row = connection.execute("SELECT record FROM organization_intents WHERE scope=%s", (self._operation_scope(scope),)).fetchone()
            return OrganizationIntent.model_validate(row["record"]) if row else None

    def active_organization_intent(self, actor):
        actor = Actor.model_validate(actor)
        if actor.workspace.kind != "organization":
            raise ContractViolation("Not found", "not_found")
        with self.repository._connection() as connection:
            self._identity(connection, actor.identity, actor.workspace)
            row = connection.execute("SELECT i.record FROM organization_operation_owners o JOIN organization_intents i USING(operation_id) WHERE o.organization_id=%s", (actor.workspace.owner_id,)).fetchone()
            return OrganizationIntent.model_validate(row["record"]) if row else None

    def _locked_intent(self, connection, scope, expected_revision):
        row = connection.execute("SELECT record FROM organization_intents WHERE scope=%s FOR UPDATE", (self._operation_scope(scope),)).fetchone()
        if row is None:
            raise ContractViolation("Organization operation not found", "not_found")
        intent = OrganizationIntent.model_validate(row["record"])
        if intent.revision != expected_revision or intent.status in {"committed", "failed"}:
            raise ContractViolation("Organization operation changed", "state_conflict")
        if intent.organization_id:
            owner = connection.execute("SELECT operation_id FROM organization_operation_owners WHERE organization_id=%s", (intent.organization_id,)).fetchone()
            if owner is None or owner["operation_id"] != intent.operation_id:
                raise ContractViolation("Organization operation ownership changed", "state_conflict")
        return intent

    def _save_intent(self, connection, intent):
        connection.execute("UPDATE organization_intents SET organization_id=%s,record=%s WHERE scope=%s",
                           (intent.organization_id, _json(intent), self._operation_scope(intent.scope)))

    def advance_organization_intent(self, scope, *, expected_revision, stage, status, remote_refs=None):
        scope = OperationScope.model_validate(scope)
        with self.repository._connection() as connection:
            intent = self._locked_intent(connection, scope, expected_revision)
            if status not in {"submitted", "recovery_required"}:
                raise ContractViolation("Invalid organization operation transition")
            refs = SafeRemoteRefs.model_validate(remote_refs) if remote_refs is not None else intent.remote_refs
            for field in SafeRemoteRefs.model_fields:
                old = getattr(intent.remote_refs, field)
                if old is not None and getattr(refs, field) != old:
                    raise ContractViolation("Authoritative operation references are immutable", "state_conflict")
            organization_id = intent.organization_id or refs.organization_id
            if intent.organization_id and refs.organization_id not in {None, intent.organization_id}:
                raise ContractViolation("Organization reference changed", "state_conflict")
            if organization_id and intent.organization_id is None:
                self._own_organization(connection, organization_id, intent.operation_id)
            data = intent.model_dump(mode="json")
            data.update(stage=stage, status=status, revision=intent.revision + 1, remote_refs=refs, organization_id=organization_id, updated_at=_now())
            intent = OrganizationIntent.model_validate(data)
            self._save_intent(connection, intent)
            return intent

    def complete_organization_intent(self, scope, *, expected_revision, result, affected_user_ids=(), invitation_transition=None):
        scope = OperationScope.model_validate(scope)
        result = OrganizationOperationResult.model_validate(result)
        with self.repository._connection() as connection:
            intent = self._locked_intent(connection, scope, expected_revision)
            if intent.status not in {"submitted", "recovery_required"}:
                raise ContractViolation("Remote outcome has not been submitted", "state_conflict")
            if isinstance(result.body, ErrorDTO) and (result.status_code >= 500 or result.body.code == "outcome_unknown"):
                intent = intent.model_copy(update={"result": result, "status": "recovery_required", "stage": "recovery_required",
                                                   "revision": intent.revision + 1, "updated_at": _now()})
                self._save_intent(connection, intent)
                self.repository._audit(connection, scope.actor, scope.operation, scope.resource_id, "recovery_required", operation_id=intent.operation_id)
                return intent
            status = "failed" if isinstance(result.body, ErrorDTO) else "committed"
            if isinstance(result.body, (OrganizationDTO, InvitationDTO)) and result.body.organization_id != intent.organization_id:
                raise ContractViolation("Result organization differs from operation", "state_conflict")
            if isinstance(result.body, MemberDTO) and result.body.user_id != intent.target.user_id:
                raise ContractViolation("Result member differs from operation", "state_conflict")
            if status == "committed" and scope.operation in {"invitation:accept", "invitation:cancel"} and invitation_transition is None:
                invitation_transition = InvitationTransition(invitation_id=intent.target.invitation_id,
                                                              status="accepted" if scope.operation == "invitation:accept" else "cancelled")
            if invitation_transition is not None:
                transition = InvitationTransition.model_validate(invitation_transition)
                operation = "invitation:accept" if transition.status == "accepted" else "invitation:cancel"
                if scope.operation != operation or transition.invitation_id != intent.target.invitation_id or status != "committed":
                    raise ContractViolation("Invitation transition differs from operation", "state_conflict")
                row = connection.execute("SELECT * FROM identity_invitations WHERE invitation_id=%s AND organization_id=%s AND status='pending' FOR UPDATE", (transition.invitation_id, intent.organization_id)).fetchone()
                if row is None:
                    raise ContractViolation("Invitation unavailable", "invitation_invalid")
                invitation = InvitationRecord.model_validate(row["record"])
                if transition.status == "accepted":
                    now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                    evidence = transition.accepted_at is not None and transition.accepted_user_id == scope.actor.identity.sub and transition.accepted_at <= min(row["expires_at"], now)
                    if transition.accepted_at is not None and not evidence or row["expires_at"] <= now and not evidence or invitation.intended_user_id not in {None, scope.actor.identity.user_id}:
                        raise ContractViolation("Invitation unavailable", "invitation_invalid")
                    invitation = invitation.model_copy(update={"intended_user_id": scope.actor.identity.user_id})
                invitation = invitation.model_copy(update={"status": transition.status})
                connection.execute("UPDATE identity_invitations SET status=%s,record=%s WHERE invitation_id=%s", (transition.status, _json(invitation), invitation.invitation_id))
            if intent.organization_id:
                affected = set(affected_user_ids)
                if status == "committed" and scope.operation in {"member:role", "member:remove", "invitation:accept"}:
                    affected.add(intent.target.user_id)
                self._invalidate_authority(connection, list(affected), WorkspaceContext(kind="organization", owner_id=intent.organization_id))
            intent = intent.model_copy(update={"result": result, "status": status, "stage": status, "revision": intent.revision + 1, "updated_at": _now()})
            self._save_intent(connection, intent)
            connection.execute("DELETE FROM organization_operation_owners WHERE operation_id=%s", (intent.operation_id,))
            self.repository._audit(connection, scope.actor, scope.operation, scope.resource_id, status, operation_id=intent.operation_id)
            return intent

    def reject_prepared_organization_intent(self, scope, *, expected_revision, result):
        scope = OperationScope.model_validate(scope)
        result = OrganizationOperationResult.model_validate(result)
        with self.repository._connection() as connection:
            intent = self._locked_intent(connection, scope, expected_revision)
            if intent.status != "prepared" or not isinstance(result.body, ErrorDTO) or result.status_code >= 500:
                raise ContractViolation("Only a known pre-submit denial can release a prepared operation", "state_conflict")
            intent = intent.model_copy(update={"result": result, "status": "failed", "stage": "failed",
                                               "revision": intent.revision + 1, "updated_at": _now()})
            self._save_intent(connection, intent)
            connection.execute("DELETE FROM organization_operation_owners WHERE operation_id=%s", (intent.operation_id,))
            self.repository._audit(connection, scope.actor, scope.operation, scope.resource_id, "failed", operation_id=intent.operation_id)
            return intent

    def store_invitation(self, scope, *, expected_revision, invitation, ticket, intended_user_id=None):
        scope = OperationScope.model_validate(scope)
        invitation = InvitationDTO.model_validate(invitation)
        if not isinstance(ticket, str) or not 43 <= len(ticket) <= 128 or not all(character.isascii() and (character.isalnum() or character in "_-") for character in ticket):
            raise ContractViolation("Invalid private invitation ticket")
        with self.repository._connection() as connection:
            intent = self._locked_intent(connection, scope, expected_revision)
            if scope.operation != "invitation:create" or intent.status not in {"submitted", "recovery_required"} or (invitation.organization_id, invitation.email, invitation.role) != (intent.organization_id, intent.target.email, intent.target.role) or invitation.status != "pending":
                raise ContractViolation("Invitation differs from operation", "state_conflict")
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if not now < invitation.expires_at <= now + timedelta(days=7):
                raise ContractViolation("Invalid invitation expiry")
            if intended_user_id and connection.execute("SELECT 1 FROM identity_users WHERE user_id=%s", (intended_user_id,)).fetchone() is None:
                raise ContractViolation("Identity not found", "not_found")
            record = InvitationRecord(**invitation.model_dump(), ticket_hash=_hash(ticket), intended_user_id=intended_user_id, issued_by=scope.actor)
            rows = connection.execute("SELECT record FROM identity_invitations WHERE invitation_id=%s OR ticket_hash=%s", (invitation.invitation_id, record.ticket_hash)).fetchall()
            if rows:
                if len(rows) != 1 or InvitationRecord.model_validate(rows[0]["record"]) != record:
                    raise ContractViolation("Conflicting invitation evidence", "idempotency_conflict")
                return record
            connection.execute("INSERT INTO identity_invitations VALUES (%s,%s,%s,%s,%s,%s)", (record.invitation_id, record.organization_id, record.ticket_hash, record.expires_at, record.status, _json(record)))
            return record

    def prepare_invitation_creation(self, scope, *, expected_revision, material):
        scope, material = OperationScope.model_validate(scope), InvitationCreationMaterial.model_validate(material)
        with self.repository._connection() as connection:
            self._identity(connection, scope.actor.identity, scope.actor.workspace)
            intent = self._locked_intent(connection, scope, expected_revision)
            if scope.operation != "invitation:create" or intent.status != "prepared":
                raise ContractViolation("Invalid invitation preparation", "state_conflict")
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if not now < material.expires_at <= now + timedelta(days=7):
                raise ContractViolation("Invalid invitation expiry")
            row = connection.execute("SELECT invitation_material FROM organization_intents WHERE scope=%s", (self._operation_scope(scope),)).fetchone()
            if row["invitation_material"] is not None:
                saved = InvitationCreationMaterial.model_validate(self._open(row["invitation_material"], "invitation-creation", intent.operation_id))
                if saved != material:
                    raise ContractViolation("Conflicting invitation material", "idempotency_conflict")
                return intent
            connection.execute("UPDATE organization_intents SET invitation_material=%s WHERE scope=%s", (self._seal(material.model_dump(mode="json"), "invitation-creation", intent.operation_id), self._operation_scope(scope)))
            intent = intent.model_copy(update={"revision": intent.revision + 1, "updated_at": _now()})
            self._save_intent(connection, intent)
            return intent

    def invitation_creation_material(self, scope):
        scope = OperationScope.model_validate(scope)
        with self.repository._connection() as connection:
            self._identity(connection, scope.actor.identity, scope.actor.workspace)
            row = connection.execute("SELECT record,invitation_material FROM organization_intents WHERE scope=%s", (self._operation_scope(scope),)).fetchone()
            if row is None or scope.operation != "invitation:create":
                raise ContractViolation("Organization operation not found", "not_found")
            intent = OrganizationIntent.model_validate(row["record"])
            if intent.scope != scope:
                raise ContractViolation("Organization operation not found", "not_found")
            return InvitationCreationMaterial.model_validate(self._open(row["invitation_material"], "invitation-creation", intent.operation_id)) if row["invitation_material"] is not None else None

    def invitation_for_ticket(self, ticket):
        if not isinstance(ticket, str):
            raise ContractViolation("Invitation unavailable", "invitation_invalid")
        with self.repository._connection() as connection:
            row = connection.execute("SELECT record FROM identity_invitations WHERE ticket_hash=%s AND status='pending' AND expires_at>clock_timestamp()", (_hash(ticket),)).fetchone()
            if row is None:
                raise ContractViolation("Invitation unavailable", "invitation_invalid")
            return InvitationRecord.model_validate(row["record"])

    def invitation_for_acceptance(self, scope, *, ticket, verified_email):
        scope = OperationScope.model_validate(scope)
        if scope.operation != "invitation:accept" or scope.resource_id != "invitations" or not isinstance(ticket, str) or not isinstance(verified_email, str):
            raise ContractViolation("Invitation unavailable", "invitation_invalid")
        with self.repository._connection() as connection:
            self._identity(connection, scope.actor.identity, scope.actor.workspace)
            row = connection.execute("SELECT *,expires_at>clock_timestamp() AS valid FROM identity_invitations WHERE ticket_hash=%s", (_hash(ticket),)).fetchone()
            if row is None:
                raise ContractViolation("Invitation unavailable", "invitation_invalid")
            invitation = InvitationRecord.model_validate(row["record"])
            actor = scope.actor
            if actor.workspace.kind == "organization" and actor.workspace.owner_id != invitation.organization_id:
                raise ContractViolation("Not found", "not_found")
            operation = connection.execute("SELECT record FROM organization_intents WHERE scope=%s", (self._operation_scope(scope),)).fetchone()
            intent = OrganizationIntent.model_validate(operation["record"]) if operation else None
            same = intent is not None and (intent.target.invitation_id, intent.target.organization_id, intent.target.user_id) == (invitation.invitation_id, invitation.organization_id, actor.identity.user_id)
            if invitation.status == "accepted" and same and intent.status == "committed" and invitation.intended_user_id == actor.identity.user_id:
                return invitation
            if invitation.status != "pending" or invitation.email.casefold() != verified_email.casefold() or invitation.intended_user_id not in {None, actor.identity.user_id}:
                raise ContractViolation("Invitation unavailable", "invitation_invalid")
            if not row["valid"] and not (same and intent.status in {"submitted", "recovery_required"}):
                raise ContractViolation("Invitation unavailable", "invitation_invalid")
            return invitation

    def get_invitation(self, actor, invitation_id):
        actor = Actor.model_validate(actor)
        if actor.workspace.kind != "organization":
            raise ContractViolation("Not found", "not_found")
        with self.repository._connection() as connection:
            self._identity(connection, actor.identity, actor.workspace)
            row = connection.execute("SELECT record,expires_at<=clock_timestamp() AS expired FROM identity_invitations WHERE invitation_id=%s AND organization_id=%s", (invitation_id, actor.workspace.owner_id)).fetchone()
            if row is None:
                raise ContractViolation("Not found", "not_found")
            invitation = InvitationRecord.model_validate(row["record"])
            return invitation.model_copy(update={"status": "expired"}) if row["expired"] and invitation.status == "pending" else invitation

    def list_invitations(self, actor, *, after_id=None, limit=101):
        actor = Actor.model_validate(actor)
        if actor.workspace.kind != "organization":
            raise ContractViolation("Not found", "not_found")
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ContractViolation("Invalid invitation pagination")
        with self.repository._connection() as connection:
            self._identity(connection, actor.identity, actor.workspace)
            rows = connection.execute("SELECT record,expires_at<=clock_timestamp() AS expired FROM identity_invitations WHERE organization_id=%s AND (%s::text IS NULL OR invitation_id>%s) ORDER BY invitation_id LIMIT %s", (actor.workspace.owner_id, after_id, after_id, limit)).fetchall()
            result = []
            for row in rows:
                invitation = InvitationRecord.model_validate(row["record"])
                result.append(invitation.model_copy(update={"status": "expired"}) if row["expired"] and invitation.status == "pending" else invitation)
            return result

    def storage(self, cookie):
        return SessionStorage(self, cookie)

    def _storage_key(self, cookie, key, *, value=None, write=False):
        if key not in SDK_KEYS or (value is not None and not isinstance(value, str)):
            raise ContractViolation("Invalid private SDK storage key")
        with self.repository._connection() as connection:
            row = self._session(connection, cookie, lock=write)
            if row is None:
                raise ContractViolation("Session unavailable", "unauthenticated")
            payload = self._open(row["storage"], "session", row["session_hash"])
            if not write:
                return payload["sdk_storage"].get(key)
            if value is None:
                payload["sdk_storage"].pop(key, None)
            else:
                payload["sdk_storage"][key] = value
            _sdk(payload["sdk_storage"])
            connection.execute("UPDATE auth_sessions SET storage=%s,revision=revision+1 WHERE session_hash=%s",
                               (self._seal(payload, "session", row["session_hash"]), row["session_hash"]))


class SessionStorage:
    """A fresh SDK-compatible storage view bound to exactly one opaque cookie."""

    def __init__(self, store, cookie):
        self._store, self._cookie = store, cookie

    def get(self, key):
        return self._store._storage_key(self._cookie, key)

    def set(self, key, value):
        self._store._storage_key(self._cookie, key, value=value, write=True)

    def delete(self, key):
        self.set(key, None)
