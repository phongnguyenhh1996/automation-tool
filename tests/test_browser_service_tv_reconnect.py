import asyncio
from pathlib import Path
from typing import Optional

from automation_tool import browser_service as browser_service_mod
from automation_tool import coinmap
from automation_tool import coinmap_tradingview_async
from automation_tool import images
from automation_tool.browser_service import BrowserServiceState, _TvWarmTab


class _FakeReconnectButton:
    def __init__(self, calls: list[str], visible_count: int = 1) -> None:
        self._calls = calls
        self._visible_count = visible_count

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._visible_count

    async def click(self, **_kwargs) -> None:
        self._calls.append("connect")


class _FakePage:
    def __init__(self, calls: list[str], visible_count: int = 1) -> None:
        self._button = _FakeReconnectButton(calls, visible_count=visible_count)

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        assert "overlap-manager-root" in selector
        assert "Kết nối" in selector
        return self._button


def test_tv_capture_frame_clicks_reconnect_dialog_before_using_warm_tab(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    service = BrowserServiceState()
    service._tv_warm["default"] = _TvWarmTab(
        _FakePage(calls),
        "default",
        "BTCUSD",
        "15 phút",
    )

    async def noop_ensure() -> None:
        return None

    async def fake_select_symbol(_page, _tv, symbol: str) -> None:
        calls.append(f"select:{symbol}")

    async def fake_reset(_page, _tv) -> None:
        calls.append("reset")

    async def fake_ensure_indicators(_page, _tv) -> None:
        calls.append("ensure_indicators")

    async def fake_capture(_page, _tv, charts_dir, stamp, sym_key, slug):
        calls.append("capture")
        return tmp_path / "chart.png"

    service._ensure_tv_warm_tabs = noop_ensure  # type: ignore[method-assign]
    monkeypatch.setattr(coinmap_tradingview_async, "tv_select_symbol_async", fake_select_symbol)
    monkeypatch.setattr(coinmap_tradingview_async, "tv_reset_chart_position_async", fake_reset)
    monkeypatch.setattr(
        coinmap_tradingview_async,
        "tv_ensure_required_indicators_async",
        fake_ensure_indicators,
    )
    monkeypatch.setattr(coinmap_tradingview_async, "tv_capture_one_chart_frame_async", fake_capture)

    result = asyncio.run(
        service.tv_capture_frame(
            tv={},
            charts_dir=tmp_path,
            stamp="s",
            symbol="ETHUSD",
            interval_label="15 phút",
            slug="15m",
            indicator_profile="",
        )
    )

    assert result == {"path": str(tmp_path / "chart.png")}
    assert calls == ["connect", "select:ETHUSD", "reset", "ensure_indicators", "capture"]


def test_tv_prewarm_background_catches_system_exit() -> None:
    async def run() -> None:
        service = BrowserServiceState()

        async def fail_prewarm() -> None:
            raise SystemExit("boom")

        service._prewarm_tradingview_tabs_async = fail_prewarm  # type: ignore[method-assign]
        service.schedule_tv_prewarm_background()
        await asyncio.sleep(0)

        assert service._prewarm_bg_task is not None
        assert service._prewarm_bg_task.done()
        assert service._prewarm_bg_task.exception() is None

    asyncio.run(run())


def test_tv_prewarm_login_check_runs_on_first_warm_tab_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _FakeContext:
        def __init__(self) -> None:
            self.pages = [_FakeWarmPage("ict"), _FakeWarmPage("default")]
            self.index = 0

        async def new_page(self):
            page = self.pages[self.index]
            self.index += 1
            return page

    class _FakeWarmPage:
        def __init__(self, name: str) -> None:
            self.name = name

    calls: list[tuple[str, str, bool, Optional[str], Optional[str]]] = []
    cfg = {
        "settle_ms": 1234,
        "tradingview_capture": {
            "enabled": True,
            "prewarm_enabled": True,
            "prewarm_skip_dark_mode": True,
        },
    }

    def fake_apply_profile(tv: dict, profile: str) -> dict:
        return {**tv, "_profile": profile}

    async def fake_warmup(
        page,
        tv: dict,
        *,
        symbol: str,
        interval_label: str,
        settle_ms: int,
        login_email: Optional[str],
        login_password: Optional[str],
        skip_login: bool = False,
        skip_dark_mode: bool = False,
    ) -> None:
        calls.append((page.name, tv.get("_profile", "default"), skip_login, login_email, login_password))
        assert symbol == "XAUUSD"
        assert interval_label == "15 phút"
        assert settle_ms == 1234
        assert skip_dark_mode is True

    monkeypatch.setenv("COINMAP_EMAIL", "user@example.com")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "secret")
    monkeypatch.setattr(browser_service_mod, "default_coinmap_config_path", lambda: tmp_path / "coinmap.yaml")
    monkeypatch.setattr(coinmap, "load_coinmap_yaml", lambda _path: cfg)
    monkeypatch.setattr(coinmap, "apply_main_chart_symbol_to_config", lambda cfg_in, _sym: cfg_in)
    monkeypatch.setattr(coinmap, "_tv_apply_indicator_profile", fake_apply_profile)
    monkeypatch.setattr(images, "get_active_main_symbol", lambda: "XAUUSD")
    monkeypatch.setattr(coinmap_tradingview_async, "tv_warmup_tab_async", fake_warmup)

    async def run() -> None:
        service = BrowserServiceState()
        service._context = _FakeContext()  # type: ignore[assignment]

        await service._prewarm_tradingview_tabs_async()

    asyncio.run(run())

    assert calls == [
        ("ict", "ict_killzones", False, "user@example.com", "secret"),
        ("default", "default", True, "user@example.com", "secret"),
    ]
