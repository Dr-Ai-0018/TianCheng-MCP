"""Server-owned, startup-only policy for ordinary command execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

MAX_POLICY_BYTES = 1024 * 1024
BUILTINS = frozenset({"python", "pytest", "py", "uv", "git", "gh", "node", "rg", "npm", "npx", "codex", "pwsh", "powershell", "cmd", "bash", "sh"})


class CommandPolicyError(ValueError):
    pass


def execution_denied(code: str, reason: str, hint: str) -> PermissionError:
    """Keep exception compatibility and never include request arguments/paths."""
    error = PermissionError(f"[{code}] {reason}；{hint}")
    error.reason_code = code
    return error


def command_name(value: object) -> str:
    if not isinstance(value, str):
        raise CommandPolicyError("Command name must be text")
    name = value.casefold().removesuffix(".exe")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise CommandPolicyError("Command name must be a bounded alias, not a path")
    return name


def disabled_name(value: object) -> str:
    """A direct-entry filename, including punctuation used by native tools."""
    if not isinstance(value, str):
        raise CommandPolicyError("Disabled command name must be text")
    name = value.casefold().removesuffix(".exe")
    if not name or len(name) > 255 or name in {".", ".."} or any(token in name for token in ("/", "\\", ":", "\x00")):
        raise CommandPolicyError("Disabled command must be a bounded filename, not a path")
    return name


def trusted_path(path: Path, workspace: Path | None, label: str) -> Path:
    """Check original ancestors before resolving, including Windows junctions."""
    if not path.is_absolute():
        raise CommandPolicyError(f"{label} must be an absolute path")
    for component in (path, *path.parents):
        if os.path.lexists(component):
            details = component.lstat()
            if component.is_symlink() or getattr(details, "st_file_attributes", 0) & 0x400:
                raise CommandPolicyError(f"{label} cannot traverse a link or reparse point")
    location = path.resolve(strict=False)
    if workspace is not None and location.is_relative_to(workspace.resolve()):
        raise CommandPolicyError(f"{label} must be outside the workspace")
    return location


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise CommandPolicyError("Duplicate command policy field")
        result[key] = value
    return result


def read_document(path: Path, workspace: Path | None = None, *, optional: bool = False) -> dict:
    path = trusted_path(path, workspace, "Command policy")
    if optional and not path.exists():
        return {"schema_version": 1}
    if not path.is_file() or path.stat().st_size > MAX_POLICY_BYTES:
        raise CommandPolicyError("Command policy must be a bounded regular file")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_POLICY_BYTES + 1)
        if len(payload) > MAX_POLICY_BYTES:
            raise CommandPolicyError("Command policy is too large")
        document = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise CommandPolicyError("Invalid command policy JSON") from exc
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise CommandPolicyError("Command policy schema_version must be 1")
    return document


def defaults_path() -> Path:
    checkout = Path(__file__).resolve().parents[2] / "config/command-policy.defaults.json"
    if checkout.is_file():
        return checkout
    return Path(__file__).resolve().parent / "data/command-policy.defaults.json"


def _argument_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 256 or any(
        not isinstance(item, str) or "\x00" in item or len(item) > 8192 for item in value
    ):
        raise CommandPolicyError("Command arguments must be bounded text arrays")
    return tuple(value)


@dataclass(frozen=True)
class CommandRule:
    builtin: str | None
    argv: tuple[str, ...]
    exact: tuple[tuple[str, ...], ...] | None
    source: str

    @classmethod
    def parse(cls, value: object, source: str, workspace: Path | None) -> CommandRule:
        if not isinstance(value, dict) or set(value) - {"builtin", "argv", "arguments"}:
            raise CommandPolicyError("Unknown command rule fields")
        if ("builtin" in value) == ("argv" in value):
            raise CommandPolicyError("Command rule requires exactly one of builtin or argv")
        builtin = None
        argv: tuple[str, ...] = ()
        if "builtin" in value:
            builtin = command_name(value["builtin"])
            if builtin not in BUILTINS:
                raise CommandPolicyError("Unknown builtin command")
        else:
            argv = _argument_list(value["argv"])
            if not argv:
                raise CommandPolicyError("Custom command argv cannot be empty")
            executable = trusted_path(Path(argv[0]), workspace, "Custom executable")
            if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
                raise CommandPolicyError("Custom executable must be an executable regular file")
            if os.name == "nt" and executable.suffix.casefold() != ".exe":
                raise CommandPolicyError("Custom Windows executable must be .exe; use an explicit interpreter for scripts")
            argv = (str(executable), *argv[1:])
        arguments = value.get("arguments", "any")
        exact = None
        if arguments != "any":
            if not isinstance(arguments, dict) or set(arguments) != {"exact"}:
                raise CommandPolicyError("Arguments must be any or an exact argument list")
            allowed = arguments["exact"]
            if not isinstance(allowed, list) or not 1 <= len(allowed) <= 256:
                raise CommandPolicyError("Exact arguments must contain 1 to 256 arrays")
            exact = tuple(_argument_list(item) for item in allowed)
        return cls(builtin, argv, exact, source)

    def check(self, arguments: list[str]) -> None:
        if self.exact is not None and tuple(arguments) not in self.exact:
            raise execution_denied(
                "ARGUMENTS_NOT_ALLOWED",
                "Command arguments are denied by the selected command policy",
                "参数不匹配完整模板；按运行策略允许的参数调用，或由本机修改规则后重启 MCP。",
            )


def _rules(value: object, source: str, workspace: Path | None) -> dict[str, CommandRule]:
    if not isinstance(value, dict) or len(value) > 256:
        raise CommandPolicyError("Command rules must be a bounded object")
    result: dict[str, CommandRule] = {}
    for raw_name, rule in value.items():
        name = command_name(raw_name)
        if name in result:
            raise CommandPolicyError("Duplicate normalized command alias")
        result[name] = CommandRule.parse(rule, source, workspace)
    return result


@dataclass(frozen=True)
class CommandPolicy:
    preset: str
    rules: Mapping[str, CommandRule]
    disabled: frozenset[str]
    available_presets: tuple[str, ...]
    local_path: str
    search_path: str

    @property
    def unrestricted(self) -> bool:
        return self.preset == "unrestricted"

    def request_key(self, command: str) -> str:
        if self.unrestricted and (not command or len(command) > 8192 or "\x00" in command):
            raise execution_denied(
                "INVALID_COMMAND",
                "Executable name/path must be bounded text",
                "程序名称或绝对路径须为有长度限制且不含 NUL 的文本。",
            )
        if self.unrestricted and Path(command).is_absolute():
            return command
        if self.unrestricted:
            if command in {".", ".."} or any(token in command for token in ("/", "\\", ":")):
                raise execution_denied(
                    "INVALID_COMMAND",
                    "Command must be a program name or an absolute executable path",
                    "使用程序名；unrestricted 也支持完整绝对程序路径，不能传相对路径。",
                )
            # Preserve case and punctuation for POSIX PATH names, while known
            # configured aliases retain their existing normalization.
            normalized = command.casefold().removesuffix(".exe")
            return normalized if normalized in self.rules else command
        try:
            return command_name(command)
        except CommandPolicyError as exc:
            raise execution_denied(
                "INVALID_COMMAND",
                "Command must be an alias or, in unrestricted, an absolute executable path",
                "使用合法命令别名；绝对程序路径只在 unrestricted 下支持。",
            ) from exc

    def command_prefix(self, key: str, resolved: Mapping[str, list[str]]) -> list[str]:
        self.check_entry(key)
        if key in self.rules:
            if key not in resolved:
                raise execution_denied(
                    "COMMAND_UNAVAILABLE",
                    "Configured command is unavailable",
                    "所选规则的程序未安装或启动时未找到；安装/修正程序配置后重启 MCP。",
                )
            target = Path(resolved[key][0])
            if not target.is_file() or (os.name != "nt" and not os.access(target, os.X_OK)):
                raise execution_denied(
                    "COMMAND_UNAVAILABLE",
                    "Configured command is unavailable",
                    "启动快照中的程序已不存在或不可执行；恢复程序后重试，程序路径改变则需重启 MCP。",
                )
            return list(resolved[key])
        executable = key if Path(key).is_absolute() else None
        if executable is None:
            # Search only captured absolute PATH directories. shutil.which on
            # Windows may prepend the current directory even with explicit PATH.
            filename = key if os.name != "nt" or key.casefold().endswith(".exe") else key + ".exe"
            for directory in self.search_path.split(os.pathsep):
                if not directory:
                    continue
                candidate = Path(directory) / filename
                if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                    executable = str(candidate)
                    break
        if not executable:
            raise execution_denied(
                "COMMAND_UNAVAILABLE",
                "Executable is unavailable in the startup PATH",
                "启动时 PATH 未找到程序；安装程序/修正 PATH 后重启，或使用 unrestricted 的绝对路径。",
            )
        target = Path(executable).resolve()
        if not target.exists():
            raise execution_denied(
                "COMMAND_UNAVAILABLE",
                "Executable is unavailable",
                "所选绝对程序路径不存在；确认程序安装位置后重试。",
            )
        if not target.is_file() or (os.name != "nt" and not os.access(target, os.X_OK)):
            raise execution_denied(
                "INVALID_EXECUTABLE",
                "Executable must be an executable regular file",
                "目标须是现存且有执行权限的普通程序文件。",
            )
        if os.name == "nt" and target.suffix.casefold() != ".exe":
            raise execution_denied(
                "INVALID_EXECUTABLE",
                "Windows executable must be .exe; use an explicit interpreter for scripts",
                "Windows 只直接启动 .exe；脚本须通过显式解释器调用。",
            )
        return [str(target)]

    @classmethod
    def load(cls, local_path: Path, workspace: Path | None = None, *, defaults: Path | None = None) -> CommandPolicy:
        base = read_document(defaults or defaults_path(), workspace)
        if set(base) - {"schema_version", "default_preset", "presets"}:
            raise CommandPolicyError("Unknown command defaults fields")
        definitions = base.get("presets")
        if not isinstance(definitions, dict) or not definitions or len(definitions) > 16:
            raise CommandPolicyError("Command presets must be a bounded object")
        presets = {command_name(name): _rules(value, "preset", workspace) for name, value in definitions.items()}
        if len(presets) != len(definitions):
            raise CommandPolicyError("Duplicate normalized preset")
        local = read_document(local_path, workspace, optional=True)
        if set(local) - {"schema_version", "preset", "add", "disable"}:
            raise CommandPolicyError("Unknown local command policy fields")
        default = command_name(base.get("default_preset"))
        preset = command_name(local.get("preset", default))
        if default not in presets or preset not in presets:
            raise CommandPolicyError("Unknown command preset")
        rules = dict(presets[preset])
        rules.update(_rules(local.get("add", {}), "local", workspace))
        disabled = local.get("disable", [])
        if not isinstance(disabled, list) or len(disabled) > 256:
            raise CommandPolicyError("Disabled commands must be a bounded array")
        deny = frozenset(disabled_name(name) for name in disabled)
        return cls(preset, MappingProxyType(rules), deny, tuple(presets), str(local_path), os.pathsep.join(
            entry for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).is_absolute()
        ))

    def resolve(self, discovered: Mapping[str, list[str]], workspace: Path) -> dict[str, list[str]]:
        from .command_discovery import discover_shell_commands
        discovered = dict(discovered)
        if any(rule.builtin in {"pwsh", "powershell", "cmd", "bash", "sh"} for rule in self.rules.values()):
            discovered.update(discover_shell_commands(workspace))
        result: dict[str, list[str]] = {}
        for name, rule in self.rules.items():
            if name in self.disabled:
                continue
            prefix = discovered.get(rule.builtin) if rule.builtin else list(rule.argv)
            if prefix:
                # Builtins were discovered by the server; a venv interpreter
                # may legitimately be a symlink. Custom paths were checked
                # before resolution when the configuration was loaded.
                trusted_path(Path(prefix[0]).resolve(), workspace, "Allowed executable")
                result[name] = list(prefix)
        return result

    def check_entry(self, name: str) -> None:
        alias = Path(name).name.casefold().removesuffix(".exe")
        if alias in self.disabled:
            raise execution_denied(
                "COMMAND_DISABLED",
                "Command is not allowlisted by the selected command policy",
                "本机 disable 已禁用此直接调用；由本机撤销禁用并重启 MCP 后生效。",
            )
        if not self.unrestricted and name not in self.rules:
            raise execution_denied(
                "COMMAND_NOT_ALLOWED",
                "Command is not allowlisted by the selected command policy",
                "当前预设没有此命令；查看 workspace_info.command_policy，按需由本机添加规则/切换预设并重启。",
            )

    def check(self, name: str, arguments: list[str]) -> None:
        self.check_entry(name)
        if name in self.rules:
            self.rules[name].check(arguments)

    @property
    def revision(self) -> str:
        document = {
            "preset": self.preset, "disabled": sorted(self.disabled),
            "rules": {name: {"builtin": rule.builtin, "argv": rule.argv,
                              "exact": rule.exact, "source": rule.source}
                      for name, rule in sorted(self.rules.items())},
        }
        payload = json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def summary(self, resolved: Mapping[str, list[str]], *, enabled: bool | None,
                configuration_role: str = "runtime_snapshot") -> dict:
        return {
            "schema_version": 1, "preset": self.preset, "execution_enabled": enabled,
            "scope": "ordinary_commands_only", "reload": "restart",
            "configuration_role": configuration_role, "policy_revision": self.revision,
            "mode": "unrestricted" if self.unrestricted else "allowlist",
            "command_list_complete": not self.unrestricted,
            "absolute_executable_paths": self.unrestricted,
            "host_identity": "server_account",
            "available_presets": list(self.available_presets),
            "commands": [
                {"name": name, "source": rule.source,
                 "status": "disabled" if name in self.disabled else ("available" if name in resolved else "unavailable"),
                 "arguments": "exact" if rule.exact is not None else "any"}
                for name, rule in sorted(self.rules.items())
            ],
            "disabled": sorted(self.disabled),
        }
