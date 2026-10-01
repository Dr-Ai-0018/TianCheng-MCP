"""A delegated path root retains its owner's current authorization rules."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .policy import AccessPolicy
from .security import WorkspaceJail, WorkspaceSecurityError


@dataclass(frozen=True)
class AccessContext:
    root: Path
    operation: str
    policy: Callable[[], AccessPolicy]
    # A grant checks expiry/revocation/mode as well as the current static deny.
    grant_authorizer: Callable[[Path, str], None] | None = None

    def authorize(self, path: Path, operation: str | None = None) -> None:
        operation = operation or self.operation
        if self.grant_authorizer is not None:
            self.grant_authorizer(path, operation)
            return
        decision = self.policy().authorize(path, operation)
        if decision.requires_approval:
            raise PermissionError("This path requires its own external access approval")


class ContextJail(WorkspaceJail):
    def __init__(self, context: AccessContext) -> None:
        super().__init__(context.root, create=False)
        self.context = context

    def resolve(self, user_path, *, operation: str | None = None, **kwargs) -> Path:
        path = super().resolve(user_path, **kwargs)
        self.context.authorize(path, operation)
        return path

    def reject_reparse_tree(self, root: Path, *, max_entries: int = 200_000,
                            operation: str | None = None, destination: Path | None = None) -> None:
        # Validate the complete source and projected destination before the
        # first filesystem mutation. A file has a one-node tree.
        WorkspaceJail.reject_reparse_tree(self, root, max_entries=max_entries)
        self.context.authorize(root, operation)
        if destination is not None:
            self.context.authorize(destination, "write")
        if root.is_file():
            return
        inspected = 0
        for current, directories, files in os.walk(root, followlinks=False):
            for name in [*directories, *files]:
                inspected += 1
                if inspected > max_entries:
                    raise WorkspaceSecurityError("Recursive authorization scan exceeded its entry budget")
                path = Path(current) / name
                self.context.authorize(path, operation)
                if destination is not None:
                    self.context.authorize(destination / path.relative_to(root), "write")
