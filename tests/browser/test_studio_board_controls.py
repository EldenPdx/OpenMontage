"""Version-bound browser controls; real API and durable PostgreSQL decisions."""

from hashlib import sha256
import json
from datetime import datetime, timezone

from playwright.sync_api import expect
import pytest

from lib.config_model import PiProfile
from production.contracts import ApprovalBinding, ApprovalOption, ApprovalRequest, ApprovalScope, FileReference, TaskCreate, TaskState, canonical_sha256
from production.pi_config import snapshot_for
from tests.browser.test_studio_submission import studio_browser
from tests.fixtures.studio.repository import test_repository as isolated_repository
from tests.contracts.test_phase0_contracts import sample_artifact


def pending_gate(repo, projects_dir, *, stage="model_cost", version=1, summary="Authorize one unquoted model request"):
    profile = PiProfile(provider="xvan",base_url="http://127.0.0.1:9999/v1",model="xvan-model",
                        credential_env="BROWSER_MODEL_KEY",reasoning=False,thinking_level="off")
    request = TaskCreate(brief="A short lighthouse video")
    task = repo.create_task(request,snapshot_for(profile,request),"browser-gate-create")
    claim = repo.claim_command("browser-gate-worker")
    task = repo.transition(task.task_id,TaskState.RUNNING,expected_version=task.version,fence=claim.context.fence)
    root = projects_dir / task.project_id
    (root / "artifacts").mkdir(parents=True,exist_ok=True)
    artifact_name = {"proposal":"proposal_packet","script":"script","scene_plan":"scene_plan","assets":"asset_manifest"}.get(stage)
    artifact = sample_artifact(artifact_name) if artifact_name else {"summary":summary,"version":version}
    evidence = json.dumps(artifact).encode()
    path = f"artifacts/{artifact_name or 'model-cost'}.json"
    (root / path).write_bytes(evidence)
    digest = sha256(evidence).hexdigest()
    reference = FileReference(path=path,revision=version,sha256=digest)
    scope = ApprovalScope(provider=profile.provider,model=profile.model,budget_usd_micros=task.request.budget_usd_micros,
                          configuration_sha256=task.config_snapshot.configuration_sha256,unknown_price=True,authorized_limit_usd_micros=250000)
    checkpoint = reference
    if artifact_name:
        checkpoint_path = f"checkpoint_{stage}.json"
        value = {"version":"1.0","project_id":task.project_id,"pipeline_type":"animated-explainer","stage":stage,
                 "status":"awaiting_human","timestamp":datetime.now(timezone.utc).isoformat(),"artifacts":{artifact_name:artifact}}
        (root / checkpoint_path).write_text(json.dumps(value),encoding="utf-8")
        checkpoint = FileReference(path=checkpoint_path,revision=version,sha256=sha256((root / checkpoint_path).read_bytes()).hexdigest())
        (root / "project.json").write_text(json.dumps({"version":"1.0","project_id":task.project_id,"title":"Lighthouse browser fixture","pipeline_type":"animated-explainer"}))
        scope = scope.model_copy(update={"render_runtime":"ffmpeg","unknown_price":False,"authorized_limit_usd_micros":None})
    gate = ApprovalRequest(binding=ApprovalBinding(task_id=task.task_id,run_id=task.run_id,gate_id="model-cost-gate",
                           checkpoint_revision=version,checkpoint_sha256=checkpoint.sha256,artifact_revision=version,artifact_sha256=digest,
                           scope_sha256=canonical_sha256(scope)),stage=stage,artifact=reference,checkpoint=checkpoint,scope=scope,summary=summary,
                           options=[ApprovalOption(option_id="ffmpeg",label="Current FFmpeg runtime"),ApprovalOption(option_id="hyperframes",label="Use HyperFrames")] if artifact_name else [])
    task = repo.put_gate(gate,expected_version=task.version,fence=claim.context.fence)
    repo.finish_command(claim)
    return task


@pytest.mark.parametrize("stage", ["model_cost", "media_cost"])
def test_browser_approves_current_cost_gate_once_with_exact_binding_and_no_fake_zero(tmp_path,stage):
    with isolated_repository() as repo:
        task = pending_gate(repo,tmp_path,stage=stage)
        with studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(base_url+f"/p/{task.project_id}?task={task.task_id}")
            expect(page.locator("#studio-controls")).to_be_visible(timeout=2000)
            expect(page.locator("#studio-controls")).to_contain_text("price is unquoted")
            expect(page.locator("#studio-controls")).to_contain_text("$0.25")
            expect(page.locator("#studio-controls")).to_contain_text("not a hard limit")
            expect(page.locator(".slate .cost b")).to_have_text("Unquoted")
            page.locator("#approve-gate").dblclick()
            expect(page.locator("#studio-control-heading")).to_have_text("Queued")
            assert repo.get_task(task.task_id).approval.status=="approved"
            assert repo.get_task(task.task_id).approval.binding==task.approval.binding
            page.reload()
            expect(page.locator("#studio-control-heading")).to_have_text("Queued")
            assert page.locator("#approve-gate").count()==0
            assert not errors


@pytest.mark.parametrize("stage", ["proposal","script","scene_plan","assets"])
def test_canonical_production_gates_display_reviewable_artifacts_and_approve_exact_versions(tmp_path,stage):
    with isolated_repository() as repo:
        task = pending_gate(repo,tmp_path,stage=stage,summary=f"Review this {stage} before continuing")
        with studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
            page.goto(base_url+f"/p/{task.project_id}")
            expect(page.locator("#studio-controls")).to_be_visible()
            expect(page.locator(".approval-review")).to_be_visible()
            expect(page.locator("#studio-controls")).to_contain_text("Artifact revision 1")
            page.locator("#approve-gate").click()
            expect(page.locator("#studio-control-heading")).to_have_text("Queued")
            assert repo.get_task(task.task_id).approval.status=="approved"


def test_runtime_change_requires_revision_and_feedback_is_plain_text(tmp_path):
    with isolated_repository() as repo:
        task = pending_gate(repo,tmp_path,stage="script",summary="Review <script>window.injected=1</script>")
        with studio_browser(repo,projects_dir=tmp_path,viewport={"width":390,"height":844}) as (page,base_url):
            page.goto(base_url+f"/p/{task.project_id}")
            expect(page.locator("#studio-controls")).to_be_visible()
            page.get_by_role("checkbox",name="Use HyperFrames",exact=True).check()
            expect(page.locator("#approve-gate")).to_be_disabled()
            feedback="<img src=x onerror=window.injected=1> Use a closer view."
            page.locator("#revision-comment").fill(feedback)
            page.locator("#revise-gate").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#studio-control-heading")).to_have_text("Queued")
            assert repo.get_task(task.task_id).approval.status=="revised"
            claim=repo.claim_command("browser-revision-check")
            decision=claim.command.payload["decision"]
            assert decision["comment"]==feedback
            assert decision["selected_option_ids"]==["hyperframes"]
            assert page.evaluate("window.injected || 0")==0
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            repo.finish_command(claim)


def test_changed_artifact_is_not_approved_and_current_gate_is_refetched(tmp_path):
    with isolated_repository() as repo:
        task = pending_gate(repo,tmp_path)
        with studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
            def change_before_submit(route):
                (tmp_path/task.project_id/task.approval.artifact.path).write_text('{"changed":true}',encoding="utf-8")
                route.continue_()
            page.route("**/approvals/*/decision",change_before_submit)
            page.goto(base_url+f"/p/{task.project_id}")
            page.locator("#approve-gate").click()
            expect(page.locator("#studio-control-status")).to_contain_text("version changed")
            assert repo.get_task(task.task_id).approval.status=="pending"
            assert repo.get_task(task.task_id).state==TaskState.AWAITING_APPROVAL


def test_recovery_requires_reconciliation_and_never_turns_unknown_submission_into_resume(tmp_path):
    from production.contracts import CallIntent, ErrorDTO

    with isolated_repository() as repo:
        profile=PiProfile(provider="xvan",base_url="http://127.0.0.1:9999/v1",model="xvan-model",credential_env="BROWSER_MODEL_KEY",reasoning=False,thinking_level="off")
        request=TaskCreate(brief="A lighthouse with an unknown remote submission")
        task=repo.create_task(request,snapshot_for(profile,request),"browser-recovery-create")
        claim=repo.claim_command("browser-recovery-worker")
        task=repo.transition(task.task_id,TaskState.RUNNING,expected_version=task.version,fence=claim.context.fence)
        intent=repo.reserve_call(CallIntent(call_id="lost-submit",task_id=task.task_id,run_id=task.run_id,fence=claim.context.fence,
                               kind="tool",operation="generate",provider="local",model="test-video",request_sha256="a"*64,
                               price_status="quoted",reserved_usd_micros=100000))
        repo.update_call(intent.model_copy(update={"status":"outcome_unknown"}))
        task=repo.get_task(task.task_id)
        task=repo.transition(task.task_id,TaskState.RECOVERY_REQUIRED,expected_version=task.version,fence=claim.context.fence,
                            updates={"error":ErrorDTO(code="outcome_unknown",message="Remote acceptance has no reliable receipt",recovery_actions=["reconcile","cancel"])})
        repo.finish_command(claim)
        with studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
            page.goto(base_url+f"/p/{task.project_id}")
            expect(page.locator("#studio-control-heading")).to_have_text("Recovery required")
            expect(page.locator("#studio-controls")).to_contain_text("Unknown paid submissions will not be sent again")
            assert page.locator("#resume-task").count()==0
            page.locator("#refresh-recovery").click()
            assert repo.get_call("lost-submit").status=="outcome_unknown"
            assert repo.get_task(task.task_id).cost.reserved_usd_micros==100000


def test_legacy_project_keeps_artifacts_readable_and_has_no_studio_actions(tmp_path):
    from lib.checkpoint import init_project

    init_project("legacy-browser",title="Legacy browser project",pipeline_type="cinematic",pipeline_dir=tmp_path)
    with isolated_repository() as repo, studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
        page.goto(base_url+"/p/legacy-browser")
        expect(page.locator(".slate h1")).to_have_text("Legacy browser project")
        assert page.locator("#studio-controls").count()==0
        assert page.locator("#approve-gate").count()==0
        assert page.locator("#cancel-task").count()==0
        page.goto(base_url+"/")
        expect(page.locator("#studio-link")).to_be_visible()


def test_known_job_recovery_shows_receipt_and_resume_only_requeues_existing_work(tmp_path):
    from production.contracts import CallIntent,ErrorDTO

    with isolated_repository() as repo:
        profile=PiProfile(provider="xvan",base_url="http://127.0.0.1:9999/v1",model="xvan-model",credential_env="BROWSER_MODEL_KEY",reasoning=False,thinking_level="off")
        request=TaskCreate(brief="A lighthouse with a known remote job")
        task=repo.create_task(request,snapshot_for(profile,request),"browser-known-job")
        claim=repo.claim_command("browser-known-worker")
        task=repo.transition(task.task_id,TaskState.RUNNING,expected_version=task.version,fence=claim.context.fence)
        intent=repo.reserve_call(CallIntent(call_id="known-job-call",task_id=task.task_id,run_id=task.run_id,fence=claim.context.fence,
                               kind="tool",operation="generate",provider="local",request_sha256="b"*64,price_status="quoted",reserved_usd_micros=100000))
        repo.update_call(intent.model_copy(update={"status":"receipted","external_job_id":"remote-job-existing","resume_reference":{"job_id":"remote-job-existing"}}))
        task=repo.get_task(task.task_id)
        task=repo.transition(task.task_id,TaskState.BLOCKED,expected_version=task.version,fence=claim.context.fence,
                            updates={"error":ErrorDTO(code="rpc_error",message="The local worker stopped after submission",recovery_actions=["resume","cancel"])})
        repo.finish_command(claim)
        with studio_browser(repo,projects_dir=tmp_path) as (page,base_url):
            page.goto(base_url+f"/p/{task.project_id}")
            expect(page.locator("#studio-controls")).to_contain_text("remote-job-existing")
            page.locator("#resume-task").click()
            expect(page.locator("#studio-control-heading")).to_have_text("Queued")
            assert repo.get_call("known-job-call").external_job_id=="remote-job-existing"
            assert len(repo.unresolved_intents(task.task_id))==1
