"""An unresolved submission blocks fresh paid intents at the atomic admission seam."""

import pytest

from production.contracts import ContractViolation
from tests.integration.test_studio_policy import intent, running
from tests.integration.test_studio_repository import repository, repository_factory


def test_unknown_submission_with_remaining_budget_blocks_a_fresh_model_reservation(repository, tmp_path):
    _, context, _ = running(repository, tmp_path)
    original = repository.reserve_call(intent(context, "lost-submission").model_copy(update={"reserved_usd_micros": 500_000}))
    submitted = repository.update_call(original.model_copy(update={"status": "submitted"}))
    unknown = repository.update_call(submitted.model_copy(update={"status": "outcome_unknown"}))
    before = repository.get_task(context.task_id).cost
    assert before.reserved_usd_micros == 500_000 and before.spent_usd_micros == 0
    assert before.budget_usd_micros - before.reserved_usd_micros > 500_000
    assert unknown.external_job_id is None and context.config_snapshot.max_turns > 2
    assert repository.reserve_call(original) == unknown, "The same ID still replays its original receipt without reserving again"

    fresh = intent(context, "fresh-summary-request").model_copy(update={"reserved_usd_micros": 500_000})
    with pytest.raises(ContractViolation) as refusal:
        repository.reserve_call(fresh)
    assert refusal.value.code == "outcome_unknown"
    assert repository.get_task(context.task_id).cost == before
    assert repository.get_call(original.call_id) == unknown
