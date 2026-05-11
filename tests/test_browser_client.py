import subprocess
from pathlib import Path

from automation_tool import browser_client


def test_browser_service_log_path_defaults_to_cwd_logs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BROWSER_SERVICE_LOG_FILE", raising=False)

    assert browser_client.browser_service_log_path(cwd=tmp_path) == tmp_path / "logs" / "browser_service.log"


def test_browser_service_log_path_honors_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-service.log"
    monkeypatch.setenv("BROWSER_SERVICE_LOG_FILE", str(custom))

    assert browser_client.browser_service_log_path(cwd=tmp_path) == custom.resolve()


def test_spawn_browser_service_detached_redirects_output_to_log(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BROWSER_SERVICE_LOG_FILE", raising=False)
    monkeypatch.setattr(browser_client.sys, "executable", "/python")
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            kwargs["stdout"].write(b"startup error\n")

    monkeypatch.setattr(browser_client.subprocess, "Popen", FakePopen)
    proc = browser_client.spawn_browser_service_detached(cwd=tmp_path)

    assert isinstance(proc, FakePopen)
    assert captured["cmd"] == ["/python", "-m", "automation_tool.browser_service"]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert (tmp_path / "logs" / "browser_service.log").read_bytes() == b"startup error\n"
