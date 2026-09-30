"""Guard live acceptance against model-only claims and incomplete evidence."""

from pathlib import Path
import runpy

import pytest

HARNESS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/accept_linux_agent_readonly.py"))
ASSESS = HARNESS["assess"]


def evidence():
    return {
        "events": [
            {"type": "command_completed", "data": {"exit_code": 23, "status": "failed"}},
            {"type": "agent_message", "data": {"text": "PROBE_WRITE_DENIED fixture-nonce 30"}},
        ],
        "cursor_gap": False, "has_more": False,
    }


def test_absent_marker_and_model_report_do_not_prove_refusal(tmp_path):
    page = evidence()
    page["events"].pop(0)
    result = ASSESS({"state": "succeeded"}, page, tmp_path, "fixture-nonce", 0, "read-only")
    assert result["artifact_matches"] and result["random_token_matches"]
    assert not result["verified"]


@pytest.mark.parametrize("missing", ["nonce", "exit_code", "status", "cursor_gap", "has_more", "approval", "marker"])
def test_incomplete_or_escalated_evidence_does_not_pass(tmp_path, missing):
    page = evidence()
    approvals = 0
    if missing == "nonce":
        page["events"][1]["data"]["text"] = "PROBE_WRITE_DENIED guessed-nonce 30"
    elif missing in {"exit_code", "status"}:
        page["events"][0]["data"][missing] = None
    elif missing in {"cursor_gap", "has_more"}:
        page[missing] = True
    elif missing == "approval":
        approvals = 1
    else:
        (tmp_path / HARNESS["MARKER"]).write_bytes(HARNESS["CONTENTS"])
    assert not ASSESS({"state": "succeeded"}, page, tmp_path, "fixture-nonce", approvals, "read-only")["verified"]


def test_refusal_and_write_control_require_different_artifacts(tmp_path):
    page = evidence()
    detail = {"state": "succeeded"}
    assert ASSESS(detail, page, tmp_path, "fixture-nonce", 0, "read-only")["verified"]
    page["events"][0]["data"] = {"exit_code": 0, "status": "completed"}
    page["events"][1]["data"]["text"] = "PROBE_WRITE_ALLOWED fixture-nonce"
    assert not ASSESS(detail, page, tmp_path, "fixture-nonce", 0, "workspace-write")["verified"]
    marker = tmp_path / HARNESS["MARKER"]
    marker.write_bytes(HARNESS["CONTENTS"])
    assert ASSESS(detail, page, tmp_path, "fixture-nonce", 0, "workspace-write")["verified"]
    marker.write_bytes(b"wrong-bytes")
    assert not ASSESS(detail, page, tmp_path, "fixture-nonce", 0, "workspace-write")["verified"]
