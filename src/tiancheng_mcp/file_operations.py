"""Coordinate overlapping filesystem mutations within this server process."""
from __future__ import annotations

import inspect
import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .security import WorkspaceJail


class PathCoordinator:
    """Reserve whole subtrees atomically, without retaining idle path locks."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: dict[object, tuple[int, tuple[Path, ...]]] = {}

    @staticmethod
    def _overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    @contextmanager
    def hold(self, paths, check_cancelled=lambda: None):
        keys = tuple(Path(os.path.normcase(os.path.abspath(path))) for path in paths)
        owner = threading.get_ident()
        token = object()
        with self._condition:
            while True:
                check_cancelled()
                if not any(
                    active_owner != owner and any(self._overlap(a, b) for a in keys for b in held)
                    for active_owner, held in self._active.values()
                ):
                    self._active[token] = (owner, keys)
                    break
                self._condition.wait(timeout=0.1)
        try:
            yield
        finally:
            with self._condition:
                del self._active[token]
                self._condition.notify_all()


# Scoped services and independently constructed services can overlap roots.
# A process-wide coordinator also covers that overlap without a root registry.
FILE_OPERATIONS = PathCoordinator()


def coordinate_mutation(*parameters: str, whole_workspace: bool = False):
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def coordinated(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            paths = [self.jail.root] if whole_workspace else []
            for name in parameters:
                label = bound.arguments[name]
                if label is not None:
                    # Determine lock keys without borrowing the delegated mode.
                    # The operation re-resolves and authorizes after acquiring.
                    target = WorkspaceJail.resolve(self.jail, label, must_exist=False, allow_root=False)
                    paths.append(target.parent)
            with FILE_OPERATIONS.hold(paths, self._check_cancelled):
                return function(self, *args, **kwargs)

        return coordinated
    return decorate
