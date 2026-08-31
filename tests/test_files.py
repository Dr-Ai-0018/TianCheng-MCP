from __future__ import annotations

from pathlib import Path

import pytest

from tiancheng_mcp.security import WorkspaceSecurityError
from tiancheng_mcp.service import TianChengService


def test_normal_write_overwrite_and_read(service: TianChengService, workspace: Path) -> None:
    created = service.write_text("notes/hello.txt", "first\nsecond\n")
    assert created["created"] is True
    assert created["bytes_written"] == len("first\nsecond\n".encode())
    assert (workspace / "notes/hello.txt").read_text(encoding="utf-8") == "first\nsecond\n"

    replaced = service.write_text("notes/hello.txt", "changed")
    assert replaced["created"] is False
    result = service.read_text("notes/hello.txt")
    assert result["content"] == "changed"
    assert result["truncated"] is False


def test_unicode_chinese_path_and_append(service: TianChengService) -> None:
    written = service.write_text("学习/语文笔记.md", "你好，天成")
    appended = service.append_text(
        "学习/语文笔记.md", "\n第二行", expected_sha256=written["sha256"]
    )
    assert appended["previous_sha256"] == written["sha256"]
    assert appended["sha256"] != written["sha256"]
    result = service.read_text("学习/语文笔记.md", start_line=2, end_line=2)
    assert result["content"] == "第二行"
    assert result["path"] == "学习/语文笔记.md"


def test_append_sha256_rejects_stale_content(service: TianChengService) -> None:
    written = service.write_text("append.txt", "before")
    service.write_text("append.txt", "changed")
    with pytest.raises(RuntimeError, match="expected_sha256"):
        service.append_text("append.txt", "!", expected_sha256=written["sha256"])


def test_hash_file_returns_sha256_and_has_size_cap(
    service: TianChengService,
) -> None:
    written = service.write_text("hash.txt", "hash me")
    hashed = service.hash_file("hash.txt")
    assert hashed["sha256"] == written["sha256"]
    assert hashed["size_bytes"] == len("hash me".encode())
    with pytest.raises(ValueError, match="limited"):
        service.hash_file("hash.txt", max_bytes=1)


def test_chunked_read_preserves_unicode_boundaries(service: TianChengService) -> None:
    content = "第一段🙂第二段\n第三段"
    service.write_text("分块.txt", content)
    chunks = []
    offset = 0
    while True:
        result = service.read_text_chunk("分块.txt", offset_bytes=offset, max_bytes=7)
        chunks.append(result["content"])
        offset = result["next_offset_bytes"]
        if result["eof"]:
            break
    assert "".join(chunks) == content
    assert offset == len(content.encode("utf-8"))


def test_exact_edit_and_sha256_precondition(service: TianChengService) -> None:
    written = service.write_text("edit.txt", "alpha beta alpha\n")
    with pytest.raises(RuntimeError, match="expected 1 matches, found 2"):
        service.edit_text("edit.txt", "alpha", "omega")
    edited = service.edit_text(
        "edit.txt",
        "alpha",
        "omega",
        expected_replacements=2,
        expected_sha256=written["sha256"],
    )
    assert edited["replacements"] == 2
    assert service.read_text("edit.txt")["content"] == "omega beta omega\n"
    with pytest.raises(RuntimeError, match="changed since"):
        service.write_text(
            "edit.txt", "stale overwrite", expected_sha256=written["sha256"]
        )


def test_read_truncation_and_binary_refusal(service: TianChengService, workspace: Path) -> None:
    service.write_text("large.txt", "abcdef")
    result = service.read_text("large.txt", max_bytes=3)
    assert result["content"] == "abc"
    assert result["truncated"] is True
    (workspace / "binary.bin").write_bytes(b"abc\x00def")
    with pytest.raises(ValueError, match="Binary"):
        service.read_text("binary.bin")
    (workspace / "late-binary.bin").write_bytes(b"a" * 9000 + b"\x00tail")
    with pytest.raises(ValueError, match="Binary"):
        service.read_text("late-binary.bin")


def test_list_stat_mkdir_move_and_copy(service: TianChengService) -> None:
    service.mkdir("docs/inside")
    service.write_text("docs/inside/a.txt", "a")
    listing = service.list_dir("docs", depth=2)
    assert {entry["path"] for entry in listing["entries"]} == {
        "docs/inside",
        "docs/inside/a.txt",
    }
    assert service.stat("docs/inside/a.txt")["size"] == 1

    service.copy("docs", "docs-copy")
    assert service.read_text("docs-copy/inside/a.txt")["content"] == "a"
    service.move("docs-copy/inside/a.txt", "renamed.txt")
    assert service.read_text("renamed.txt")["content"] == "a"


def test_glob_and_search(service: TianChengService) -> None:
    service.write_text("root.py", "Needle root\n")
    service.write_text("src/a.py", "one\nneedle here\n")
    service.write_text("src/readme.md", "needle ignored by filter\n")
    service.write_text("notes/today.md", "Needle note\n")

    matches = service.glob("**/*.py", max_results=10)
    assert {item["path"] for item in matches["results"]} == {"root.py", "src/a.py"}

    search = service.search_text(
        "needle", glob_pattern="**/*.py", case_sensitive=False, max_results=10
    )
    assert [(item["path"], item["line"]) for item in search["results"]] == [
        ("root.py", 1),
        ("src/a.py", 2),
    ]
    assert search["engine"] in {"ripgrep", "python-fallback"}
    assert search["scanned_bytes"] <= search["max_scan_bytes"]

    rooted = service.glob("**/*.py", max_results=10, base_path="src")
    assert {item["path"] for item in rooted["results"]} == {"src/a.py"}
    rooted_search = service.search_text(
        "needle", glob_pattern="**/*.py", base_path="src", max_results=10
    )
    assert [item["path"] for item in rooted_search["results"]] == ["src/a.py"]


def test_search_scan_budget_is_aggregate(service: TianChengService) -> None:
    service.write_text("a.py", "needle\n" + ("a" * 30))
    service.write_text("b.py", "needle\n" + ("b" * 30))
    result = service.search_text(
        "needle", glob_pattern="**/*.py", max_results=20, max_scan_bytes=40
    )
    assert result["scanned_bytes"] <= 40
    assert result["max_scan_bytes"] == 40
    assert result["truncated"] is True


def test_search_respects_gitignore_and_internal_excludes(
    service: TianChengService,
) -> None:
    if not service.rg_executable:
        pytest.skip("ripgrep is unavailable")
    service.write_text(".gitignore", "ignored/\n")
    service.write_text("visible.txt", "special needle\n")
    service.write_text("ignored/hidden.txt", "special needle\n")
    service.write_text(".github/config.txt", "special needle\n")
    service.write_text(".tiancheng-trash/old.txt", "special needle\n")
    normal = service.search_text("special needle", max_results=20)
    assert {item["path"] for item in normal["results"]} == {
        ".github/config.txt",
        "visible.txt",
    }
    all_ignored = service.search_text(
        "special needle",
        max_results=20,
        respect_gitignore=False,
        include_internal=True,
    )
    assert {item["path"] for item in all_ignored["results"]} >= {
        ".github/config.txt",
        ".tiancheng-trash/old.txt",
        "ignored/hidden.txt",
        "visible.txt",
    }


def test_delete_moves_to_unique_trash(service: TianChengService, workspace: Path) -> None:
    service.write_text("temporary.txt", "recoverable")
    first = service.delete("temporary.txt")
    assert first["permanently_deleted"] is False
    assert not (workspace / "temporary.txt").exists()
    trashed = workspace / Path(first["trash_path"])
    assert trashed.read_text(encoding="utf-8") == "recoverable"

    service.write_text("temporary.txt", "again")
    second = service.delete("temporary.txt")
    assert second["trash_path"] != first["trash_path"]


def test_trash_list_restore_and_permanent_purge(
    service: TianChengService, workspace: Path
) -> None:
    service.write_text("restore/me.txt", "come back")
    deleted = service.delete("restore/me.txt")
    listing = service.trash_list()
    listed = next(item for item in listing["items"] if item["path"] == deleted["trash_path"])
    assert listed["original_path"] == "restore/me.txt"
    restored = service.trash_restore(deleted["trash_path"])
    assert restored["restored_path"] == "restore/me.txt"
    assert service.read_text("restore/me.txt")["content"] == "come back"

    deleted_again = service.delete("restore/me.txt")
    purged = service.trash_purge(deleted_again["trash_path"])
    assert purged["permanently_deleted"] is True
    assert purged["count"] == 1
    assert not (workspace / Path(deleted_again["trash_path"])).exists()


def test_nonexistent_safe_write_parent_and_escape(service: TianChengService) -> None:
    service.write_text("new/deep/file.txt", "safe")
    assert service.read_text("new/deep/file.txt")["content"] == "safe"
    with pytest.raises(WorkspaceSecurityError):
        service.write_text(r"new\..\..\outside.txt", "blocked")


def test_recursive_copy_and_move_cannot_target_their_own_tree(
    service: TianChengService,
) -> None:
    service.mkdir("tree")
    service.write_text("tree/a.txt", "a")
    with pytest.raises(ValueError, match="inside itself"):
        service.copy("tree", "tree/copy")
    with pytest.raises(ValueError, match="inside itself"):
        service.move("tree", "tree/moved")
