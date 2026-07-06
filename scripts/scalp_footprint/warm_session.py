"""Persistent GoCharting tabs with continuous WS listeners for scalp footprint watch."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

_log = logging.getLogger("scalp_footprint.warm_session")

DEFAULT_HEALTH_INTERVAL_SEC = 300
_DEFAULT_WARM_WAIT_MS = 15_000


@dataclass
class _WarmTab:
    interval: str
    chart_url: str
    page: Page
    dest: Path
    footprint_docs: list[dict[str, Any]] = field(default_factory=list)
    ohlc_docs: list[dict[str, Any]] = field(default_factory=list)
    last_ok_monotonic: float = 0.0


class WarmFootprintSession:
    """Keep M5/M15 GoCharting chart tabs open; poll in-memory WS buffers on candle close."""

    def __init__(
        self,
        *,
        charts_dir: Path,
        gocharting_yaml: Path,
        headless: bool = True,
        intervals: tuple[str, ...] = ("5m", "15m"),
        warm_wait_ms: int = _DEFAULT_WARM_WAIT_MS,
        health_interval_sec: int = DEFAULT_HEALTH_INTERVAL_SEC,
    ) -> None:
        self.charts_dir = charts_dir
        self.gocharting_yaml = gocharting_yaml
        self.headless = headless
        self.intervals = tuple(iv.strip().lower() for iv in intervals)
        self.warm_wait_ms = max(1000, int(warm_wait_ms))
        self.health_interval_sec = max(30, int(health_interval_sec))

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._tabs: dict[str, _WarmTab] = {}
        self._cfg: dict[str, Any] = {}
        self._email = ""
        self._password = ""
        self._lock_acquired = False
        self._started = False
        self._last_health_monotonic = 0.0

    def start(self) -> None:
        if self._started:
            return

        from automation_tool.config import default_storage_state_path, load_all_dotenv
        from automation_tool.gocharting_capture import load_gocharting_yaml
        from automation_tool.gocharting_capture_lock import acquire_gocharting_capture_lock
        from automation_tool.gocharting_gc_spot_convert import native_gc_footprint_cfg
        from automation_tool.gocharting_ws_decode import footprint_ws_interval_specs
        from automation_tool.playwright_browser import launch_chrome_context
        from playwright.sync_api import sync_playwright

        load_all_dotenv()
        self._cfg = native_gc_footprint_cfg(load_gocharting_yaml(self.gocharting_yaml))
        ws = self._cfg.setdefault("footprint_ws", {})
        if not isinstance(ws, dict):
            ws = {}
            self._cfg["footprint_ws"] = ws
        ws["extra_session_days"] = 0
        # Warm mode relies on continuous WS buffers; skip IDB polls (store often absent on chart tabs).
        idb = ws.get("idb")
        if not isinstance(idb, dict):
            idb = {}
        idb["enabled"] = False
        ws["idb"] = idb

        self._email = os.getenv("GOCHARTING_EMAIL", "")
        self._password = os.getenv("GOCHARTING_PASSWORD", "")

        specs = {
            iv: url for iv, url in footprint_ws_interval_specs(self._cfg) if iv and url
        }
        missing = [iv for iv in self.intervals if iv not in specs]
        if missing:
            raise ValueError(f"footprint_screenshot.intervals missing page_url for: {missing}")

        acquire_gocharting_capture_lock()
        self._lock_acquired = True

        storage = default_storage_state_path()
        vw = int(self._cfg.get("viewport_width", 1920))
        vh = int(self._cfg.get("viewport_height", 1080))

        self._playwright = sync_playwright().start()
        self._browser, self._context = launch_chrome_context(
            self._playwright,
            headless=self.headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )

        for iv in self.intervals:
            page = self._context.new_page()
            dest = self._resolve_dest(iv)
            tab = _WarmTab(interval=iv, chart_url=specs[iv], page=page, dest=dest)
            self._tabs[iv] = tab
            self._setup_tab(tab)

        self._started = True
        self._last_health_monotonic = time.monotonic()
        _log.info(
            "Warm footprint session started | intervals=%s headless=%s",
            ", ".join(self.intervals),
            self.headless,
        )

    def close(self) -> None:
        from automation_tool.gocharting_capture_lock import release_gocharting_capture_lock
        from automation_tool.playwright_browser import close_browser_and_context

        for tab in list(self._tabs.values()):
            try:
                if not tab.page.is_closed():
                    tab.page.close()
            except Exception:
                pass
        self._tabs.clear()

        if self._browser is not None and self._context is not None:
            try:
                close_browser_and_context(self._browser, self._context)
            except Exception:
                pass
        self._browser = None
        self._context = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

        if self._lock_acquired:
            release_gocharting_capture_lock()
            self._lock_acquired = False
        self._started = False
        _log.info("Warm footprint session closed")

    def capture_intervals(self, intervals: tuple[str, ...]) -> list[Path]:
        """Wait for fresh WS data on due intervals and write combined JSON files."""
        if not self._started:
            raise RuntimeError("WarmFootprintSession.start() required before capture_intervals()")

        self.ensure_healthy()

        paths: list[Path] = []
        for iv in intervals:
            key = iv.strip().lower()
            tab = self._tabs.get(key)
            if tab is None:
                _log.warning("Warm session has no tab for interval %s", iv)
                continue
            try:
                dest = self._capture_tab(tab)
                paths.append(dest)
            except Exception:
                _log.exception("Warm capture failed for %s — reconnecting tab", key)
                self._reconnect_tab(tab)
                dest = self._capture_tab(tab)
                paths.append(dest)
        return paths

    def ensure_healthy(self) -> None:
        """Periodic health check; reload tabs that are closed or stale."""
        now = time.monotonic()
        if now - self._last_health_monotonic < self.health_interval_sec:
            for tab in self._tabs.values():
                if tab.page.is_closed():
                    _log.warning("Warm tab %s closed — reconnecting", tab.interval)
                    self._reconnect_tab(tab)
            return

        self._last_health_monotonic = now
        for tab in self._tabs.values():
            if tab.page.is_closed():
                _log.warning("Warm tab %s closed — reconnecting", tab.interval)
                self._reconnect_tab(tab)
                continue
            if tab.last_ok_monotonic and now - tab.last_ok_monotonic > self.health_interval_sec * 2:
                _log.info("Warm tab %s idle >%ds — refresh subscribe", tab.interval, self.health_interval_sec * 2)
                self._resubscribe(tab)

    def _resolve_dest(self, interval: str) -> Path:
        from automation_tool.gocharting_footprint_ocr import footprint_images_dir
        from automation_tool.gocharting_ws_capture import _resolve_output_path
        from automation_tool.gocharting_ws_decode import footprint_ws_export_format

        iv = interval.strip().lower()
        fp_dir = footprint_images_dir(self.charts_dir, gocharting_yaml=self.gocharting_yaml)
        fmt = footprint_ws_export_format(self._cfg)
        return _resolve_output_path(fp_dir=fp_dir, interval=iv, export_format=fmt, out_path=None)

    def _setup_tab(self, tab: _WarmTab, *, reload: bool = False) -> None:
        from automation_tool.gocharting_capture import _maybe_login_gocharting, _select_chart_symbol
        from automation_tool.gocharting_footprint_ws_request import (
            _resolve_footprint_security,
            request_footprint_dates_on_page,
        )
        from automation_tool.gocharting_ws_capture import (
            _attach_ws_listeners,
            _symbol_entry_for_footprint_ws,
            footprint_capture_session_dates,
        )
        from automation_tool.gocharting_ws_decode import footprint_ws_export_format

        iv = tab.interval
        fmt = footprint_ws_export_format(self._cfg)

        if reload:
            tab.footprint_docs.clear()
            tab.ohlc_docs.clear()
        else:
            tab.footprint_docs = []
            tab.ohlc_docs = []

        _attach_ws_listeners(
            tab.page,
            interval=iv,
            export_format=fmt,
            footprint_docs=tab.footprint_docs,
            ohlc_docs=tab.ohlc_docs,
        )

        _log.info("footprint_warm: opening %s (%s)", tab.chart_url, iv)
        tab.page.goto(tab.chart_url, wait_until="domcontentloaded", timeout=120_000)
        _maybe_login_gocharting(tab.page, self._cfg, self._email, self._password)
        tab.page.wait_for_timeout(2000)

        symbol_entry = _symbol_entry_for_footprint_ws(self._cfg, None)
        if symbol_entry.get("search_query"):
            _select_chart_symbol(tab.page, self._cfg, symbol_entry)
            tab.footprint_docs.clear()
            tab.ohlc_docs.clear()
            tab.page.wait_for_timeout(5000)
            _log.info(
                "footprint_warm: switched chart symbol to %r (%s)",
                symbol_entry.get("search_query"),
                iv,
            )

        self._resubscribe(tab)
        tab.last_ok_monotonic = time.monotonic()

    def _resubscribe(self, tab: _WarmTab) -> None:
        from automation_tool.gocharting_footprint_ws_request import (
            _resolve_footprint_security,
            request_footprint_dates_on_page,
        )
        from automation_tool.gocharting_ws_capture import footprint_capture_session_dates

        capture_now = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
        ws_security = _resolve_footprint_security(self._cfg)
        subscribe_dates = footprint_capture_session_dates(tab.footprint_docs, now=capture_now)
        sub_result = request_footprint_dates_on_page(
            tab.page, subscribe_dates, interval=tab.interval, security=ws_security
        )
        if not sub_result.get("ok"):
            _log.warning("footprint_warm: subscribeFootprint failed (%s): %s", tab.interval, sub_result)
        else:
            _log.info(
                "footprint_warm: subscribed dates=%s (%s)",
                subscribe_dates,
                tab.interval,
            )
        tab.page.wait_for_timeout(500)

    def _reconnect_tab(self, tab: _WarmTab) -> None:
        if tab.page.is_closed():
            if self._context is None:
                raise RuntimeError("Warm session context is not available")
            tab.page = self._context.new_page()
        else:
            try:
                tab.page.reload(wait_until="domcontentloaded", timeout=120_000)
            except Exception:
                if not tab.page.is_closed():
                    tab.page.close()
                tab.page = self._context.new_page()
        self._setup_tab(tab, reload=True)

    def _capture_tab(self, tab: _WarmTab) -> Path:
        from automation_tool.gocharting_ws_capture import (
            FootprintCaptureStaleError,
            _assert_output_fresh,
            _build_output_document,
            _idb_lookup_dates_for_capture,
            _wait_for_footprint_data,
        )
        from automation_tool.gocharting_ws_decode import (
            document_timeframe,
            footprint_ws_export_format,
            footprint_ws_max_candles,
            footprint_ws_min_ready_candles,
            trim_footprint_document,
            write_footprint_document,
        )

        iv = tab.interval
        fmt = footprint_ws_export_format(self._cfg)
        mc = footprint_ws_max_candles(self._cfg)
        min_ready = min(footprint_ws_min_ready_candles(self._cfg), mc)
        idb_lookup_dates = _idb_lookup_dates_for_capture(self._cfg, 0)
        capture_now = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)

        wait_source, candle_count, elapsed_ms = _wait_for_footprint_data(
            tab.page,
            cfg=self._cfg,
            interval=iv,
            export_format=fmt,
            footprint_docs=tab.footprint_docs,
            max_wait_ms=self.warm_wait_ms,
            min_candles=min_ready,
            lookup_dates=idb_lookup_dates,
            now=capture_now,
        )
        if wait_source == "timeout":
            raise FootprintCaptureStaleError(
                f"Warm WS wait timeout for {iv}: candles={candle_count} elapsed={elapsed_ms}ms"
            )

        output_doc = _build_output_document(
            footprint_docs=tab.footprint_docs,
            ohlc_docs=tab.ohlc_docs,
            export_format=fmt,
            interval=iv,
            cfg=self._cfg,
        )
        output_doc = trim_footprint_document(output_doc, max_candles=mc)

        from automation_tool.gocharting_ws_decode import latest_closed_candle_open_for_interval

        expected_closed = latest_closed_candle_open_for_interval(capture_now, iv)
        _assert_output_fresh(
            output_doc,
            interval=iv,
            expected_closed_open=expected_closed,
            wait_source=wait_source,
            now=capture_now,
        )

        write_footprint_document(tab.dest, output_doc)
        tab.last_ok_monotonic = time.monotonic()

        _log.info(
            "footprint_warm: wrote %s (%d candles, %s %s, source=%s elapsed=%dms)",
            tab.dest.name,
            len(output_doc.get("candles") or []),
            output_doc.get("symbol"),
            document_timeframe(output_doc) or iv,
            wait_source,
            elapsed_ms,
        )
        return tab.dest

    def __enter__(self) -> WarmFootprintSession:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
