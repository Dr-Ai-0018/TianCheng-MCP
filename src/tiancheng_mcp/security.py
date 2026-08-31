"""Workspace path validation for Windows.

The jail deliberately rejects every symlink, junction, or other reparse point
below the workspace root. That is stricter than merely checking the final
resolved path and avoids accidentally dereferencing a link during recursive
operations.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath


FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class WorkspaceSecurityError(ValueError):
    """Raised when an input path cannot safely be accessed in the workspace."""


def _is_reparse_point(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise WorkspaceSecurityError(f"Cannot inspect path safely: {path.name!r}") from exc
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _common_path_is_root(root: Path, candidate: Path) -> bool:
    root_text = os.path.normcase(os.path.abspath(str(root)))
    candidate_text = os.path.normcase(os.path.abspath(str(candidate)))
    try:
        return os.path.commonpath((root_text, candidate_text)) == root_text
    except ValueError:
        return False


def _validate_component_name(component: str) -> None:
    if component in {"", "."}:
        return
    if component == "..":
        raise WorkspaceSecurityError("Parent path components ('..') are not allowed")
    if "\x00" in component:
        raise WorkspaceSecurityError("NUL bytes are not allowed in paths")
    if ":" in component:
        raise WorkspaceSecurityError("Colon and alternate data stream syntax are not allowed")
    if component.endswith((" ", ".")):
        raise WorkspaceSecurityError("Path components cannot end with a space or dot")
    if any(character in component for character in '<>"|?*') or any(
        ord(character) < 32 for character in component
    ):
        raise WorkspaceSecurityError("Path contains characters that are unsafe on Windows")
    base = component.rstrip(" .").split(".", 1)[0].upper()
    if base in _WINDOWS_DEVICES:
        raise WorkspaceSecurityError("Windows device names are not allowed")


class WorkspaceJail:
    """Resolve untrusted relative paths without allowing workspace escape."""

    def __init__(self, root: str | Path, *, create: bool = True) -> None:
        requested = Path(root)
        if create:
            requested.mkdir(parents=True, exist_ok=True)
        if not requested.exists() or not requested.is_dir():
            raise WorkspaceSecurityError("Workspace root must be an existing directory")
        if _is_reparse_point(requested):
            raise WorkspaceSecurityError("Workspace root cannot be a reparse point")
        self.root = requested.resolve(strict=True)

    def relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceSecurityError("Resolved path is outside the workspace") from exc
        text = relative.as_posix()
        return "." if text == "." else text

    def resolve(
        self,
        user_path: str | Path | None,
        *,
        must_exist: bool,
        expect: str | None = None,
        allow_root: bool = True,
    ) -> Path:
        """Return a checked path.

        For a non-existent target, the nearest existing ancestor is resolved
        and checked. Existing components may not be reparse points.
        """

        raw = "." if user_path is None or str(user_path) == "" else str(user_path)
        if "\x00" in raw:
            raise WorkspaceSecurityError("NUL bytes are not allowed in paths")

        windows_path = PureWindowsPath(raw.replace("/", "\\"))
        if windows_path.drive or windows_path.root or windows_path.is_absolute():
            raise WorkspaceSecurityError("Only workspace-relative paths are allowed")

        components: list[str] = []
        for part in windows_path.parts:
            _validate_component_name(part)
            if part not in {"", "."}:
                components.append(part)

        lexical = self.root.joinpath(*components)
        if not _common_path_is_root(self.root, lexical):
            raise WorkspaceSecurityError("Path escapes the workspace")
        if lexical == self.root and not allow_root:
            raise WorkspaceSecurityError("This operation is not allowed on the workspace root")

        existing = lexical
        missing: list[str] = []
        while not os.path.lexists(existing):
            if existing == self.root:
                break
            missing.append(existing.name)
            existing = existing.parent

        if not os.path.lexists(existing):
            raise WorkspaceSecurityError("Could not find a safe existing parent")

        self._reject_reparse_components(existing)
        resolved_existing = existing.resolve(strict=True)
        if not _common_path_is_root(self.root, resolved_existing):
            raise WorkspaceSecurityError("Resolved path escapes the workspace")

        resolved = resolved_existing.joinpath(*reversed(missing))
        if not _common_path_is_root(self.root, resolved):
            raise WorkspaceSecurityError("Resolved path escapes the workspace")

        exists = os.path.lexists(lexical)
        if must_exist and not exists:
            raise FileNotFoundError(f"Workspace path does not exist: {raw}")
        if exists:
            self._reject_reparse_components(lexical)
            resolved = lexical.resolve(strict=True)
            if not _common_path_is_root(self.root, resolved):
                raise WorkspaceSecurityError("Resolved path escapes the workspace")

        if expect == "file" and exists and not resolved.is_file():
            raise IsADirectoryError(f"Expected a file: {raw}")
        if expect == "directory" and exists and not resolved.is_dir():
            raise NotADirectoryError(f"Expected a directory: {raw}")
        return resolved

    def _reject_reparse_components(self, path: Path) -> None:
        if not _common_path_is_root(self.root, path):
            raise WorkspaceSecurityError("Path escapes the workspace")
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceSecurityError("Path escapes the workspace") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if os.path.lexists(current) and _is_reparse_point(current):
                raise WorkspaceSecurityError(
                    f"Symlink, junction, or reparse point is not allowed: {part!r}"
                )

    def reject_reparse_tree(self, root: Path, *, max_entries: int = 200_000) -> None:
        """Reject reparse points anywhere below an existing directory."""

        checked = self.resolve(self.relative(root), must_exist=True)
        if checked.is_file():
            return
        inspected = 0
        for current, directories, files in os.walk(checked, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                inspected += 1
                if inspected > max_entries:
                    raise WorkspaceSecurityError(
                        f"Recursive safety scan exceeded {max_entries} entries"
                    )
                child = current_path / name
                if _is_reparse_point(child):
                    raise WorkspaceSecurityError(
                        f"Recursive operation refused reparse point: {self.relative(child)}"
                    )


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a small, path-separator-aware glob dialect."""

    if not pattern or "\x00" in pattern:
        raise ValueError("glob pattern must be a non-empty string")
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise WorkspaceSecurityError("glob pattern must be workspace-relative")
    if any(part == ".." for part in normalized.split("/")):
        raise WorkspaceSecurityError("glob pattern cannot contain '..'")

    output = ["^"]
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if char == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    output.append("(?:.*/)?")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif char == "?":
            output.append("[^/]")
        elif char == "[":
            closing = normalized.find("]", index + 1)
            if closing == -1:
                output.append(r"\[")
            else:
                content = normalized[index + 1 : closing]
                if content.startswith("!"):
                    content = "^" + content[1:]
                output.append("[" + content.replace("\\", r"\\") + "]")
                index = closing
        else:
            output.append(re.escape(char))
        index += 1
    output.append("$")
    return re.compile("".join(output), re.IGNORECASE if os.name == "nt" else 0)
