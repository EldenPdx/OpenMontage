"""A business database outage must target its dedicated CI PostgreSQL container."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def ci():
    path = Path(__file__).resolve().parents[2] / "auth-runtime/ci.py"
    spec = importlib.util.spec_from_file_location("business_outage_ci", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def business_container():
    return {"Id": "a" * 64, "State": {"Running": True, "Paused": False, "Health": {"Status": "healthy"}},
            "Config": {"Env": ["POSTGRES_DB=studio_identity", "POSTGRES_USER=auth32_ci_bootstrap"]},
            "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]}}}


def test_business_outage_accepts_only_the_actual_business_connection_and_explicit_ci_service(ci):
    container = business_container()
    assert ci.validate_business_container(container, "a" * 64, "postgresql://auth32_test:private-test-sentinel@127.0.0.1:5432/studio_identity",
                                          ("studio_identity", "auth32_test", 5432)) == "a" * 64


@pytest.mark.parametrize("fault", ["identity-postgres", "container-id", "published-port", "paused", "sql-database", "sql-user", "dsn-host"])
def test_business_outage_rejects_wrong_database_container_or_runtime_target(ci, fault):
    container = deepcopy(business_container())
    identity = ("studio_identity", "auth32_test", 5432)
    dsn = "postgresql://auth32_test:private-test-sentinel@127.0.0.1:5432/studio_identity"
    if fault == "identity-postgres":
        container["Config"]["Env"] = ["POSTGRES_DB=postgres", "POSTGRES_USER=auth_bootstrap"]
    elif fault == "container-id":
        container["Id"] = "b" * 64
    elif fault == "published-port":
        container["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "55433"
    elif fault == "paused":
        container["State"]["Paused"] = True
    elif fault == "sql-database":
        identity = ("postgres", "auth32_test", 5432)
    elif fault == "sql-user":
        identity = ("studio_identity", "auth32_ci_bootstrap", 5432)
    else:
        dsn = dsn.replace("127.0.0.1", "remote.invalid")
    with pytest.raises(RuntimeError, match="business PostgreSQL"):
        ci.validate_business_container(container, "a" * 64, dsn, identity)
