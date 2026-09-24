from __future__ import annotations

import base64
import json
import socket
import sys
import threading
import urllib.request
from pathlib import Path

import pytest
import httpx2

from tiancheng_mcp.proxy import ProxySettings, add_agent_proxy
from tiancheng_mcp.proxy_probe import probe
from tiancheng_mcp import cli
from tiancheng_mcp.agent_sources import AgentSourcePolicy
from tiancheng_mcp.policy import AccessPolicy
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp.server import create_server
from tiancheng_mcp.tunnel_supervisor import (
    LauncherConfig,
    SupervisorSettings,
    TunnelSupervisor,
)


def _config(tmp_path: Path, defaults: dict, local: dict) -> tuple[Path, Path]:
    default_path = tmp_path / "defaults.json"
    local_path = tmp_path / "local.json"
    default_path.write_text(json.dumps({"proxy": defaults}), encoding="utf-8")
    local_path.write_text(json.dumps({"proxy": local}), encoding="utf-8")
    return default_path, local_path


def test_proxy_fields_merge_with_process_environment_priority(tmp_path: Path) -> None:
    defaults, local = _config(
        tmp_path,
        {"http": "http://default.test:8000", "https": "http://default.test:8001"},
        {"https": "http://local.test:8001", "noProxy": "internal.test", "agent": "inherit"},
    )
    settings = ProxySettings.load(
        defaults,
        local,
        {
            "http_proxy": "http://lower.test:8000",
            "HTTP_PROXY": "http://upper.test:8000",
            "no_proxy": "example.test",
        },
    )
    assert settings.values["http"] == "http://upper.test:8000"
    assert settings.values["https"] == "http://local.test:8001"
    assert settings.values["noProxy"].startswith("example.test,")
    assert settings.agent == "always"
    target: dict[str, str] = {}
    settings.apply_to_process(target)
    assert target["HTTP_PROXY"] == target["http_proxy"] == "http://upper.test:8000"
    assert target["HTTPS_PROXY"] == target["https_proxy"] == "http://local.test:8001"
    assert urllib.request.proxy_bypass_environment(
        "localhost", {"no": target["NO_PROXY"]}
    )
    assert urllib.request.proxy_bypass_environment(
        "127.0.0.1", {"no": target["NO_PROXY"]}
    )


def test_unconfigured_proxy_leaves_environment_untouched(tmp_path: Path) -> None:
    defaults, local = _config(tmp_path, {}, {})
    settings = ProxySettings.load(defaults, local, {})
    target = {"http_proxy": "existing"}
    settings.apply_to_process(target)
    assert target == {"http_proxy": "existing"}
    assert settings.agent_environment() == {}


def test_no_proxy_only_does_not_claim_agent_proxy(tmp_path: Path) -> None:
    defaults, local = _config(tmp_path, {}, {"noProxy": "internal.test", "agent": "always"})
    settings = ProxySettings.load(defaults, local, {})
    assert settings.agent_environment() == {}


@pytest.mark.parametrize("mode", ["off", "selective", "always", "inherit"])
def test_agent_proxy_mode_is_normalized_without_exposing_credentials(
    tmp_path: Path, mode: str
) -> None:
    defaults, local = _config(
        tmp_path, {}, {"http": "http://user:pass@proxy.test:8000", "agent": mode}
    )
    settings = ProxySettings.load(defaults, local, {})
    assert settings.agent == ("always" if mode == "inherit" else mode)
    assert ("HTTP_PROXY" in settings.agent_environment()) is (mode != "off")


def test_probe_reports_egress_ip_without_leaking_proxy_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    defaults, local = _config(
        tmp_path, {}, {"https": "socks5h://user:pass@proxy.test:7891"}
    )
    settings = ProxySettings.load(defaults, local, {})

    class FakeResponse:
        text = "203.0.113.10"

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == settings.values["https"]
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, url):
            assert url == "https://api.ipify.org"
            return FakeResponse()

    monkeypatch.setattr("tiancheng_mcp.proxy_probe.httpx2.Client", FakeClient)
    result = probe(settings)
    assert result == {
        "http": {"status": "unconfigured"},
        "https": {"status": "ok", "egress_ip": "203.0.113.10"},
    }
    assert "user" not in str(result)
    assert "pass" not in str(result)


def test_probe_reports_only_error_class_when_transport_mentions_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    defaults, local = _config(
        tmp_path, {}, {"http": "http://user:secret@proxy.test:8000"}
    )

    class FailingClient:
        def __init__(self, **_kwargs):
            raise RuntimeError("http://user:secret@proxy.test:8000")

    monkeypatch.setattr("tiancheng_mcp.proxy_probe.httpx2.Client", FailingClient)
    result = probe(ProxySettings.load(defaults, local, {}))
    assert result["http"] == {"status": "failed", "reason": "RuntimeError"}
    assert "secret" not in str(result)


def test_process_empty_value_overrides_local_proxy(tmp_path: Path) -> None:
    defaults, local = _config(tmp_path, {}, {"http": "http://local.test:8000"})
    settings = ProxySettings.load(defaults, local, {"HTTP_PROXY": ""})
    target = {"HTTP_PROXY": "stale", "http_proxy": "stale"}
    settings.apply_to_process(target)
    assert "HTTP_PROXY" not in target
    assert "http_proxy" not in target


@pytest.mark.parametrize("url", ["http://proxy.test:bad", "http://[broken", "file://proxy.test"])
def test_invalid_proxy_url_is_rejected_without_echoing_value(tmp_path: Path, url: str) -> None:
    defaults, local = _config(tmp_path, {}, {"http": url})
    with pytest.raises(ValueError, match="proxy.http must be") as error:
        ProxySettings.load(defaults, local, {})
    assert url not in str(error.value)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks5_proxy_url_is_accepted_for_https_targets(tmp_path: Path, scheme: str) -> None:
    defaults, local = _config(
        tmp_path, {}, {"https": f"{scheme}://127.0.0.1:7891"}
    )
    settings = ProxySettings.load(defaults, local, {})
    assert settings.values["https"] == f"{scheme}://127.0.0.1:7891"


@pytest.mark.parametrize(
    ("credentials", "auth_method"),
    [("", 0), ("user:p%40ss@", 2)],
)
def test_http_client_really_uses_socks5_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credentials: str,
    auth_method: int,
) -> None:
    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    received: list[str] = []
    errors: list[Exception] = []

    def read_exact(connection: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise ConnectionError("SOCKS client closed early")
            data += chunk
        return data

    def serve() -> None:
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    greeting = read_exact(connection, 2)
                    assert greeting[0] == 5
                    assert auth_method in read_exact(connection, greeting[1])
                    connection.sendall(bytes((5, auth_method)))
                    if auth_method == 2:
                        auth_header = read_exact(connection, 2)
                        assert auth_header[0] == 1
                        username = read_exact(connection, auth_header[1])
                        password = read_exact(connection, read_exact(connection, 1)[0])
                        assert (username, password) == (b"user", b"p@ss")
                        connection.sendall(b"\x01\x00")
                    request = read_exact(connection, 4)
                    assert request[:3] == b"\x05\x01\x00"
                    assert request[3] == 3  # Target hostname reaches the SOCKS proxy.
                    host = read_exact(connection, read_exact(connection, 1)[0])
                    read_exact(connection, 2)  # Target port.
                    received.append(host.decode("ascii"))
                    connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                    request_bytes = b""
                    while b"\r\n\r\n" not in request_bytes:
                        request_bytes += connection.recv(4096)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                        b"Connection: close\r\n\r\nOK"
                    )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    defaults, local = _config(
        tmp_path, {}, {"http": f"socks5h://{credentials}127.0.0.1:{port}"}
    )
    settings = ProxySettings.load(defaults, local, {})
    environment: dict[str, str] = {}
    settings.apply_to_process(environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with httpx2.Client(timeout=5) as client:
        response = client.get("http://proxy-target.invalid/through-socks")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert received == ["proxy-target.invalid"]
    assert response.text == "OK"


def test_http_client_sends_http_proxy_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    received: list[bytes] = []
    errors: list[Exception] = []

    def serve() -> None:
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += connection.recv(4096)
                    received.append(request)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                        b"Connection: close\r\n\r\nOK"
                    )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    defaults, local = _config(
        tmp_path, {}, {"http": f"http://user:p%40ss@127.0.0.1:{port}"}
    )
    settings = ProxySettings.load(defaults, local, {})
    environment: dict[str, str] = {}
    settings.apply_to_process(environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with httpx2.Client(timeout=5) as client:
        response = client.get("http://proxy-target.invalid/through-http")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert response.text == "OK"
    assert received and received[0].startswith(
        b"GET http://proxy-target.invalid/through-http HTTP/1.1\r\n"
    )
    token = base64.b64encode(b"user:p@ss")
    assert b"Proxy-Authorization: Basic " + token + b"\r\n" in received[0]


def test_http_client_bypasses_socks_for_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    errors: list[Exception] = []

    def serve() -> None:
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += connection.recv(4096)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n"
                        b"Connection: close\r\n\r\nDIRECT"
                    )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    defaults, local = _config(
        tmp_path, {}, {"http": "socks5h://127.0.0.1:1"}
    )
    settings = ProxySettings.load(defaults, local, {})
    environment: dict[str, str] = {}
    settings.apply_to_process(environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with httpx2.Client(timeout=5) as client:
        response = client.get(f"http://127.0.0.1:{port}/readyz")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert response.text == "DIRECT"


def test_supervisor_loopback_probe_never_uses_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, url: str, timeout: int):
            assert url == "http://127.0.0.1:8080/readyz"
            assert timeout == 2
            return Response()

    def build_opener(handler):
        assert isinstance(handler, urllib.request.ProxyHandler)
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    config = LauncherConfig(
        tunnel_client=Path(sys.executable),
        profile_dir=None,
        health_base_url="http://127.0.0.1:8080",
        settings=SupervisorSettings.from_mapping(None),
    )
    supervisor = TunnelSupervisor(
        config=config, profile="test", state_path=tmp_path / "state.json"
    )
    assert supervisor._ready()


@pytest.mark.parametrize("agent_mode,expected", [("off", False), ("always", True)])
def test_agent_child_proxy_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_mode: str, expected: bool
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    defaults, local = _config(
        tmp_path, {}, {"http": "http://proxy.test:8000", "agent": agent_mode}
    )
    settings = ProxySettings.load(defaults, local, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_proxy_environment=settings.agent_environment(),
    )
    try:
        output = workspace / "child-env.json"
        code = (
            "import json,os,sys;"
            "open(sys.argv[1],'w').write(json.dumps({"
            "k:os.environ.get(k) for k in ('HTTP_PROXY','http_proxy','NO_PROXY')}))"
        )
        service._start_managed_process_prepared(
            "python",
            [*service._exec_commands["python"], "-c", code, str(output)],
            workspace,
            max_runtime_seconds=10,
            output_limit_bytes=4096,
            include_passthrough_env=False,
            stdin_enabled=False,
            owner="agent_run",
            agent_proxy=expected,
        )
        record = next(iter(service._processes.values()))
        assert record.process.wait(timeout=10) == 0
        child = json.loads(output.read_text(encoding="utf-8"))
        assert (child["HTTP_PROXY"] == "http://proxy.test:8000") is expected
        assert (child["http_proxy"] == "http://proxy.test:8000") is expected
        assert (child["NO_PROXY"] is not None) is expected
    finally:
        service.shutdown()


def test_agent_proxy_does_not_replace_existing_case_variant() -> None:
    child = {"http_proxy": "http://child.test:9000"}
    add_agent_proxy(
        child,
        {
            "HTTP_PROXY": "http://parent.test:8000",
            "http_proxy": "http://parent.test:8000",
        },
    )
    assert child == {"http_proxy": "http://child.test:9000"}


@pytest.mark.parametrize("mode", ["off", "selective", "always"])
@pytest.mark.asyncio
async def test_agent_session_schema_exposes_proxy_choice_only_in_selective_mode(
    tmp_path: Path, mode: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_proxy_mode=mode,
        agent_proxy_environment={"HTTP_PROXY": "http://proxy.test:8000"},
    )
    try:
        tools = {item.name: item for item in await create_server(service).list_tools()}
        session_tool = tools["agent_session"]
        assert ("use_proxy" in session_tool.input_schema["properties"]) is (
            mode == "selective"
        )
        assert f"Agent proxy mode is {mode}" in session_tool.description
        info = service.workspace_info()
        assert info["agent_proxy_mode"] == mode
        assert info["agent_proxy_configured"] is True
    finally:
        service.shutdown()


def test_cli_applies_proxy_before_creating_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config"
    config.mkdir()
    (config / "launcher.defaults.json").write_text(
        json.dumps({"proxy": {"http": "http://proxy.test:8000", "agent": "inherit"}}),
        encoding="utf-8",
    )
    local = config / "launcher.local.json"
    local.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    observed: dict[str, str] = {}

    class FakeService:
        def __init__(self, **kwargs):
            observed.update(kwargs["agent_proxy_environment"])
            assert cli.os.environ["HTTP_PROXY"] == "http://proxy.test:8000"

        def shutdown(self):
            pass

    class FakeServer:
        def run(self, *, transport: str):
            assert transport == "stdio"

    monkeypatch.setattr(cli, "TianChengService", FakeService)
    monkeypatch.setattr(cli, "create_server", lambda _service: FakeServer())
    cli.main(["--workspace", str(tmp_path / "workspace"), "--launcher-local-config", str(local)])
    assert observed["HTTP_PROXY"] == "http://proxy.test:8000"
