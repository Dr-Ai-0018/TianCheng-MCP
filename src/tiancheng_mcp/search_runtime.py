"""One bounded candidate policy for native and Python text search."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import fnmatch
from pathlib import Path
import re
import threading
import time

from .jobs import JobCancelled
from .security import WorkspaceJail, WorkspaceSecurityError, compile_glob

INTERNAL_NAMES = frozenset({'.git', '.tiancheng-trash', '.tiancheng-tmp', 'node_modules', '.venv'})


class IgnorePattern:
    """Segment glob matching with bounded states instead of recursive **."""

    def __init__(self, pattern: str):
        self.parts: list[re.Pattern[str] | None] = []
        for part in pattern.split('/'):
            if part == '**':
                self.parts.append(None)
                continue
            part = re.sub(r'\\(.)', lambda m: {'*': '[*]', '?': '[?]', '[': '[[]'}.get(m[1], m[1]), part)
            self.parts.append(re.compile(fnmatch.translate(part)))

    def fullmatch(self, value: str) -> bool:
        def closure(states: set[int]) -> set[int]:
            states = set(states)
            for index in range(len(self.parts) - 1):
                if index in states and self.parts[index] is None:
                    states.add(index + 1)
            return states
        states = closure({0})
        for part in value.split('/'):
            following: set[int] = set()
            for index in states:
                if index == len(self.parts):
                    continue
                matcher = self.parts[index]
                if matcher is None:
                    following.add(index)
                    if index == len(self.parts) - 1:
                        following.add(index + 1)
                elif matcher.fullmatch(part):
                    following.add(index + 1)
            states = closure(following)
        return len(self.parts) in states


@dataclass(frozen=True)
class IgnoreRule:
    directory: Path
    matcher: IgnorePattern
    basename: bool
    directory_only: bool
    negate: bool
    priority: int

    def matches(self, path: Path, is_directory: bool) -> bool:
        if self.directory_only and not is_directory:
            return False
        relative = path.relative_to(self.directory).as_posix()
        return bool(self.matcher.fullmatch(path.name if self.basename else relative))


@dataclass
class SearchCandidates:
    jail: WorkspaceJail
    base: Path
    glob_pattern: str
    include_hidden: bool
    respect_gitignore: bool
    include_internal: bool
    scan_limit: int
    per_file_limit: int
    max_files: int
    timeout_seconds: float
    cancel_event: threading.Event | None = None
    started: float = field(default_factory=time.monotonic)
    paths: list[Path] = field(default_factory=list)
    scanned_bytes: int = 0
    examined_entries: int = 0
    truncated: bool = False
    ignore_bytes: int = 0
    rule_count: int = 0
    rules: dict[Path, list[IgnoreRule]] = field(default_factory=dict)

    def check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled('Job cancelled during text search')
        if time.monotonic() - self.started >= self.timeout_seconds:
            raise TimeoutError('Text search timed out')

    def _load(self, directory: Path) -> None:
        if directory in self.rules or not self.respect_gitignore:
            return
        rules = []
        sources = [('.gitignore', 0), ('.ignore', 1), ('.rgignore', 2)]
        if directory == self.jail.root:
            sources.insert(0, ('.git/info/exclude', -1))
        for name, priority in sources:
            self.check()
            path = directory / name
            if not os.path.lexists(path):
                continue
            checked = self.jail.resolve(self.jail.relative(path), must_exist=True, expect='file')
            with checked.open('rb') as stream:
                data = stream.read(64 * 1024 + 1)
            self.ignore_bytes += len(data)
            if len(data) > 64 * 1024 or self.ignore_bytes > 1024 * 1024:
                raise ValueError('Search ignore files exceeded their bounded metadata budget')
            for line in data.decode('utf-8-sig', errors='replace').splitlines():
                self.check()
                while line.endswith(' ') and not line.endswith('\\ '):
                    line = line[:-1]
                if not line or line.startswith('#'):
                    continue
                negate = line.startswith('!')
                if negate:
                    line = line[1:]
                directory_only = line.endswith('/')
                line = line.removesuffix('/')
                anchored = line.startswith('/')
                line = line.removeprefix('/')
                if not line:
                    continue
                try:
                    matcher = IgnorePattern(line)
                except re.error:
                    continue
                rules.append(IgnoreRule(directory, matcher, not anchored and '/' not in line,
                    directory_only, negate, priority))
                self.rule_count += 1
                if self.rule_count > 10_000:
                    raise ValueError('Search ignore rules exceeded their bounded metadata budget')
        self.rules[directory] = rules

    def _excluded(self, path: Path, is_directory: bool) -> bool:
        parts = path.relative_to(self.jail.root).parts
        if not self.include_hidden and any(part.startswith('.') for part in parts):
            return True
        if not self.include_internal and any(part.casefold() in INTERNAL_NAMES for part in parts):
            return True
        if not self.respect_gitignore:
            return False
        parents = [self.jail.root]
        for part in path.relative_to(self.jail.root).parts[:-1]:
            parents.append(parents[-1] / part)
        rules = []
        for parent in parents:
            self._load(parent)
            rules.extend(self.rules[parent])
        # Different ignore file kinds have precedence; within a kind, deeper
        # directories and later lines override earlier rules.
        rules.sort(key=lambda rule: rule.priority)
        ignored = False
        for rule in rules:
            self.check()
            if rule.matches(path, is_directory):
                ignored = not rule.negate
        return ignored

    def collect(self) -> 'SearchCandidates':
        self.check()
        matcher = compile_glob(self.glob_pattern)
        # Honor excluded ancestors even when the caller starts inside them.
        path = self.jail.root
        for part in self.base.relative_to(path).parts:
            path /= part
            if self._excluded(path, True):
                return self
        def walk_error(error):
            raise error
        for current, directories, files in os.walk(self.base, followlinks=False, onerror=walk_error):
            self.check()
            directory = Path(current)
            self._load(directory)
            safe = []
            for name in sorted(directories):
                self.check()
                self.examined_entries += 1
                if self.examined_entries > self.max_files:
                    self.truncated = True
                    return self
                child = directory / name
                if self._excluded(child, True):
                    continue
                try:
                    self.jail.resolve(self.jail.relative(child), must_exist=True, expect='directory')
                except (OSError, WorkspaceSecurityError):
                    continue
                safe.append(name)
            directories[:] = safe
            for name in sorted(files):
                self.check()
                self.examined_entries += 1
                if self.examined_entries > self.max_files:
                    self.truncated = True
                    return self
                child = directory / name
                if self._excluded(child, False) or not matcher.fullmatch(child.relative_to(self.base).as_posix()):
                    continue
                try:
                    checked = self.jail.resolve(self.jail.relative(child), must_exist=True, expect='file')
                    size = checked.stat().st_size
                except (OSError, WorkspaceSecurityError):
                    continue
                if size > self.per_file_limit:
                    continue
                if self.scanned_bytes + size > self.scan_limit:
                    self.truncated = True
                    return self
                self.paths.append(checked)
                self.scanned_bytes += size
        self.check()
        return self
