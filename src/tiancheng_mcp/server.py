"""MCP tool registration using the official MCP Python SDK v2 API."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import PureWindowsPath
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .jobs import JobCancelled
from .service import TianChengService


_T = TypeVar("_T")


def _exception_group_message(group: BaseExceptionGroup) -> str:
    """Flatten worker/anyio exception groups into a bounded tool error.

    Exception groups must not escape a tool handler: the MCP SDK may otherwise
    terminate the stdio dispatcher instead of returning a JSON-RPC error.
    Keep the response short and never include traceback or operation payloads.
    """

    leaves: list[BaseException] = []

    def collect(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                collect(child)
        else:
            leaves.append(exc)

    collect(group)
    if not leaves:
        return "Background operation failed (exception group)"
    first = leaves[0]
    if isinstance(first, JobCancelled):
        return "Job cancelled"
    detail = str(first).strip()
    if detail:
        detail = detail[:500]
        return f"Background operation failed: {detail}"
    return f"Background operation failed: {type(first).__name__}"
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
EXECUTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
OPEN_WORLD_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
OPEN_WORLD_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def _audit_path(value: str | None) -> str | None:
    """Return only a relative, non-secret label suitable for an audit log."""

    if value is None or value == "":
        return "."
    candidate = PureWindowsPath(value.replace("/", "\\"))
    if candidate.drive or candidate.root or any(part == ".." for part in candidate.parts):
        return "<rejected-path>"
    return "/".join(part for part in candidate.parts if part != ".") or "."


def _instructions(service: TianChengService) -> str:
    """Describe the path rules that actually apply to this server.

    The two tool families take paths in different shapes, so a single static
    sentence is wrong for one of them.  Stating only the workspace rule is what
    sends a caller to ``read_text`` with an absolute path, where it gets a
    refusal that reads like the grant is broken rather than like the wrong tool.
    """

    paragraphs = [
        "Workspace tools take paths relative to the fixed workspace. For them, "
        "absolute paths, parent traversal, symlinks, junctions, and reparse "
        "points are refused."
    ]
    if service.external_grants.enabled:
        paragraphs.append(
            "The external_* tools are the opposite: they take an absolute path "
            "outside the workspace, and reach it only where the static access "
            "policy already grants it, or under a grant_id from "
            "request_external_access. Call access_policy_explain to see whether "
            "a path is covered before reading it, and workspace_info to list "
            "every granted directory. Git tools are workspace-only; for a "
            "repository outside the workspace use external_run_command with "
            "command \"git\"."
        )
    paragraphs.append(
        "Delete moves items to .tiancheng-trash instead of permanently "
        "destroying them. Tools that run longer than the interactive budget "
        "automatically return a job_id; use job_status, job_result, and "
        "job_cancel to continue or stop them."
    )
    return " ".join(paragraphs)


def create_server(service: TianChengService) -> MCPServer:
    mcp = MCPServer(
        name="tiancheng-local-mcp",
        title="TianCheng Local MCP",
        description="Workspace-jailed local file and Git tools for the configured workspace",
        version=__version__,
        instructions=_instructions(service),
    )

    expected_errors = (
        FileNotFoundError,
        FileExistsError,
        IsADirectoryError,
        NotADirectoryError,
        PermissionError,
        TimeoutError,
        JobCancelled,
        RuntimeError,
        OSError,
        ValueError,
    )

    def call_label(
        tool: str,
        label: str | None,
        operation: Callable[[], _T],
        *,
        job_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> _T | dict[str, Any]:
        try:
            return service.run_with_fallback(
                tool,
                label,
                operation,
                metadata=job_metadata,
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
            )
        except expected_errors as exc:
            raise ToolError(str(exc)) from exc
        except BaseExceptionGroup as exc:
            # A worker can surface an anyio/TaskGroup ExceptionGroup after a
            # cancellation or subprocess failure.  Convert it to a normal
            # protocol-level tool error so one bad call cannot kill stdio.
            raise ToolError(_exception_group_message(exc)) from exc

    def call(tool: str, path: str | None, operation: Callable[[], _T]) -> _T:
        return call_label(tool, _audit_path(path), operation)

    def external_tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Only expose external-capability tools under the explicit feature flag."""
        if service.external_grants.enabled:
            return mcp.tool(**kwargs)
        return lambda function: function

    def external_call(
        tool: str,
        grant_id: str,
        operation: Callable[[], _T],
        *,
        idempotency_key: str | None = None,
        fingerprint_payload: Any = None,
    ) -> _T | dict[str, Any]:
        return call_label(
            tool,
            "<external-grant>",
            operation,
            job_metadata={"grant_id": grant_id},
            idempotency_key=idempotency_key,
            idempotency_fingerprint=(
                side_effect_fingerprint(tool, fingerprint_payload)
                if idempotency_key is not None
                else None
            ),
        )

    def side_effect_fingerprint(tool: str, payload: Any) -> str:
        """Hash operation inputs without logging or persisting their contents."""

        encoded = json.dumps(
            {"tool": tool, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @mcp.tool(
        description=(
            "Show the fixed workspace, server version, enabled local capabilities, and "
            "every directory the static access policy grants."
        ),
        annotations=READ_ONLY,
    )
    def workspace_info() -> dict[str, Any]:
        return call("workspace_info", ".", service.workspace_info)

    @mcp.tool(
        description=(
            "Query the metadata-only local agent conversation catalog. Actions: providers, "
            "sources, refresh, list, inspect. refresh and inspect take a source_id or "
            "conversation_ref obtained from sources/list; call those first. Sources are "
            "configured only from the local TianCheng control surface; this tool cannot add "
            "paths or read raw transcripts. refresh may continue as a background job."
        ),
        annotations=READ_ONLY,
    )
    def agent_catalog(
        action: str,
        source_id: str = "",
        provider: str = "",
        conversation_ref: str = "",
        query: str = "",
        cursor: int = 0,
        limit: int = 50,
        max_bytes: int = 131072,
    ) -> dict[str, Any]:
        if action == "providers":
            return call_label(
                "agent_catalog_providers",
                "<agent-catalog>",
                service.agent_catalog_providers,
            )
        if action == "sources":
            return call_label(
                "agent_catalog_sources",
                "<agent-catalog>",
                service.agent_catalog_sources,
            )
        if action == "refresh":
            if not source_id:
                # "source_id is invalid" tells a caller nothing about where to
                # get one, and refreshing every source implicitly could be a
                # long unbounded scan the caller did not ask for.
                raise ValueError(
                    "refresh requires source_id; call action='sources' first to "
                    "list the configured source ids"
                )
            return call_label(
                "agent_catalog_refresh",
                "<agent-source>",
                lambda: service.agent_catalog_refresh(source_id),
            )
        if action == "list":
            return call_label(
                "agent_catalog_list",
                "<agent-catalog>",
                lambda: service.agent_catalog_list(
                    provider, source_id, query, cursor, limit, max_bytes
                ),
            )
        if action == "inspect":
            return call_label(
                "agent_catalog_inspect",
                "<agent-conversation>",
                lambda: service.agent_catalog_inspect(conversation_ref),
            )
        raise ValueError(
            "action must be providers, sources, refresh, list, or inspect"
        )

    @mcp.tool(
        description=(
            "Return the status of a request that automatically continued as a background job. "
            "This is always a lightweight operation."
        ),
        annotations=READ_ONLY,
    )
    def job_status(job_id: str) -> dict[str, Any]:
        return call_label("job_status", "<job>", lambda: service.job_status(job_id))

    @mcp.tool(
        description=(
            "Return the completed result of a background job, subject to a byte limit. "
            "If it is still running, return ready=false. List-like results support cursor/max_items."
        ),
        annotations=READ_ONLY,
    )
    def job_result(
        job_id: str,
        cursor: int = 0,
        max_items: int = 100,
        max_bytes: int = 262144,
    ) -> dict[str, Any]:
        return call_label(
            "job_result",
            "<job>",
            lambda: service.job_result(
                job_id,
                cursor=cursor,
                max_items=max_items,
                max_bytes=max_bytes,
            ),
        )

    @mcp.tool(
        description="Cancel a queued or running background job and request cleanup of its worker.",
        annotations=DESTRUCTIVE,
    )
    def job_cancel(job_id: str, reason: str = "") -> dict[str, Any]:
        return call_label("job_cancel", "<job>", lambda: service.job_cancel(job_id, reason))

    @mcp.tool(
        description="List retained background jobs without exposing command arguments or file contents.",
        annotations=READ_ONLY,
    )
    def job_list(include_finished: bool = True, limit: int = 50) -> dict[str, Any]:
        return call_label("job_list", "<jobs>", lambda: service.job_list(include_finished, limit))

    @mcp.tool(
        description="Explain which static access-policy rule matches an absolute path and operation.",
        annotations=READ_ONLY,
    )
    def access_policy_explain(path: str, operation: str = "read") -> dict[str, Any]:
        return call_label(
            "access_policy_explain",
            path,
            lambda: service.access_policy_explain(path, operation),
        )

    @mcp.tool(
        description="Reload and validate access-policy.json atomically; failed reload keeps the current policy.",
        annotations=WRITE,
    )
    def access_policy_reload() -> dict[str, Any]:
        return call_label("access_policy_reload", "<access-policy>", service.reload_access_policy)

    if service.allow_policy_hot_reload:

        @mcp.tool(
            description=(
                "Stage a request to widen the access policy so agents and external file "
                "tools may use additional local directories. This grants nothing by "
                "itself: it records the paths and mode and returns a one-time challenge. "
                "Show the user the exact paths and mode and wait for their reply, then "
                "call access_policy_change_confirm. The staged capability is frozen here "
                "and cannot be raised at confirmation time. The server's own code, "
                "policy and log directories, system directories, filesystem roots, "
                "credential-named paths, and anything an explicit deny rule covers are "
                "always refused."
            ),
            annotations=WRITE_IDEMPOTENT,
        )
        def access_policy_change_request(
            paths: list[str],
            mode: str = "read",
            allow_exec: bool = False,
            note: str = "",
        ) -> dict[str, Any]:
            return call_label(
                "access_policy_change_request",
                "<access-policy>",
                lambda: service.policy_change_request(paths, mode, allow_exec, note),
            )

        @mcp.tool(
            description=(
                "Commit one directory-access request that access_policy_change_request "
                "already staged and the user has reviewed. The effect is to widen the "
                "access policy immediately, without a restart. This tool cannot choose, "
                "change, or broaden paths or permissions: it only commits the pending "
                "request named by request_id, whose capability was fixed when it was "
                "staged. It requires that request's one-time challenge and the user's "
                "own confirmation word, which is recorded with the call."
            ),
            annotations=WRITE_IDEMPOTENT,
        )
        def access_policy_change_confirm(
            request_id: str, challenge: str, confirmation: str = ""
        ) -> dict[str, Any]:
            return call_label(
                "access_policy_change_approve",
                "<access-policy>",
                lambda: service.policy_change_approve(
                    request_id, challenge, confirmation
                ),
            )

        @mcp.tool(
            description=(
                "Discard one staged directory-access request without applying it. The "
                "access policy is left unchanged."
            ),
            annotations=WRITE_IDEMPOTENT,
        )
        def access_policy_change_cancel(request_id: str) -> dict[str, Any]:
            return call_label(
                "access_policy_change_cancel",
                "<access-policy>",
                lambda: service.policy_change_cancel(request_id),
            )

        @mcp.tool(
            description=(
                "List directory-access requests that are staged and still awaiting the "
                "user's decision, with their paths, mode and expiry. Read-only."
            ),
            annotations=READ_ONLY,
        )
        def access_policy_change_status() -> dict[str, Any]:
            return call_label(
                "access_policy_change_status",
                "<access-policy>",
                service.policy_change_status,
            )

    @external_tool(
        description=(
            "Request a temporary capability for an absolute directory outside the workspace. "
            "The request remains pending until approve_external_access receives the one-time "
            "challenge and explicit confirmation='批准'. This does not grant access by itself."
        ),
        annotations=WRITE_IDEMPOTENT,
    )
    def request_external_access(
        path: str,
        mode: str = "read",
        ttl_seconds: int = 600,
        reason: str = "",
    ) -> dict[str, object]:
        return call_label(
            "request_external_access",
            "<external-request>",
            lambda: service.request_external_access(path, mode, ttl_seconds, reason),
        )

    @external_tool(
        description=(
            "Approve one pending external access request after explicit user confirmation. "
            "Submit the one-time non-secret challenge returned by request_external_access "
            "and confirmation='批准'. The resulting grant expires automatically."
        ),
        annotations=WRITE,
    )
    def approve_external_access(request_id: str, challenge: str, confirmation: str = "") -> dict[str, object]:
        return call_label(
            "approve_external_access",
            "<external-request>",
            lambda: service.approve_external_access(request_id, challenge, confirmation),
        )

    @external_tool(
        description="List pending requests and active temporary external grants.",
        annotations=READ_ONLY,
    )
    def external_grant_status() -> dict[str, object]:
        return call_label(
            "external_grant_status",
            "<external-grants>",
            service.external_grant_status,
        )

    @external_tool(
        description="Immediately revoke one active external grant; subsequent calls fail.",
        annotations=DESTRUCTIVE,
    )
    def revoke_external_access(grant_id: str) -> dict[str, object]:
        return call_label(
            "revoke_external_access",
            "<external-grant>",
            lambda: service.revoke_external_access(grant_id),
        )

    @external_tool(
        description="Cancel a pending external access request before it is approved.",
        annotations=DESTRUCTIVE,
    )
    def cancel_external_access_request(request_id: str) -> dict[str, object]:
        return call_label(
            "cancel_external_access_request",
            "<external-request>",
            lambda: service.cancel_external_access_request(request_id),
        )

    @external_tool(
        description="List using grant_id, or an absolute path covered by a no-approval static policy rule.",
        annotations=READ_ONLY,
    )
    def external_list_dir(path: str = ".", depth: int = 1, grant_id: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_list_dir", grant_id, lambda: service.external_list_dir(grant_id, path, depth))
        return call_label("external_list_dir", path, lambda: service.policy_external_list_dir(path, depth))

    @external_tool(description="Return metadata using grant_id, or an absolute path covered by a no-approval static policy rule.", annotations=READ_ONLY)
    def external_stat(path: str, grant_id: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_stat", grant_id, lambda: service.external_stat(grant_id, path))
        return call_label("external_stat", path, lambda: service.policy_external_stat(path))

    @external_tool(description="Read UTF-8 text using grant_id, or an absolute path covered by a no-approval static policy rule.", annotations=READ_ONLY)
    def external_read_text(path: str, start_line: int | None = None, end_line: int | None = None, max_bytes: int = 262144, grant_id: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_read_text", grant_id, lambda: service.external_read_text(grant_id, path, start_line, end_line, max_bytes))
        return call_label("external_read_text", path, lambda: service.policy_external_read_text(path, start_line, end_line, max_bytes))

    @external_tool(description="Read a source-byte chunk using grant_id, or an absolute path covered by a no-approval static policy rule.", annotations=READ_ONLY)
    def external_read_text_chunk(path: str, offset_bytes: int = 0, max_bytes: int = 262144, grant_id: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_read_text_chunk", grant_id, lambda: service.external_read_text_chunk(grant_id, path, offset_bytes, max_bytes))
        return call_label("external_read_text_chunk", path, lambda: service.policy_external_read_text_chunk(path, offset_bytes, max_bytes))

    @external_tool(description="Create or replace UTF-8 text using grant_id, or an absolute path covered by a no-approval writable static policy rule.", annotations=WRITE_IDEMPOTENT)
    def external_write_text(path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_write_text", grant_id, lambda: service.external_write_text(grant_id, path, content, create_parents, expected_sha256), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, path, content, create_parents, expected_sha256])
        return call_label("external_write_text", path, lambda: service.policy_external_write_text(path, content, create_parents, expected_sha256), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_write_text", [path, content, create_parents, expected_sha256]))

    @external_tool(description="Append UTF-8 text using grant_id, or an absolute path covered by a no-approval writable static policy rule.", annotations=WRITE)
    def external_append_text(path: str, content: str, create_parents: bool = True, expected_sha256: str | None = None, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_append_text", grant_id, lambda: service.external_append_text(grant_id, path, content, create_parents, expected_sha256), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, path, content, create_parents, expected_sha256])
        return call_label("external_append_text", path, lambda: service.policy_external_append_text(path, content, create_parents, expected_sha256), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_append_text", [path, content, create_parents, expected_sha256]))

    @external_tool(description="Create a directory using grant_id, or an absolute path covered by a no-approval writable static policy rule.", annotations=WRITE_IDEMPOTENT)
    def external_mkdir(path: str, parents: bool = True, exist_ok: bool = True, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_mkdir", grant_id, lambda: service.external_mkdir(grant_id, path, parents, exist_ok), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, path, parents, exist_ok])
        return call_label("external_mkdir", path, lambda: service.policy_external_mkdir(path, parents, exist_ok), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_mkdir", [path, parents, exist_ok]))

    @external_tool(description="Move or rename using grant_id, or absolute paths covered by one no-approval writable static policy rule; never overwrites.", annotations=WRITE)
    def external_move(source: str, destination: str, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_move", grant_id, lambda: service.external_move(grant_id, source, destination), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, source, destination])
        return call_label("external_move", f"{source} -> {destination}", lambda: service.policy_external_move(source, destination), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_move", [source, destination]))

    @external_tool(description="Copy using grant_id, or absolute paths covered by one no-approval writable static policy rule; never overwrites.", annotations=WRITE)
    def external_copy(source: str, destination: str, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_copy", grant_id, lambda: service.external_copy(grant_id, source, destination), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, source, destination])
        return call_label("external_copy", f"{source} -> {destination}", lambda: service.policy_external_copy(source, destination), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_copy", [source, destination]))

    @external_tool(description="Move an item to trash using grant_id, or an absolute path covered by a no-approval writable static policy rule.", annotations=DESTRUCTIVE)
    def external_delete(path: str, grant_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if grant_id:
            return external_call("external_delete", grant_id, lambda: service.external_delete(grant_id, path), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, path])
        return call_label("external_delete", path, lambda: service.policy_external_delete(path), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_delete", [path]))

    @external_tool(description="Find paths using grant_id, or an absolute base_path covered by a no-approval static policy rule.", annotations=READ_ONLY)
    def external_glob(
        pattern: str, max_results: int = 200, base_path: str = ".", grant_id: str | None = None
    ) -> dict[str, Any]:
        if grant_id:
            return external_call("external_glob", grant_id, lambda: service.external_glob(grant_id, pattern, max_results, base_path))
        return call_label("external_glob", base_path, lambda: service.policy_external_glob(pattern, max_results, base_path))

    @external_tool(description="Search text using grant_id, or an absolute base_path covered by a no-approval static policy rule.", annotations=READ_ONLY)
    def external_search_text(
        query: str,
        glob_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = 100,
        max_scan_bytes: int = 32 * 1024 * 1024,
        include_hidden: bool = True,
        timeout_seconds: int = 30,
        base_path: str = ".",
        grant_id: str | None = None,
    ) -> dict[str, Any]:
        if grant_id:
            return external_call("external_search_text", grant_id, lambda: service.external_search_text(grant_id, query, glob_pattern, case_sensitive, max_results, max_scan_bytes, include_hidden, timeout_seconds, base_path))
        return call_label("external_search_text", base_path, lambda: service.policy_external_search_text(query, glob_pattern, case_sensitive, max_results, max_scan_bytes, include_hidden, timeout_seconds, base_path))

    if service.external_grants.enabled:

        @mcp.tool(
            description="Run an allowlisted command with grant_id, or an absolute cwd covered by a no-approval static exec policy rule.",
            annotations=EXECUTION,
        )
        def external_run_command(
            command: str,
            args: list[str] | None = None,
            cwd: str = ".",
            timeout_seconds: int = 60,
            max_output_bytes: int = 262144,
            grant_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            if grant_id:
                return external_call("external_run_command", grant_id, lambda: service.external_run_command(grant_id, command, args, cwd, timeout_seconds, max_output_bytes), idempotency_key=idempotency_key, fingerprint_payload=[grant_id, command, args or [], cwd, timeout_seconds, max_output_bytes])
            return call_label("external_run_command", cwd, lambda: service.policy_external_run_command(command, args, cwd, timeout_seconds, max_output_bytes), idempotency_key=idempotency_key, idempotency_fingerprint=side_effect_fingerprint("external_run_command", [command, args or [], cwd, timeout_seconds, max_output_bytes]))

    @mcp.tool(
        description="List a workspace-relative directory with bounded recursive depth (1-5).",
        annotations=READ_ONLY,
    )
    def list_dir(path: str = ".", depth: int = 1) -> dict[str, Any]:
        return call("list_dir", path, lambda: service.list_dir(path, depth))

    @mcp.tool(
        description="Return basic metadata for one workspace-relative file or directory.",
        annotations=READ_ONLY,
    )
    def stat(path: str) -> dict[str, Any]:
        return call("stat", path, lambda: service.stat(path))

    @mcp.tool(
        description="Return a bounded SHA-256 hash and size for one workspace-relative file.",
        annotations=READ_ONLY,
    )
    def hash_file(path: str, max_bytes: int = 268435456) -> dict[str, Any]:
        return call("hash_file", path, lambda: service.hash_file(path, max_bytes))

    @mcp.tool(
        description=(
            "Read a UTF text file with a byte cap. Optional start_line/end_line are 1-based "
            "and useful for large files; binary content is refused."
        ),
        annotations=READ_ONLY,
    )
    def read_text(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int = 262144,
    ) -> dict[str, Any]:
        return call(
            "read_text",
            path,
            lambda: service.read_text(path, start_line, end_line, max_bytes),
        )

    @mcp.tool(
        description=(
            "Read a large UTF text file by source-byte cursor. Continue with the returned "
            "next_offset_bytes; binary content and invalid cursor boundaries are refused."
        ),
        annotations=READ_ONLY,
    )
    def read_text_chunk(
        path: str,
        offset_bytes: int = 0,
        max_bytes: int = 262144,
    ) -> dict[str, Any]:
        return call(
            "read_text_chunk",
            path,
            lambda: service.read_text_chunk(path, offset_bytes, max_bytes),
        )

    @mcp.tool(
        description="Atomically create or replace a UTF-8 text file inside the workspace.",
        annotations=WRITE_IDEMPOTENT,
    )
    def write_text(
        path: str,
        content: str,
        create_parents: bool = True,
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call_label(
            "write_text",
            path,
            lambda: service.write_text(path, content, create_parents, expected_sha256),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint(
                "write_text", [path, content, create_parents, expected_sha256]
            ),
        )

    @mcp.tool(
        description=(
            "Atomically replace exact text only when the match count and optional SHA-256 "
            "precondition agree, preventing ambiguous or stale edits."
        ),
        annotations=WRITE_IDEMPOTENT,
    )
    def edit_text(
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call_label(
            "edit_text",
            path,
            lambda: service.edit_text(
                path, old_text, new_text, expected_replacements, expected_sha256
            ),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint(
                "edit_text", [path, old_text, new_text, expected_replacements, expected_sha256]
            ),
        )

    @mcp.tool(
        description=(
            "Append UTF-8 text to a workspace file, optionally creating parents; an optional "
            "SHA-256 precondition prevents appending to stale content."
        ),
        annotations=WRITE,
    )
    def append_text(
        path: str,
        content: str,
        create_parents: bool = True,
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call_label(
            "append_text",
            path,
            lambda: service.append_text(path, content, create_parents, expected_sha256),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint(
                "append_text", [path, content, create_parents, expected_sha256]
            ),
        )

    @mcp.tool(
        description="Create a workspace-relative directory.",
        annotations=WRITE,
    )
    def mkdir(
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call_label(
            "mkdir", path, lambda: service.mkdir(path, parents, exist_ok),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("mkdir", [path, parents, exist_ok]),
        )

    @mcp.tool(
        description="Move or rename a file/directory inside the workspace; never overwrites.",
        annotations=WRITE,
    )
    def move(source: str, destination: str, idempotency_key: str | None = None) -> dict[str, Any]:
        label = f"{_audit_path(source)} -> {_audit_path(destination)}"
        return call_label(
            "move", label, lambda: service.move(source, destination),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("move", [source, destination]),
        )

    @mcp.tool(
        description="Copy a file/directory inside the workspace; never overwrites.",
        annotations=WRITE,
    )
    def copy(source: str, destination: str, idempotency_key: str | None = None) -> dict[str, Any]:
        label = f"{_audit_path(source)} -> {_audit_path(destination)}"
        return call_label(
            "copy", label, lambda: service.copy(source, destination),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("copy", [source, destination]),
        )

    @mcp.tool(
        description=(
            "Remove a file/directory by moving it to .tiancheng-trash. This is marked "
            "destructive even though the first version does not permanently erase data."
        ),
        annotations=DESTRUCTIVE,
    )
    def delete(path: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return call_label(
            "delete", path, lambda: service.delete(path),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("delete", [path]),
        )

    @mcp.tool(
        description="List recoverable items in .tiancheng-trash without reading their contents.",
        annotations=READ_ONLY,
    )
    def trash_list(max_results: int = 200) -> dict[str, Any]:
        return call("trash_list", ".tiancheng-trash", lambda: service.trash_list(max_results))

    @mcp.tool(
        description=(
            "Restore one direct trash item to its recorded original path or an explicit "
            "workspace-relative destination; existing files are never overwritten."
        ),
        annotations=WRITE,
    )
    def trash_restore(
        trash_path: str, destination: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        label = f"{_audit_path(trash_path)} -> {_audit_path(destination)}"
        return call_label(
            "trash_restore", label, lambda: service.trash_restore(trash_path, destination),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint(
                "trash_restore", [trash_path, destination]
            ),
        )

    @mcp.tool(
        description=(
            "Permanently purge one direct trash item, or all items when trash_path is omitted. "
            "This cannot be undone and refuses reparse-point trees."
        ),
        annotations=DESTRUCTIVE,
    )
    def trash_purge(
        trash_path: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return call_label(
            "trash_purge",
            trash_path or ".tiancheng-trash",
            lambda: service.trash_purge(trash_path),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("trash_purge", [trash_path]),
        )

    @mcp.tool(
        description="Find workspace entries with a bounded glob, optionally rooted at base_path.",
        annotations=READ_ONLY,
    )
    def glob(pattern: str, max_results: int = 200, base_path: str = ".") -> dict[str, Any]:
        return call("glob", base_path, lambda: service.glob(pattern, max_results, base_path))

    @mcp.tool(
        description=(
            "Search bounded UTF text files. base_path narrows enumeration before glob_pattern "
            "filtering; results contain relative path, 1-based line number, and short context."
        ),
        annotations=READ_ONLY,
    )
    def search_text(
        query: str,
        glob_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = 100,
        max_scan_bytes: int = 33554432,
        include_hidden: bool = True,
        respect_gitignore: bool = True,
        include_internal: bool = False,
        timeout_seconds: int = 30,
        base_path: str = ".",
    ) -> dict[str, Any]:
        return call(
            "search_text",
            base_path,
            lambda: service.search_text(
                query,
                glob_pattern,
                case_sensitive,
                max_results,
                max_scan_bytes,
                include_hidden,
                respect_gitignore,
                include_internal,
                timeout_seconds,
                base_path,
            ),
        )

    @mcp.tool(
        description="Return concise local Git status for a repository inside the workspace.",
        annotations=READ_ONLY,
    )
    def git_status(repo: str = ".") -> dict[str, Any]:
        return call("git_status", repo, lambda: service.git_status(repo))

    @mcp.tool(
        description="Return a bounded unstaged or staged local Git diff.",
        annotations=READ_ONLY,
    )
    def git_diff(
        repo: str = ".",
        staged: bool = False,
        path: str | None = None,
        max_bytes: int = 524288,
    ) -> dict[str, Any]:
        return call(
            "git_diff", repo, lambda: service.git_diff(repo, staged, path, max_bytes)
        )

    @mcp.tool(
        description="Return recent local commits (maximum 100) without contacting remotes.",
        annotations=READ_ONLY,
    )
    def git_log(repo: str = ".", limit: int = 20) -> dict[str, Any]:
        return call("git_log", repo, lambda: service.git_log(repo, limit))

    @mcp.tool(
        description="Initialize a standalone local Git repository inside the workspace.",
        annotations=WRITE,
    )
    def git_init(path: str = ".", idempotency_key: str | None = None) -> dict[str, Any]:
        return call_label(
            "git_init", path, lambda: service.git_init(path),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("git_init", [path]),
        )

    @mcp.tool(
        description="Stage one or more repository-relative paths; no remote operations.",
        annotations=WRITE_IDEMPOTENT,
    )
    def git_add(
        paths: list[str], repo: str = ".", idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return call_label(
            "git_add", repo, lambda: service.git_add(paths, repo),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint("git_add", [paths, repo]),
        )

    @mcp.tool(
        description=(
            "Create a local commit with optional explicit identity. By default it uses the "
            "repository/global Git identity and falls back to a non-personal local identity."
        ),
        annotations=WRITE,
    )
    def git_commit(
        message: str,
        repo: str = ".",
        author_name: str | None = None,
        author_email: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call_label(
            "git_commit",
            repo,
            lambda: service.git_commit(message, repo, author_name, author_email),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=side_effect_fingerprint(
                "git_commit", [message, repo, author_name, author_email]
            ),
        )

    if service.allow_exec:

        @mcp.tool(
            description="List configured Git remotes without exposing embedded credentials.",
            annotations=READ_ONLY,
        )
        def git_remote_list(repo: str = ".") -> dict[str, Any]:
            return call("git_remote_list", repo, lambda: service.git_remote_list(repo))

        @mcp.tool(
            description="Add a Git remote using HTTPS/SSH/Git or a workspace-local path.",
            annotations=WRITE,
        )
        def git_remote_add(
            name: str, url: str, repo: str = ".", idempotency_key: str | None = None
        ) -> dict[str, Any]:
            return call_label(
                "git_remote_add", repo, lambda: service.git_remote_add(name, url, repo),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_remote_add", [name, url, repo]
                ),
            )

        @mcp.tool(
            description="Change a Git remote URL; embedded credentials are refused.",
            annotations=WRITE,
        )
        def git_remote_set_url(
            name: str, url: str, repo: str = ".", idempotency_key: str | None = None
        ) -> dict[str, Any]:
            return call_label(
                "git_remote_set_url", repo, lambda: service.git_remote_set_url(name, url, repo),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_remote_set_url", [name, url, repo]
                ),
            )

        @mcp.tool(
            description="Remove a local Git remote configuration without contacting the remote.",
            annotations=DESTRUCTIVE,
        )
        def git_remote_remove(
            name: str, repo: str = ".", idempotency_key: str | None = None
        ) -> dict[str, Any]:
            return call_label(
                "git_remote_remove", repo, lambda: service.git_remote_remove(name, repo),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_remote_remove", [name, repo]
                ),
            )

        @mcp.tool(
            description="Clone a Git repository into a new workspace-relative directory.",
            annotations=OPEN_WORLD_WRITE,
        )
        def git_clone(
            url: str, destination: str, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            return call_label(
                "git_clone", destination, lambda: service.git_clone(url, destination),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_clone", [url, destination]
                ),
            )

        @mcp.tool(
            description="Fetch remote refs using the user's existing global Git/GCM identity.",
            annotations=OPEN_WORLD_WRITE,
        )
        def git_fetch(
            repo: str = ".",
            remote: str = "origin",
            prune: bool = True,
            tags: bool = False,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return call_label(
                "git_fetch", repo, lambda: service.git_fetch(repo, remote, prune, tags),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_fetch", [repo, remote, prune, tags]
                ),
            )

        @mcp.tool(
            description="Pull from a remote with explicit ff-only, rebase, or merge strategy.",
            annotations=OPEN_WORLD_DESTRUCTIVE,
        )
        def git_pull(
            repo: str = ".",
            remote: str = "origin",
            branch: str | None = None,
            strategy: str = "ff-only",
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return call_label(
                "git_pull",
                repo,
                lambda: service.git_pull(repo, remote, branch, strategy),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_pull", [repo, remote, branch, strategy]
                ),
            )

        @mcp.tool(
            description=(
                "Push commits/tags using global Git Credential Manager. Plain --force and "
                "remote deletion are unavailable; force_with_lease is explicit."
            ),
            annotations=OPEN_WORLD_DESTRUCTIVE,
        )
        def git_push(
            repo: str = ".",
            remote: str = "origin",
            branch: str | None = None,
            set_upstream: bool = False,
            force_with_lease: bool = False,
            tags: bool = False,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return call_label(
                "git_push",
                repo,
                lambda: service.git_push(
                    repo, remote, branch, set_upstream, force_with_lease, tags
                ),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "git_push", [repo, remote, branch, set_upstream, force_with_lease, tags]
                ),
            )

        @mcp.tool(
            description=(
                "Run an allowlisted developer command with separated args, a workspace cwd, "
                "global Git/GCM access, a secret-scrubbed environment, process-tree timeout, "
                "and bounded output. Credential-printing commands are blocked. This is not an "
                "OS sandbox."
            ),
            annotations=EXECUTION,
        )
        def run_command(
            command: str,
            args: list[str] | None = None,
            cwd: str = ".",
            timeout_seconds: int = 60,
            max_output_bytes: int = 262144,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return call_label(
                "run_command",
                cwd,
                lambda: service.run_command(
                    command, args, cwd, timeout_seconds, max_output_bytes
                ),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=side_effect_fingerprint(
                    "run_command", [command, args or [], cwd, timeout_seconds, max_output_bytes]
                ),
            )

        @mcp.tool(
            description=(
                "Start an allowlisted long-running developer process in a workspace cwd. "
                "Output is kept only in bounded memory and the process has a hard lifetime."
            ),
            annotations=EXECUTION,
        )
        def start_process(
            command: str,
            args: list[str] | None = None,
            cwd: str = ".",
            max_runtime_seconds: int = 3600,
            output_limit_bytes: int = 524288,
        ) -> dict[str, Any]:
            return call(
                "start_process",
                cwd,
                lambda: service.start_process(
                    command, args, cwd, max_runtime_seconds, output_limit_bytes
                ),
            )

        @mcp.tool(
            description="Return status for one process managed by this MCP session.",
            annotations=READ_ONLY,
        )
        def process_status(process_id: str) -> dict[str, Any]:
            return call_label(
                "process_status", "<managed-process>", lambda: service.process_status(process_id)
            )

        @mcp.tool(
            description="List managed background processes without exposing their arguments.",
            annotations=READ_ONLY,
        )
        def list_processes(include_exited: bool = True) -> dict[str, Any]:
            return call_label(
                "list_processes", "<managed-processes>", lambda: service.list_processes(include_exited)
            )

        @mcp.tool(
            description=(
                "Read bounded stdout/stderr for one managed process. Use after_bytes with the "
                "returned per-stream next_offset_bytes fields for incremental reads; a cursor "
                "gap means older output was evicted from the bounded buffer."
            ),
            annotations=READ_ONLY,
        )
        def process_output(
            process_id: str,
            stream: str = "both",
            max_bytes: int = 262144,
            after_bytes: int = 0,
        ) -> dict[str, Any]:
            return call_label(
                "process_output",
                "<managed-process>",
                lambda: service.process_output(process_id, stream, max_bytes, after_bytes),
            )

        @mcp.tool(
            description=(
                "Send bounded UTF-8 text to a managed process stdin. Optionally close stdin "
                "after writing; input is never persisted in audit logs."
            ),
            annotations=EXECUTION,
        )
        def process_input(
            process_id: str, input_text: str, close_stdin: bool = False
        ) -> dict[str, Any]:
            return call_label(
                "process_input",
                "<managed-process>",
                lambda: service.process_input(process_id, input_text, close_stdin),
            )

        @mcp.tool(
            description="Stop one managed process and its Windows child-process tree.",
            annotations=DESTRUCTIVE,
        )
        def stop_process(process_id: str, force: bool = False) -> dict[str, Any]:
            return call_label(
                "stop_process",
                "<managed-process>",
                lambda: service.stop_process(process_id, force),
            )

        @mcp.tool(
            description=(
                "Manage a server-owned local agent session. Actions: create, attach, list, "
                "inspect, close. attach accepts only an agent_catalog conversation_ref; callers "
                "cannot supply a native session id or history path. cwd must be inside the "
                "TianCheng workspace or a directory the static access policy already covers, "
                "and the sandbox is limited to read-only or workspace-write."
            ),
            annotations=EXECUTION,
        )
        def agent_session(
            action: str,
            session_id: str = "",
            profile: str = "codex-default",
            cwd: str = ".",
            sandbox: str = "read-only",
            conversation_ref: str = "",
        ) -> dict[str, Any]:
            if action == "create":
                return call_label(
                    "agent_session_create",
                    cwd,
                    lambda: service.agent_session_create(profile, cwd, sandbox),
                )
            if action == "attach":
                return call_label(
                    "agent_session_attach",
                    "<agent-conversation>",
                    lambda: service.agent_session_attach(
                        conversation_ref, profile, sandbox
                    ),
                )
            if action == "list":
                return call_label("agent_session_list", "<agent-sessions>", service.agent_session_list)
            if action == "inspect":
                return call_label(
                    "agent_session_inspect", "<agent-session>", lambda: service.agent_session_inspect(session_id)
                )
            if action == "close":
                return call_label(
                    "agent_session_close", "<agent-session>", lambda: service.agent_session_close(session_id)
                )
            raise ValueError(
                "action must be create, attach, list, inspect, or close"
            )

        @mcp.tool(
            description=(
                "Run the prompt in a managed local agent session. Actions: start, inspect, "
                "events, result, cancel. start returns immediately with run_id/process_id; "
                "events supports bounded cursor paging and wait_ms up to 10000 ms."
            ),
            annotations=EXECUTION,
        )
        def agent_run(
            action: str,
            session_id: str,
            run_id: str = "",
            prompt: str = "",
            after_seq: int = 0,
            limit: int = 100,
            max_bytes: int = 65536,
            wait_ms: int = 0,
            reason: str = "",
        ) -> dict[str, Any]:
            if action == "start":
                return call_label(
                    "agent_run_start",
                    "<agent-session>",
                    lambda: service.agent_run_start(session_id, prompt),
                )
            if action == "inspect":
                return call_label(
                    "agent_run_inspect",
                    "<agent-run>",
                    lambda: service.agent_run_inspect(session_id, run_id),
                )
            if action == "events":
                return call_label(
                    "agent_run_events",
                    "<agent-run>",
                    lambda: service.agent_run_events(
                        session_id, run_id, after_seq, limit, max_bytes, wait_ms
                    ),
                )
            if action == "result":
                return call_label(
                    "agent_run_result",
                    "<agent-run>",
                    lambda: service.agent_run_result(session_id, run_id, max_bytes),
                )
            if action == "cancel":
                return call_label(
                    "agent_run_cancel",
                    "<agent-run>",
                    lambda: service.agent_run_cancel(session_id, run_id, reason),
                )
            raise ValueError("action must be start, inspect, events, result, or cancel")

    return mcp
