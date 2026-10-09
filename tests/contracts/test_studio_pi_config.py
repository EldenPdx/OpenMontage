"""Trusted Pi profiles, isolation and actual provider payload compatibility."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lib.config_model import PiProfile, StudioConfig


def test_defaults_are_independent_from_the_text_tool_and_invalid_profiles_fail_closed():
    config = StudioConfig()
    profile = config.profiles[config.default_profile]
    assert (profile.provider, profile.model, profile.api, profile.credential_env) == (
        "xvan", "gpt-5.6-sol", "openai-responses", "NEW_API_KEY",
    )
    assert profile.max_output_tokens >= 16_384
    assert profile.price is None
    for changed in (
        {"base_url": "!curl https://attacker.invalid"},
        {"base_url": "http://public.invalid/v1"},
        {"api": "responses"}, {"credential_env": "!cat ~/.env"},
        {"context_window": 100, "max_output_tokens": 500},
        {"sampling_params": {"base_url": "https://attacker.invalid"}},
        {"header_env": {"Authorization": "!cat ~/.env"}},
    ):
        with pytest.raises(ValidationError):
            PiProfile(**changed)


def test_prepared_config_keeps_secrets_backend_only_and_drops_host_pollution(tmp_path):
    import json

    from production.contracts import RunContext, SessionReference, TaskCreate
    from production.pi_config import prepare_pi, public_profiles, snapshot_for

    profile = PiProfile(header_env={"X-Channel-Key": "CHANNEL_KEY"})
    environment = {"NEW_API_KEY": "private-key-123", "CHANNEL_KEY": "private-header-456",
                   "ANTHROPIC_API_KEY": "host-key", "PI_CODING_AGENT_DIR": "/host/.pi/agent"}
    snapshot = snapshot_for(profile, TaskCreate(brief="A lighthouse"))
    context = RunContext(task_id="task-a",project_id="project-a",run_id="run-a",fence=1,
                         config_snapshot=snapshot,session=SessionReference(path="task-a/run-a/session.jsonl"))
    managed = prepare_pi(profile, context, tmp_path, environment=environment)
    models = (managed.agent_dir / "models.json").read_text(encoding="utf-8")
    assert '"apiKey": "${NEW_API_KEY}"' in models
    assert "private-key-123" not in models
    assert "private-header-456" not in models
    assert "host-key" not in managed.env.values()
    assert managed.env["NEW_API_KEY"] == "private-key-123"
    assert managed.env["PI_CODING_AGENT_DIR"] != "/host/.pi/agent"
    assert json.loads((managed.agent_dir / "auth.json").read_text()) == {}
    assert not any(value in repr(managed) for value in ("private-key-123", "private-header-456"))
    assert not any(value in json.dumps(public_profiles(StudioConfig(), environment)) for value in environment.values())
    assert not any(arg.startswith("--session") for arg in managed.argv)
    settings = json.loads((managed.agent_dir / "settings.json").read_text())
    assert settings["cacheWarming"] == "off"
    assert settings["retry"]["provider"]["maxRetries"] == 0
    assert settings["compaction"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["openai-responses", "openai-completions"])
async def test_real_pi_consumes_second_profile_and_native_token_sampling_thinking_limits(tmp_path, api):
    import asyncio
    import json

    from production.contracts import RunContext, SessionReference, TaskCreate
    from production.pi_config import prepare_pi, snapshot_for
    from production.pi_rpc import PiRPC
    from tests.fixtures.studio.model_server import model_server

    with model_server(expected_authorization="Bearer second-private-key", expected_headers={"X-Channel-Key": "second-private-channel"}) as (base_url, requests):
        profile = PiProfile(provider="second", model="second-model", api=api, base_url=base_url,
                            credential_env="SECOND_KEY", reasoning=False, thinking_level="off",
                            input=["text"], context_window=32768, max_output_tokens=256,
                            sampling_params={"temperature": 0.35, "top_p": 0.8}, request_timeout_seconds=5,
                            header_env={"X-Channel-Key": "SECOND_CHANNEL_KEY"})
        snapshot = snapshot_for(profile, TaskCreate(brief="A lighthouse", profile_id="second"))
        context = RunContext(task_id="task-config",project_id="project-config",run_id="run-config",fence=1,
                             config_snapshot=snapshot,session=SessionReference(path="task-config/run-config.jsonl"))
        host = tmp_path / "host" / ".pi" / "agent"
        host.mkdir(parents=True)
        (host / "auth.json").write_text(json.dumps({"second":{"type":"api_key","key":"wrong-host-key"}}))
        (host / "models.json").write_text(json.dumps({"providers":{"second":{"baseUrl":"https://wrong.invalid/v1"}}}))
        (host / "settings.json").write_text(json.dumps({"defaultModel":"wrong-model","extensions":["evil.js"]}))
        managed = prepare_pi(profile, context, tmp_path / "managed", environment={
            "HOME":str(tmp_path / "host"), "PI_CODING_AGENT_DIR":str(host),
            "SECOND_KEY":"second-private-key", "NODE_OPTIONS":"--require evil.js",
            "SECOND_CHANNEL_KEY":"second-private-channel",
        })
        client = PiRPC(managed.argv, cwd=managed.work_dir, env=managed.env, session_root=managed.session_root,
                       redact_values=managed.redact_values, startup_timeout=profile.startup_timeout_seconds,
                       request_timeout=profile.request_timeout_seconds)
        await client.start(context)
        try:
            state = await client.inspect()
            assert state["model"]["provider"] == "second"
            assert state["model"]["id"] == "second-model"
            assert state["model"]["contextWindow"] == 32768
            assert state["thinkingLevel"] == "off"
            assert state["model"]["input"] == ["text"]
            await client.prompt("Reply with a short sentence.", command_id="config-prompt")
            async def settled():
                async for event in client.events():
                    if event["type"] == "agent_settled":
                        return
                raise AssertionError("Real Pi exited before settling")
            await asyncio.wait_for(settled(), timeout=15)
            assert len(requests) == 1
            body = requests[0]
            assert body["model"] == "second-model"
            assert body.get("max_output_tokens", body.get("max_tokens", body.get("max_completion_tokens"))) == 256
            assert body["temperature"] == 0.35
            assert body["top_p"] == 0.8
            assert not body.get("reasoning")
            assert "second-private-key" not in json.dumps(state)
            assert "evil.js" not in managed.env.values()
        finally:
            await client.close()


def test_snapshot_drift_unknown_prices_and_backend_precedence(tmp_path):
    from lib.config_model import PiPrice
    from production.contracts import ContractViolation, TaskCreate
    from production.pi_config import estimated_cost_usd_micros, load_studio_config, profile_for_snapshot, snapshot_for

    source = tmp_path / "config.yaml"
    source.write_text("studio:\n  enabled: false\n  default_profile: xvan\n", encoding="utf-8")
    config = load_studio_config(source, environment={"STUDIO_ENABLED":"true"})
    assert config.enabled is True
    profile = config.profiles["xvan"]
    snapshot = snapshot_for(profile, TaskCreate(brief="A lighthouse"))
    assert profile_for_snapshot(config, snapshot) == profile
    config.profiles["xvan"] = profile.model_copy(update={"model":"changed-model"})
    with pytest.raises(ContractViolation, match="changed"):
        profile_for_snapshot(config, snapshot)
    assert estimated_cost_usd_micros(profile, {"input":10,"output":5}) is None
    priced = profile.model_copy(update={"price":PiPrice(input=2_000_000,output=6_000_000,cache_read=100_000,cache_write=2_000_000)})
    assert estimated_cost_usd_micros(priced, {"input":10,"output":5}) == 50
    with pytest.raises(ContractViolation):
        estimated_cost_usd_micros(priced, {"input":-1})
    with pytest.raises(ContractViolation):
        load_studio_config(source, environment={"STUDIO_DEFAULT_PROFILE":"missing"})


def test_credential_reference_cannot_replace_isolation_environment_or_enable_uncontrolled_retries(tmp_path):
    from production.contracts import ContractViolation, RunContext, SessionReference, TaskCreate
    from production.pi_config import prepare_pi, snapshot_for

    for name in ("HOME", "PATH", "NODE_OPTIONS", "PI_CODING_AGENT_DIR", "PI_OFFLINE"):
        with pytest.raises(ValidationError):
            PiProfile(credential_env=name)
    profile = PiProfile(max_retries=1)
    snapshot = snapshot_for(profile, TaskCreate(brief="A lighthouse"))
    context = RunContext(task_id="task-a",project_id="project-a",run_id="run-a",fence=1,
                         config_snapshot=snapshot,session=SessionReference(path="task-a/run-a/session.jsonl"))
    with pytest.raises(ContractViolation, match="guarded"):
        prepare_pi(profile,context,tmp_path,environment={"NEW_API_KEY":"private-key"})
