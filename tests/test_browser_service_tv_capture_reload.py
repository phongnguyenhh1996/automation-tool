import asyncio
from pathlib import Path

from automation_tool import coinmap_tradingview_async
from automation_tool.browser_service import BrowserServiceState, _TvWarmTab


class _FakeLocator:
    @property
    def first(self) -> "_FakeLocator":
        return self

    async def wait_for(self, state: str, timeout: int) -> None:
        return None


class _FakeWarmPage:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def is_closed(self) -> bool:
        return False

    async def reload(self, wait_until: str, timeout: int) -> None:
        self.events.append(f"reload:{self.name}")

    async def wait_for_timeout(self, ms: int) -> None:
        self.events.append(f"settle:{self.name}:{ms}")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator()


def _warm_state(events: list[str]) -> BrowserServiceState:
    st = BrowserServiceState()
    st._tv_warm = {
        "ict": _TvWarmTab(_FakeWarmPage("ict", events), "ict_killzones", "XAUUSD", "15 phút"),
        "default": _TvWarmTab(_FakeWarmPage("default", events), "default", "XAUUSD", "15 phút"),
    }
    st._tv_settle_ms = 123
    return st


def _patch_tv_helpers(monkeypatch, events: list[str]) -> None:
    async def select_symbol(page, tv, symbol):
        events.append(f"select_symbol:{page.name}:{symbol}")

    async def select_interval(page, tv, interval_label, settle_ms):
        events.append(f"select_interval:{page.name}:{interval_label}:{settle_ms}")

    async def reset_chart(page, tv):
        events.append(f"reset:{page.name}")

    async def ensure_indicators(page, tv):
        events.append(f"ensure:{page.name}")

    async def ensure_watchlist(page, tv):
        events.append(f"watchlist:{page.name}")

    async def capture(page, tv, charts_dir, stamp, symbol_key, interval_slug, **kwargs):
        events.append(f"capture:{page.name}:{symbol_key}:{interval_slug}")
        return Path(f"/tmp/{page.name}.png")

    monkeypatch.setattr(coinmap_tradingview_async, "tv_select_symbol_async", select_symbol)
    monkeypatch.setattr(coinmap_tradingview_async, "tv_select_interval_async", select_interval)
    monkeypatch.setattr(coinmap_tradingview_async, "tv_reset_chart_position_async", reset_chart)
    monkeypatch.setattr(coinmap_tradingview_async, "tv_ensure_required_indicators_async", ensure_indicators)
    monkeypatch.setattr(coinmap_tradingview_async, "tradingview_ensure_watchlist_open_async", ensure_watchlist)
    monkeypatch.setattr(coinmap_tradingview_async, "tv_capture_one_chart_frame_async", capture)


def test_tv_capture_frame_reloads_both_warm_tabs_before_capture(monkeypatch, tmp_path):
    events: list[str] = []
    _patch_tv_helpers(monkeypatch, events)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        st = _warm_state(events)

        result = loop.run_until_complete(
            st.tv_capture_frame(
                tv={"after_reload_chart_ready_ms": 456},
                charts_dir=tmp_path,
                stamp="stamp",
                symbol="BTCUSD",
                interval_label="5 phút",
                slug="5m",
                indicator_profile="",
            )
        )
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert result == {"path": "/tmp/default.png"}
    assert events.index("reload:ict") < events.index("capture:default:BTCUSD:5m")
    assert events.index("reload:default") < events.index("capture:default:BTCUSD:5m")


def test_tv_capture_plan_reloads_both_warm_tabs_before_first_capture(monkeypatch, tmp_path):
    events: list[str] = []
    _patch_tv_helpers(monkeypatch, events)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        st = _warm_state(events)

        result = loop.run_until_complete(
            st.tv_capture_plan(
                tv={"after_reload_chart_ready_ms": 456},
                plan=[{"symbol": "BTCUSD", "intervals": ["5 phút"]}],
                charts_dir=tmp_path,
                stamp="stamp",
                main_symbol="BTCUSD",
            )
        )
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert result == {"paths": ["/tmp/default.png"]}
    assert events.index("reload:ict") < events.index("capture:default:BTCUSD:5m")
    assert events.index("reload:default") < events.index("capture:default:BTCUSD:5m")
