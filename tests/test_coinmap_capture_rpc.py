from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation_tool.coinmap import _run_coinmap_via_browser_service


def test_run_coinmap_via_browser_service_raises_when_service_down(monkeypatch) -> None:
    cd = {
        "enabled": True,
        "multi_shot_enabled": True,
        "capture_plan": [{"symbol": "XAUUSD", "interval": "5m", "watchlist_category": "forex 1"}],
    }
    api_cd = {"enabled": True, "mode": "network_capture"}

    monkeypatch.setattr("automation_tool.browser_client.is_service_responding", lambda: False)

    with pytest.raises(SystemExit, match="browser up"):
        _run_coinmap_via_browser_service(
            cd=cd,
            api_cd=api_cd,
            cfg={},
            charts_dir=Path("/tmp/charts"),
            stamp="20260101_120000",
            settle_ms=500,
        )


def test_run_coinmap_via_browser_service_returns_paths(monkeypatch, tmp_path: Path) -> None:
    cd = {
        "enabled": True,
        "multi_shot_enabled": True,
        "capture_plan": [{"symbol": "XAUUSD", "interval": "5m", "watchlist_category": "forex 1"}],
    }
    api_cd = {"enabled": True, "mode": "network_capture"}
    out_json = tmp_path / "20260101_120000_coinmap_XAUUSD_5m.json"
    out_json.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("automation_tool.browser_client.is_service_responding", lambda: True)

    mock_client = MagicMock()
    mock_client.request.return_value = {
        "ok": True,
        "result": {"paths": [str(out_json)]},
    }
    monkeypatch.setattr(
        "automation_tool.browser_client.BrowserClient.from_state_file",
        lambda: mock_client,
    )

    paths = _run_coinmap_via_browser_service(
        cd=cd,
        api_cd=api_cd,
        cfg={"login_url": "https://coinmap.tech/login"},
        charts_dir=tmp_path,
        stamp="20260101_120000",
        settle_ms=500,
    )
    assert paths == [out_json]
    mock_client.request.assert_called_once()
    assert mock_client.request.call_args[0][0] == "coinmap_capture_plan"
