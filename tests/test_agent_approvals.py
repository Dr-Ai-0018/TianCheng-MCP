from __future__ import annotations

import json
import time

import pytest

from tiancheng_mcp.agent_approvals import ManualApprovalParser, load_manual_profile
from tiancheng_mcp.agent_adapters import normalize_codex_options, summarize_codex_options


def parser(**options):
    p = ManualApprovalParser(prompt="fixture", cwd="C:/fixture", sandbox="workspace-write",
        config={"model_provider": "fixture", "approvals_reviewer": "auto_review"}, options=options, native_id=None)
    p.feed_line(json.dumps({"id": 1, "result": {}}))
    assert p.outgoing[-1]["params"]["approvalsReviewer"] == "user"
    assert p.outgoing[-1]["params"]["config"]["approvals_reviewer"] == "user"
    p.feed_line(json.dumps({"id": 2, "result": {"thread": {"id": "thread-1", "modelProvider": "fixture"}}}))
    p.feed_line(json.dumps({"id": 3, "result": {"turn": {"id": "turn-1"}}}))
    p.outgoing.clear()
    return p


def request(p, rpc=50, **kwargs):
    params = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
              "command": "Write-Output 'fixture'", "reason": "fixture only", **kwargs}
    return p.feed_line(json.dumps({"id": rpc, "method": "item/commandExecution/requestApproval", "params": params}))


@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
def test_request_response_is_bound_and_single_use(decision):
    p = parser()
    e = request(p)
    assert e.type == "approval_requested"
    a = p.list_pending()[0]
    assert a["details"]["command"] == "Write-Output 'fixture'"
    with pytest.raises(FileNotFoundError):
        p.respond("wrong", decision)
    p.respond(a["approval_id"], decision)
    assert p.outgoing[-1] == {"id": 50, "result": {"decision": decision}}
    with pytest.raises(FileNotFoundError):
        p.respond(a["approval_id"], decision)


def test_expiry_cancels_and_rejects_late_response():
    p = parser(); request(p)
    aid = next(iter(p.pending)); p.pending[aid]["deadline"] = time.monotonic() - 1
    with pytest.raises(FileNotFoundError): p.respond(aid, "accept")
    assert p.outgoing[-1]["result"]["decision"] == "cancel"


@pytest.mark.parametrize("field", ["threadId", "turnId"])
def test_mismatched_binding_fails(field):
    p = parser(); request(p, **{field: "wrong"})
    assert p.done == "failed" and not p.pending


def test_duplicate_native_id_fails():
    p = parser(); request(p); request(p)
    assert p.done == "failed" and not p.pending


@pytest.mark.parametrize("command", ["x" * 20_000, "echo password=abcdef", None])
def test_unreviewable_requests_cancel(command):
    p = parser(); request(p, command=command)
    assert not p.pending and p.outgoing[-1]["result"]["decision"] == "cancel"


def test_unknown_requests_rejected():
    p = parser()
    p.feed_line(json.dumps({"id": 90, "method": "unsupported/request", "params": {}}))
    assert p.outgoing[-1]["error"]["code"] == -32601


def test_completion_invalidates_approvals():
    p = parser(); request(p); aid = next(iter(p.pending))
    p.feed_line(json.dumps({"method": "turn/completed", "params": {
        "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}))
    assert p.done == "succeeded" and not p.pending
    with pytest.raises(FileNotFoundError): p.respond(aid, "accept")


def test_file_changes_require_full_details():
    p = parser()
    params = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "patch-1"}
    p.feed_line(json.dumps({"method": "item/started", "params": {
        **params, "item": {"id": "patch-1", "type": "fileChange", "changes": [{"path": "a.txt", "diff": "+fixture"}]}}}))
    p.feed_line(json.dumps({"id": 7, "method": "item/fileChange/requestApproval", "params": params}))
    assert p.list_pending()[0]["details"]["changes"][0]["path"] == "a.txt"


def test_options_fail_explicitly():
    parser(images=(), add_dirs=())
    for options in [{"approve_for_me": True}, {"ask_for_approval": "never"}, {"search": True}]:
        with pytest.raises(ValueError): parser(**options)
    assert normalize_codex_options({"manual_approval": True}) == {"manual_approval": True}
    assert summarize_codex_options({"manual_approval": True}) == {"manual_approval": True}


def test_profile_settings_are_not_silently_discarded(tmp_path):
    (tmp_path / "fixture.config.toml").write_text('model="fixture"\n[windows]\nsandbox="elevated"\n')
    assert load_manual_profile(tmp_path, "fixture")["windows"]["sandbox"] == "elevated"
    (tmp_path / "fixture.config.toml").write_text('unknown_setting=true\n')
    with pytest.raises(ValueError): load_manual_profile(tmp_path, "fixture")
    with pytest.raises(ValueError): load_manual_profile(tmp_path, "../fixture")


def test_provider_mismatch_fails_before_model_request():
    p = ManualApprovalParser(prompt="fixture", cwd="C:/fixture", sandbox="workspace-write",
        config={"model_provider": "fixture"}, options={}, native_id=None)
    p.feed_line('{"id":1,"result":{}}')
    p.outgoing.clear()
    p.feed_line('{"id":2,"result":{"thread":{"id":"thread-1","modelProvider":"openai"}}}')
    assert p.done == "failed" and not p.outgoing


def test_interrupted_turn_is_cancelled_not_failed():
    p = parser(); request(p)
    p.feed_line(json.dumps({"method": "turn/completed", "params": {
        "threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}}}))
    assert p.done == "cancelled" and not p.pending


def test_protocol_utf8_can_be_split_between_bytes():
    p = parser()
    data = '审批测试'.encode()
    assert ''.join(p.decode_chunk(bytes([b])) for b in data) == '审批测试'
    assert p.done is None
    p.decode_chunk(b'\xff')
    assert p.done == 'failed'
