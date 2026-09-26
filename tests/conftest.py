from __future__ import annotations

from pathlib import Path

import pytest

from tiancheng_mcp.service import TianChengService


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def service(workspace: Path, tmp_path: Path) -> TianChengService:
    return TianChengService(workspace, tmp_path / "audit")


_FIXTURE_CREDENTIAL_ENV = (
    "CONTROL_PLANE_API_KEY",
    "EXAMPLE_AGENT_KEY",
    "ISOLATED_AGENT_KEY",
)


@pytest.fixture(autouse=True)
def isolate_fixture_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests supply these names through their own temporary .env files.  A
    # developer machine that exports the real credential shadows the fixture
    # value, so the assertions only fail where a real key exists and pass on a
    # clean checkout.  Clearing them first makes the result independent of the
    # host environment, and a test that needs a value still sets its own.
    for name in _FIXTURE_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_service_local_agent_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Service defaults point at ignored, machine-owned files in the checkout.
    # Tests that need a specific policy or catalog pass it explicitly; all
    # other service instances must not depend on a developer's local ACL/data.
    original_init = TianChengService.__init__

    def init_with_test_state(self: TianChengService, *args, **kwargs) -> None:
        kwargs.setdefault("agent_source_policy_path", tmp_path / "agent-sources.json")
        kwargs.setdefault("agent_catalog_path", tmp_path / "agent-catalog.sqlite3")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TianChengService, "__init__", init_with_test_state)
