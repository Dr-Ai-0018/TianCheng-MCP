from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from tiancheng_mcp.access_context import AccessContext
from tiancheng_mcp.file_operations import FILE_OPERATIONS, PathCoordinator
from tiancheng_mcp.service import TianChengService


@pytest.mark.parametrize("operation", ["write_text", "edit_text", "append_text"])
@pytest.mark.parametrize("scoped", [False, True])
def test_same_hash_has_one_commit(service, workspace, monkeypatch, operation, scoped):
    before = service.write_text("nested/file.txt", "before")["sha256"]
    other = service._scoped_service(AccessContext(
        workspace / "nested", "write", lambda: service.access_policy
    )) if scoped else service
    first_at_commit = threading.Event()
    second_at_lock = threading.Event()
    release = threading.Event()
    outcomes = []
    original_replace = TianChengService._atomic_replace_bytes
    original_hold = FILE_OPERATIONS.hold

    def paused_replace(self, target, data):
        if threading.current_thread().name == "first-writer":
            first_at_commit.set()
            assert release.wait(5)
        return original_replace(self, target, data)

    @contextmanager
    def observed_hold(*args, **kwargs):
        if threading.current_thread().name == "second-writer":
            second_at_lock.set()
        with original_hold(*args, **kwargs):
            yield

    monkeypatch.setattr(TianChengService, "_atomic_replace_bytes", paused_replace)
    # append commits through its existing stream, so pause its checksum read.
    if operation == "append_text":
        original_hash = TianChengService._sha256_bytes

        def paused_hash(data):
            if threading.current_thread().name == "first-writer":
                first_at_commit.set()
                assert release.wait(5)
            return original_hash(data)

        monkeypatch.setattr(TianChengService, "_sha256_bytes", staticmethod(paused_hash))
    monkeypatch.setattr(FILE_OPERATIONS, "hold", observed_hold)

    def run(instance, label):
        try:
            args = ("before", "after") if operation == "edit_text" else ("after",)
            outcomes.append(getattr(instance, operation)(label, *args, expected_sha256=before))
        except Exception as exc:
            outcomes.append(exc)

    first = threading.Thread(target=run, args=(service, "nested/file.txt"), name="first-writer")
    second = threading.Thread(target=run, args=(other, "file.txt" if scoped else "nested/file.txt"), name="second-writer")
    first.start()
    try:
        assert first_at_commit.wait(5)
        second.start()
        assert second_at_lock.wait(5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        if scoped:
            other.shutdown()
    assert not first.is_alive() and not second.is_alive()
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(failures) == 1 and "expected_sha256" in str(failures[0])
    assert (workspace / "nested/file.txt").read_text(encoding="utf-8") == (
        "beforeafter" if operation == "append_text" else "after"
    )
    assert not FILE_OPERATIONS._active


def test_coordinator_subtrees_parallel_and_cancelled_wait_releases(tmp_path):
    coordinator = PathCoordinator()
    attempted = threading.Event()
    cancelled = threading.Event()
    errors = []

    def check():
        attempted.set()
        if cancelled.is_set():
            raise RuntimeError("cancelled waiting mutation")

    def wait_for_child():
        try:
            with coordinator.hold([tmp_path / "tree/child"], check):
                pytest.fail("overlapping mutation entered")
        except RuntimeError as exc:
            errors.append(str(exc))

    with coordinator.hold([tmp_path / "tree"]):
        # Distinct subtrees remain independent; nested ownership is reentrant.
        with coordinator.hold([tmp_path / "other", tmp_path / "tree/own"]):
            thread = threading.Thread(target=wait_for_child)
            thread.start()
            assert attempted.wait(5)
            cancelled.set()
            thread.join(5)
            assert not thread.is_alive()
    assert errors == ["cancelled waiting mutation"]
    assert not coordinator._active


def test_delete_metadata_failure_leaves_original(service, workspace, monkeypatch):
    service.write_text("recover.txt", "retained")
    original = service._atomic_replace_bytes

    def fail_metadata(target, data):
        if target.parent.name == ".metadata":
            raise OSError("synthetic metadata failure")
        original(target, data)

    monkeypatch.setattr(service, "_atomic_replace_bytes", fail_metadata)
    with pytest.raises(OSError, match="metadata failure"):
        service.delete("recover.txt")
    assert (workspace / "recover.txt").read_text(encoding="utf-8") == "retained"
    assert service.trash_list()["items"] == []


def test_delete_move_failure_leaves_original_and_no_record(service, workspace, monkeypatch):
    service.write_text("recover.txt", "retained")
    import tiancheng_mcp.service as module
    original = module.os.replace

    def fail_payload(source, destination):
        if Path(source).name == "recover.txt":
            raise OSError("synthetic move failure")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_payload)
    with pytest.raises(OSError, match="move failure"):
        service.delete("recover.txt")
    assert (workspace / "recover.txt").read_text(encoding="utf-8") == "retained"
    assert list((workspace / ".tiancheng-trash/.metadata").iterdir()) == []


def test_delete_records_recovery_before_payload_move(service, workspace, monkeypatch):
    service.write_text("recover.txt", "retained")
    import json
    import tiancheng_mcp.service as module
    original = module.os.replace
    seen = []

    def check_intent(source, destination):
        if Path(source).name == "recover.txt":
            record = Path(destination).parent / ".metadata" / (Path(destination).name + ".json")
            seen.append(json.loads(record.read_text(encoding="utf-8"))["original_path"])
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", check_intent)
    deleted = service.delete("recover.txt")
    assert seen == ["recover.txt"]
    # A new service can recover from the durable record without in-memory state.
    with_service = TianChengService(workspace, None, enable_jobs=False, enable_agent_catalog=False)
    try:
        assert with_service.trash_restore(deleted["trash_path"])["restored"]
    finally:
        with_service.shutdown()
    assert (workspace / "recover.txt").read_text(encoding="utf-8") == "retained"


def test_restore_cleanup_failure_reports_committed_data(service, workspace, monkeypatch):
    service.write_text("recover.txt", "retained")
    deleted = service.delete("recover.txt")
    original = Path.unlink

    def fail_metadata(path, *args, **kwargs):
        if path.parent.name == ".metadata":
            raise OSError("synthetic cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_metadata)
    result = service.trash_restore(deleted["trash_path"])
    assert result["restored"] and result["metadata_cleanup_pending"]
    assert (workspace / "recover.txt").read_text(encoding="utf-8") == "retained"
    assert not (workspace / deleted["trash_path"]).exists()


def test_restore_conflict_retains_payload_and_record(service, workspace):
    service.write_text("recover.txt", "old")
    deleted = service.delete("recover.txt")
    service.write_text("recover.txt", "new")
    with pytest.raises(FileExistsError):
        service.trash_restore(deleted["trash_path"])
    assert service.read_text("recover.txt")["content"] == "new"
    assert (workspace / deleted["trash_path"]).read_text(encoding="utf-8") == "old"
    assert service.trash_list()["items"][0]["original_path"] == "recover.txt"


@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", b"\xef\xbb\xbf"),
    ("utf-16-le", b"\xff\xfe"),
    ("utf-16-be", b"\xfe\xff"),
])
def test_exact_edit_preserves_bom_and_encoding(service, workspace, encoding, bom):
    target = workspace / "encoded.txt"
    target.write_bytes(bom + "原文\r\n第二行".encode(encoding))
    before = service.hash_file("encoded.txt")["sha256"]
    result = service.edit_text("encoded.txt", "原文", "新文", expected_sha256=before)
    assert result["atomic_replace"]
    assert target.read_bytes() == bom + "新文\r\n第二行".encode(encoding)


def test_append_commit_failure_preserves_bytes(service, workspace, monkeypatch):
    before = service.write_text("append.txt", "原文")
    import tiancheng_mcp.service as module
    original = module.os.replace

    def fail_commit(source, destination):
        if Path(destination).name == "append.txt":
            raise OSError("synthetic append commit failure")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_commit)
    with pytest.raises(OSError, match="append commit"):
        service.append_text("append.txt", "尾部", expected_sha256=before["sha256"])
    assert (workspace / "append.txt").read_bytes() == "原文".encode("utf-8")
    assert not list(workspace.glob(".tiancheng-write-*"))


def test_delete_cannot_destroy_existing_recovery_record(service, workspace):
    service.write_text("recover.txt", "retained")
    deleted = service.delete("recover.txt")
    with pytest.raises(ValueError, match="trash tools"):
        service.delete(deleted["trash_path"])
    assert service.trash_restore(deleted["trash_path"])["restored"]
