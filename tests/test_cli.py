from __future__ import annotations

from pathlib import Path

from tiancheng_mcp import cli


def test_stdio_keyboard_interrupt_is_a_clean_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeService:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs
            self.stopped = False

        def stop_all_processes(self) -> None:
            self.stopped = True

    class FakeServer:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            raise KeyboardInterrupt

    service_holder: list[FakeService] = []

    def make_service(*args, **kwargs):
        service = FakeService(*args, **kwargs)
        service_holder.append(service)
        return service

    monkeypatch.setattr(cli, "TianChengService", make_service)
    monkeypatch.setattr(cli, "create_server", lambda service: FakeServer())
    cli.main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--audit-dir",
            str(tmp_path / "audit"),
            "--pass-env",
            "EXAMPLE_SERVICE_KEY",
        ]
    )
    assert service_holder and service_holder[0].stopped is True
    assert service_holder[0].kwargs["passthrough_env"] == ["EXAMPLE_SERVICE_KEY"]
