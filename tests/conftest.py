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
