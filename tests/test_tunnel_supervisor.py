from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tiancheng_mcp.tunnel_supervisor import (
    FailureWindow,
    LauncherConfig,
    ProfileLock,
    RestartBudget,
    SupervisorSettings,
    TunnelSupervisor,
    _classify_failure_line,
    load_launcher_config,
    parse_duration,
)


def test_parse_duration_is_positive_and_bounded() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("24h") == 86400
    for invalid in ("0s", "10", "1d", "169h", "-1h", "forever"):
        with pytest.raises(ValueError):
            parse_duration(invalid)


def test_supervisor_settings_reject_unsafe_or_unbounded_values() -> None:
    settings = SupervisorSettings.from_mapping(None)
    assert settings.enabled is True
    assert settings.connection_max_ttl == "24h"
    assert settings.restart_budget_max_attempts == 5

    with pytest.raises(ValueError, match="boolean"):
        SupervisorSettings.from_mapping({"enabled": "yes"})
    with pytest.raises(ValueError, match="non-empty"):
        SupervisorSettings.from_mapping({"restartBackoffSeconds": []})
    with pytest.raises(ValueError, match="between"):
        SupervisorSettings.from_mapping(
            {"restartBudget": {"maxAttempts": 0, "windowSeconds": 600}}
        )


def test_failure_window_requires_repeated_errors_within_window() -> None:
    window = FailureWindow(threshold=3, window_seconds=30)
    assert window.record(1.0) is False
    assert window.record(20.0) is False
    assert window.record(31.5) is False  # the first event has expired
    assert window.record(32.0) is True
    window.clear()
    assert window.record(100.0) is False


def test_restart_budget_opens_circuit_until_old_attempts_expire() -> None:
    budget = RestartBudget(max_attempts=2, window_seconds=10)
    assert budget.allow(1.0) is True
    assert budget.allow(2.0) is True
    assert budget.allow(3.0) is False
    assert budget.count == 2
    assert budget.allow(12.1) is True
    assert budget.count == 1


def test_profile_lock_can_be_reacquired_without_unlock_error(tmp_path: Path) -> None:
    path = tmp_path / "profile.lock"
    for _ in range(2):
        with ProfileLock(path):
            pass
    assert not path.exists()


@pytest.mark.parametrize(
    ("line", "classification"),
    [
        ("dispatcher received MCP upstream error", ("repeated_mcp_upstream_failure", False)),
        ("command response deadline reached", ("repeated_mcp_upstream_failure", False)),
        (
            '{"status":502,"failure_source":"client_internal",'
            '"upstream_response_received":false}',
            ("client_internal_without_upstream", True),
        ),
    ],
)
def test_failure_log_signatures_are_detected(
    line: str, classification: tuple[str, bool]
) -> None:
    assert _classify_failure_line(line) == classification


def test_exact_internal_502_is_an_immediate_recovery_signal() -> None:
    assert _classify_failure_line(
        '{"status":502,"failure_source":"client_internal",'
        '"upstream_response_received":false}'
    ) == ("client_internal_without_upstream", True)
    assert _classify_failure_line("command response deadline reached") == (
        "repeated_mcp_upstream_failure",
        False,
    )


@pytest.mark.parametrize(
    "line",
    [
        "poll failed; backing off",
        "poller recovered; polling operational",
        '{"failure_source":"remote","upstream_response_received":true}',
    ],
)
def test_control_plane_poll_noise_does_not_trigger_mcp_recovery(line: str) -> None:
    assert _classify_failure_line(line) is None


def test_launcher_config_merges_local_override_without_secrets(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    local = tmp_path / "local.json"
    defaults.write_text(
        json.dumps(
            {
                "tunnelClient": sys.executable,
                "profileDir": "",
                "healthBaseUrl": "http://127.0.0.1:8080",
                "supervisor": {"mcpConnectionMaxTtl": "24h"},
            }
        ),
        encoding="utf-8",
    )
    local.write_text(
        json.dumps(
            {
                "supervisor": {
                    "mcpConnectionMaxTtl": "2h",
                    "upstreamErrorThreshold": 4,
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_launcher_config(defaults, local)

    assert loaded.tunnel_client == Path(sys.executable).resolve()
    assert loaded.settings.connection_max_ttl == "2h"
    assert loaded.settings.upstream_error_threshold == 4


def test_launcher_config_rejects_non_loopback_health_url(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        json.dumps(
            {
                "tunnelClient": sys.executable,
                "profileDir": "",
                "healthBaseUrl": "https://example.test",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="127.0.0.1"):
        load_launcher_config(defaults, tmp_path / "missing.json")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080@evil.test",
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "http://127.0.0.1",
    ],
)
def test_launcher_config_rejects_lookalike_loopback_urls(
    tmp_path: Path, url: str
) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        json.dumps(
            {
                "tunnelClient": sys.executable,
                "profileDir": "",
                "healthBaseUrl": url,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="127.0.0.1"):
        load_launcher_config(defaults, tmp_path / "missing.json")


def test_supervisor_command_applies_ttl_and_profile_directory(tmp_path: Path) -> None:
    config = LauncherConfig(
        tunnel_client=Path(sys.executable),
        profile_dir=tmp_path / "profiles",
        health_base_url="http://127.0.0.1:8080",
        settings=SupervisorSettings.from_mapping(
            {"mcpConnectionMaxTtl": "2h"}
        ),
    )
    supervisor = TunnelSupervisor(
        config=config,
        profile="safe-profile",
        state_path=tmp_path / "state.json",
    )

    assert supervisor._command() == [
        sys.executable,
        "run",
        "--profile",
        "safe-profile",
        "--mcp.connection-max-ttl",
        "2h",
        "--profile-dir",
        str(tmp_path / "profiles"),
    ]


def test_recovery_writes_diagnostic_log_and_state_fields(tmp_path: Path) -> None:
    config = LauncherConfig(
        tunnel_client=Path(sys.executable),
        profile_dir=None,
        health_base_url="http://127.0.0.1:8080",
        settings=SupervisorSettings.from_mapping(
            {
                "restartBackoffSeconds": [0],
                "restartBudget": {"maxAttempts": 2, "windowSeconds": 10},
            }
        ),
    )
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "recovery.jsonl"
    supervisor = TunnelSupervisor(
        config=config,
        profile="diagnostic-profile",
        state_path=state_path,
        recovery_log_path=log_path,
    )
    supervisor.generation = 4
    supervisor.last_recovery_reason = "unit_test_failure"
    assert supervisor.restart_budget.allow(supervisor.monotonic()) is True

    assert supervisor._backoff("unit_test_failure") is False

    event = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert event["generation"] == 5
    assert event["failed_generation"] == 4
    assert event["recovery_reason"] == "unit_test_failure"
    assert event["restart_count"] == 1
    assert event["backoff_until"] is not None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["generation"] == 4
    assert state["recovery_reason"] == "unit_test_failure"
    assert state["restart_count"] == 1
    assert state["backoff_until"] == event["backoff_until"]


def test_supervisor_check_reports_safe_effective_config(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    local = tmp_path / "local.json"
    defaults.write_text(
        json.dumps(
            {
                "tunnelClient": sys.executable,
                "profileDir": "",
                "healthBaseUrl": "http://127.0.0.1:8080",
                "supervisor": {"mcpConnectionMaxTtl": "24h"},
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tiancheng_mcp.tunnel_supervisor",
            "--defaults",
            str(defaults),
            "--local-config",
            str(local),
            "--profile",
            "checked-profile",
            "--check",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "valid": True,
        "profile": "checked-profile",
        "supervisor_enabled": True,
        "mcp_connection_max_ttl": "24h",
    }


@pytest.mark.parametrize("encoding", ["ascii", "gbk", "utf-8"])
def test_tee_preserves_failure_detection_with_limited_output_encoding(tmp_path, encoding):
    supervisor = TunnelSupervisor(
        config=LauncherConfig(
            tunnel_client=Path(sys.executable), profile_dir=None,
            health_base_url="http://127.0.0.1:8080",
            settings=SupervisorSettings.from_mapping(None),
        ), profile="tee-test", state_path=tmp_path / "state.json",
    )
    supervisor.generation = 1
    line = 'µ failure_source=client_internal upstream_response_received=false\n'
    stream = io.StringIO(line + "following line\n")
    raw_output = io.BytesIO()
    output = io.TextIOWrapper(raw_output, encoding=encoding, errors="strict")
    supervisor._tee(stream, output, 1)
    assert stream.closed
    assert supervisor.recover_event.is_set()
    assert supervisor.failure_reason == "client_internal_without_upstream"
    displayed = raw_output.getvalue().decode(encoding)
    assert "following line" in displayed
    assert ("µ" if encoding == "utf-8" else r"\xb5") in displayed


@pytest.mark.parametrize("failure", [BrokenPipeError(), ValueError("closed")])
@pytest.mark.parametrize("phase", ["write", "flush"])
def test_tee_keeps_draining_after_output_breaks(tmp_path, failure, phase):
    class BrokenOutput:
        def write(self, line):
            if phase == "write":
                raise failure
        def flush(self):
            if phase == "flush":
                raise failure
    supervisor = TunnelSupervisor(
        config=LauncherConfig(
            tunnel_client=Path(sys.executable), profile_dir=None,
            health_base_url="http://127.0.0.1:8080",
            settings=SupervisorSettings.from_mapping(None),
        ), profile="tee-test", state_path=tmp_path / "state.json",
    )
    supervisor.generation = 2
    stream = io.StringIO("ordinary line\nfailure_source=client_internal upstream_response_received=false\n")
    supervisor._tee(stream, BrokenOutput(), 2)
    assert stream.closed
    assert supervisor.recover_event.is_set()
    assert supervisor.failure_reason == "client_internal_without_upstream"
