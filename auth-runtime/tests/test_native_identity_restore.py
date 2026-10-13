"""Root-coordinated whole Native and business restore must preserve real identity."""

import base64
from hashlib import sha256
import secrets
from pathlib import Path

from production.auth.routes import SESSION_COOKIE
from production.repository import PostgresRepository
from tests.fixtures.auth30 import ORIGIN
from tests.fixtures.auth32_native_recovery import (
    business_snapshot, files_snapshot, native_public_keys, native_sql_snapshot, native_state, private_file_hashes,
    recovery_bff, recovery_target, verify_native_archives,
)
from tests.fixtures.auth32_recovery_control import RootRecoveryWindow, manifest_hash, recovery_plan, write_private
from tests.fixtures.auth32_roles import grant_runtime_schema, isolated_runtime_roles, private_role_configuration
from tests.fixtures.identity import load_fixture


def authenticated_resource(context, task, expected):
    detail = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
    assert detail.status == 200 and detail.json()["result"]["video"]["sha256"] == task["result"]["video"]["sha256"]
    response = context.request.get(ORIGIN + task["result"]["download_url"])
    assert response.status == 200 and response.body() == expected, "Restored authorization must deliver the exact original owned media"


def test_coordinated_native_and_business_restore_preserves_oidc_saml_principals_old_cookie_keys_and_media(monkeypatch):
    coordinated_restore(monkeypatch, check_wrong_key=False)


def test_coordinated_native_restore_rejects_a_wrong_session_key(monkeypatch):
    coordinated_restore(monkeypatch, check_wrong_key=True)


def coordinated_restore(monkeypatch, *, check_wrong_key):
    # Missing operator input fails before any schema, listener, identity request or process is started.
    directory, plan = recovery_plan()
    owner = Path(plan["owner_state_dir"])
    fixture = load_fixture()
    assert fixture.path.parent == owner and fixture._data["namespace"] == "auth30"
    assert set(fixture._data.get("sso_subjects", {})) >= {"oidc", "saml"}, "Recovery requires actual frozen Native subject proofs and explicit grants"
    target_configuration = recovery_target(private_role_configuration())
    source, target = directory / "source", directory / "target"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    baseline = native_state(owner)
    original_files = private_file_hashes(owner)
    original_keys = native_public_keys(fixture)
    sso_evidence = directory / "sso-source"
    sso_evidence.mkdir(mode=0o700)
    monkeypatch.setenv("AUTH30_EVIDENCE_DIR", str(sso_evidence))
    source_principals, cookies = {}, {}
    with isolated_runtime_roles(source, retain_source=True) as (configuration, migration, bff, _):
        schema = migration.schema
        with recovery_bff(source, configuration, bff, fixture, monkeypatch, production=True) as (sso, studio_configuration):
            oidc, _ = sso.sign_in("oidc", organization="A")
            assert sso.callback_status == 303
            source_principals["oidc"] = sso.principal(oidc, "oidc")
            task = sso.complete(oidc)
            expected = oidc.request.get(ORIGIN + task["result"]["download_url"]).body()
            assert sha256(expected).hexdigest() == task["result"]["video"]["sha256"]
            contexts = {"oidc": oidc}
            saml, _ = sso.sign_in("saml", organization="A")
            assert sso.callback_status == 303
            source_principals["saml"] = sso.principal(saml, "saml")
            contexts["saml"] = saml
            for protocol, context in contexts.items():
                authenticated_resource(context, task, expected)
                cookies[protocol] = [cookie for cookie in context.cookies(ORIGIN) if cookie["name"] == SESSION_COOKIE]
                assert len(cookies[protocol]) == 1
            binding = bff.database_binding()
    # Real BFF, browser, Pi model, media fixture manager and Worker have all exited.
    source_rows = business_snapshot(configuration, schema)
    source_files = files_snapshot(source)
    for name, value in (("source", configuration), ("target", target_configuration)):
        write_private(directory / (name + "-operator.json"), {"database_url": value["bootstrap_database_url"], "postgres_container": value["postgres_container"]})
    write_private(directory / "mapping.json", [{"project_id": task["project_id"], "task_id": task["task_id"], "workspace": task["ownership"]["workspace"],
                                              "creator_user_id": task["ownership"]["creator"]["user_id"]}])
    native_sources = {name: baseline[name]["database"] for name in ("logto", "keycloak")}
    native_clones = {name: "auth32_restore_" + name + "_" + plan["run_id"] for name in native_sources}
    manifest = {"schema_version": 1, "run_id": plan["run_id"], "owner_state_dir": str(owner), "schema": schema,
                "source_root": str(source), "target_root": str(target), "source_operator": str(directory / "source-operator.json"),
                "target_operator": str(directory / "target-operator.json"), "mapping": str(directory / "mapping.json"),
                "business_backup": str(directory / "business-backup"), "native_sources": native_sources, "native_clones": native_clones,
                "native_baseline": baseline, "native_public_keys": original_keys, "retained_private_files": original_files,
                "source_business_rows": source_rows, "source_business_files": source_files}
    write_private(directory / "manifest.json", manifest)
    window = RootRecoveryWindow(directory, plan["run_id"], manifest_hash(directory / "manifest.json"))
    native_frozen = {}

    def observe_quiesced():
        observed = native_state(owner, quiesced=True)
        assert observed == baseline, "Quiescence must retain the actual image, seal, source database and container identity"
        assert business_snapshot(configuration, schema) == source_rows
        assert files_snapshot(source) == source_files and private_file_hashes(owner) == original_files
        native_frozen.update({name: native_sql_snapshot(baseline["identity-postgres"]["container_id"], database)
                              for name, database in native_sources.items()})
        write_private(directory / "source-native-observed.json", native_frozen)

    try:
        with window.clones(observe_quiesced):
            observed = native_state(owner)
            assert observed["identity-postgres"] == baseline["identity-postgres"], "Native PostgreSQL must retain the original source databases"
            for name in native_sources:
                assert observed[name]["database"] == native_clones[name] != native_sources[name], "Restore checks must actually run against fresh Native clones"
                assert observed[name]["image_id"] == baseline[name]["image_id"]
            assert observed["logto"]["seal"] == baseline["logto"]["seal"]
            assert native_public_keys(fixture) == original_keys
            grant_runtime_schema(target_configuration, schema)
            restored = PostgresRepository(target_configuration["bff_database_url"], schema=schema, projects_dir=target / "projects")
            assert restored.database_binding() == binding
            assert restored.get_task(task["task_id"]).public_dict()["result"]["video"]["sha256"] == task["result"]["video"]["sha256"]
            clone_evidence = directory / "sso-clone"
            clone_evidence.mkdir(mode=0o700)
            monkeypatch.setenv("AUTH30_EVIDENCE_DIR", str(clone_evidence))
            with recovery_bff(target, target_configuration, restored, fixture, monkeypatch, studio_configuration=studio_configuration) as (sso, _):
                for protocol, original_cookie in cookies.items():
                    context = sso.browser.new_context()
                    context.add_cookies(original_cookie)
                    me = sso.me(context)
                    assert me["user"]["user_id"] == source_principals[protocol]["user_id"] and me["role"] == source_principals[protocol]["role"]
                    authenticated_resource(context, task, expected)
                    context.close()
                    context, _ = sso.sign_in(protocol, organization="A")
                    assert sso.callback_status == 303 and sso.principal(context, protocol) == source_principals[protocol]
                    authenticated_resource(context, task, expected)
                    context.close()
                before_worker = restored.get_task(task["task_id"]).public_dict()
                sso.run_worker()
                assert restored.get_task(task["task_id"]).public_dict() == before_worker
            if check_wrong_key:
                wrong_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
                with recovery_bff(target, target_configuration, restored, fixture, monkeypatch, studio_configuration=studio_configuration, key=wrong_key) as (sso, _):
                    denied = sso.browser.new_context()
                    denied.add_cookies(cookies["oidc"])
                    assert denied.request.get(ORIGIN + "/api/auth/me").status == 503
                    assert denied.request.get(ORIGIN + task["result"]["download_url"]).status == 503
            observed_source = {name: native_sql_snapshot(baseline["identity-postgres"]["container_id"], database)
                               for name, database in native_sources.items()}
            assert observed_source == native_frozen, "Independent read-only observation found changed original Native data"
            write_private(directory / "source-native-after-clone-observed.json", observed_source)
    finally:
        # The handshake's finally always requests original recovery, including an assertion failure.
        assert window.original_ready, "Required original recovery acknowledgment is missing"
        observed = native_state(owner)
        for name in baseline:
            assert observed[name]["image_id"] == baseline[name]["image_id"]
        for name, database in native_sources.items():
            assert observed[name]["database"] == database
        assert observed["logto"]["seal"] == baseline["logto"]["seal"]
        assert native_public_keys(fixture) == original_keys
        assert private_file_hashes(owner) == original_files
        assert business_snapshot(configuration, schema) == source_rows and files_snapshot(source) == source_files
    verify_native_archives(directory, plan["run_id"], window.manifest_hash, native_sources, native_clones, baseline["identity-postgres"]["container_id"], native_frozen)
    write_private(directory / "receipt.json", {"schema_version": 1, "run_id": plan["run_id"], "manifest_sha256": window.manifest_hash,
                   "passed": True, "protocols": ["oidc", "saml"], "video_sha256": sha256(expected).hexdigest()})
