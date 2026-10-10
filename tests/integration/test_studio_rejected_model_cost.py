import asyncio

import pytest

from lib.config_model import PiPrice, PiProfile
from tests.integration.test_studio_policy import intent, running
from tests.integration.test_studio_repository import repository, repository_factory


@pytest.mark.parametrize("http_status", [401, 403])
def test_quoted_model_rejection_retains_unverified_fee_reservation(repository, tmp_path, http_status):
    profile = PiProfile(provider="local", model="test-model", credential_env="LOCAL_TEST_KEY",
                        reasoning=False, thinking_level="off", context_window=1000, max_output_tokens=100,
                        price=PiPrice(input=2000, output=5000, cache_read=0, cache_write=0))
    bridge, context, _ = running(repository, tmp_path, profile)
    submitted = asyncio.run(bridge.authorize_model(intent(context)))
    assert submitted.reserved_usd_micros > 0
    received = asyncio.run(bridge.settle_model(submitted.model_copy(update={
        "status": "receipted", "usage": {"input": 0, "output": 0, "http_status": http_status}})))
    assert received.status == "receipted"
    assert received.actual_usd_micros is None
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == submitted.reserved_usd_micros
    assert repository.get_task(context.task_id).cost.price_status == "unquoted"
