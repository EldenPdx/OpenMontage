"""Private organization encryption and transactions at the approved real-PG seam."""

from datetime import datetime, timedelta, timezone
import secrets

import pytest

from production.contracts import Actor, ContractViolation, InvitationCreationMaterial, OperationScope, OrganizationOperationTarget, WorkspaceContext, canonical_sha256
from tests.integration.test_identity_persistence import identity_store
from tests.integration.test_studio_repository import repository, repository_factory


def test_invitation_creation_material_is_encrypted_bound_and_readable_after_key_rotation(repository):
    store = identity_store(repository)
    identity = store.resolve_user("https://identity.test/oidc", "creator")
    store.remember_verified_organization("OrgEncrypted", "Encrypted organization")
    actor = Actor(identity=identity, workspace=WorkspaceContext(kind="organization", owner_id="OrgEncrypted"))
    scope = OperationScope(actor=actor, operation="invitation:create", resource_id="OrgEncrypted", idempotency_key="encrypted-material-001")
    target = OrganizationOperationTarget(organization_id="OrgEncrypted", email="recipient@example.test", role="viewer")
    intent = store.begin_organization_intent(scope, request_sha256=canonical_sha256(target), target=target).intent
    material = InvitationCreationMaterial(ticket=secrets.token_urlsafe(32), expires_at=datetime.now(timezone.utc) + timedelta(hours=1), organization_role_id="role-viewer", prior_native_invitation_ids=("prior-invitation",))
    prepared = store.prepare_invitation_creation(scope, expected_revision=intent.revision, material=material)
    assert material.ticket not in repr(material)
    import json
    with repository._connection() as connection:
        stored = connection.execute("SELECT record,invitation_material FROM organization_intents WHERE operation_id=%s", (intent.operation_id,)).fetchone()
        assert material.ticket not in json.dumps(stored)
    restarted = identity_store(repository, key_id="new-key", keys={**store._keys, "new-key": secrets.token_bytes(32)})
    assert restarted.invitation_creation_material(scope) == material
    assert restarted.get_organization_intent(scope).revision == prepared.revision
    isolated_key = identity_store(repository, key_id="new-key", keys={"new-key": secrets.token_bytes(32)})
    with pytest.raises(ContractViolation) as failed:
        isolated_key.invitation_creation_material(scope)
    assert failed.value.code == "identity_unavailable"
    with pytest.raises(ContractViolation) as failed:
        restarted.invitation_creation_material(scope.model_copy(update={"idempotency_key": "foreign-material-001"}))
    assert failed.value.code == "not_found"
