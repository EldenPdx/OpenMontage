"""Configured login lifetimes at the agreed real PostgreSQL storage seam."""

import time

import pytest

from production.contracts import WorkspaceContext
from tests.integration.test_identity_persistence import identity_store
from tests.integration.test_studio_repository import repository, repository_factory


@pytest.mark.parametrize("absolute_seconds,idle_seconds", [(1, 30), (30, 1)])
def test_completed_login_expires_at_the_configured_absolute_or_idle_limit(repository, absolute_seconds, idle_seconds):
    store = identity_store(repository)
    user = store.resolve_user("https://identity.test/oidc", "short-login")
    workspace = WorkspaceContext(kind="personal", owner_id=user.user_id)
    binding, state = "b" * 43, "configured-lifetime"
    store.create_login_interaction(binding, state=state, nonce="bound-nonce", code_verifier="v" * 43,
                                   return_to="/en", organization_hint=None, sdk_storage={})
    interaction = store.consume_login_interaction(binding, state)
    completed = store.complete_login(interaction, user, workspace, sdk_storage={},
                                     absolute_seconds=absolute_seconds, idle_seconds=idle_seconds)
    assert store.get_session(completed.cookie) is not None
    time.sleep(1.1)
    assert store.get_session(completed.cookie) is None
