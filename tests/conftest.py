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
