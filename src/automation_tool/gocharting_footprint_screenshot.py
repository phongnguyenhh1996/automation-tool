from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

from automation_tool.browser_client import browser_service_state_path, try_attach_playwright_via_service
from automation_tool.config import default_storage_state_path
from automation_tool.gocharting_capture import (
    _force_click_id,
    _maybe_login_gocharting,
    _wait_for_chart_before_export,
    load_gocharting_yaml,
)
from automation_tool.gocharting_capture_lock import wait_until_gocharting_capture_idle
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger(__name__)

_CAPTURE_BUSY_WAIT_S = 60.0

_DEFAULT_INTERVALS = ("5m", "15m")
_DEFAULT_FOOTPRINT_SCREENSHOT: dict[str, Any] = {
    "output_subdir": "footprint_images",
    "symbol": "COMEX:GC1!",
    "ocr_split_ratio": 0.5,
    "delete_screenshot_after_ocr": False,
    "refresh_before_capture": True,
    "clip": {"x1": 50, "y1": 50, "x2": 300, "y2": 1100},
    "intervals": {
        "15m": {
            "page_url": "https://gocharting.com/terminal/chart/S0kcqfQKt",
            "viewport_width": 500,
            "viewport_height": 1200,
            "zoom_clicks": 10,
        },
        "5m": {
            "page_url": "https://gocharting.com/terminal/chart/GC435uijM",
            "viewport_width": 500,
            "viewport_height": 1200,
            "zoom_clicks": 10,
        },
    },
}
_DEDUPE_MAX_ENTRIES = 64


@dataclass(frozen=True)
class FootprintIntervalTab:
    interval: str
    interval_minutes: int
    page: Page
    cfg: dict[str, Any]


def _interval_minutes(interval: str) -> int:
    iv = (interval or "").strip().lower()
    if iv.endswith("m"):
        return max(1, int(iv[:-1]))
    raise ValueError(f"unsupported interval {interval!r}")


def _is_first_minute_of_candle(now: datetime, interval_min: int) -> bool:
    return now.minute % interval_min == 1


def _candle_open_local(now: datetime, interval_min: int) -> datetime:
    floored_minute = (now.minute // interval_min) * interval_min
    return now.replace(minute=floored_minute, second=0, microsecond=0)


def _closed_candle_open(now: datetime, interval_min: int) -> datetime:
    return _candle_open_local(now, interval_min) - timedelta(minutes=interval_min)


def _format_candle_time_label(dt: datetime) -> str:
    return f"{dt.hour}h{dt.minute}m"


def _footprint_image_path(out_dir: Path, closed_candle_open: datetime, interval: str) -> Path:
    date_part = closed_candle_open.strftime("%Y%m%d")
    time_part = _format_candle_time_label(closed_candle_open)
    iv = (interval or "").strip().lower()
    return out_dir / f"{date_part}_{time_part}_{iv}.png"


def _clip_box_from_config(clip_cfg: dict[str, Any]) -> dict[str, int]:
    x1 = int(clip_cfg.get("x1", 50))
    y1 = int(clip_cfg.get("y1", 50))
    x2 = int(clip_cfg.get("x2", 300))
    y2 = int(clip_cfg.get("y2", 1100))
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    return {"x": x1, "y": y1, "width": width, "height": height}


def _wait_seconds_until_next_minute(now: datetime) -> float:
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return max(0.0, (next_minute - now).total_seconds())


def _wait_until_next_minute() -> None:
    wait_s = _wait_seconds_until_next_minute(datetime.now())
    if wait_s > 0:
        time.sleep(wait_s)


def _footprint_screenshot_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("footprint_screenshot")
    if not isinstance(raw, dict):
        return dict(_DEFAULT_FOOTPRINT_SCREENSHOT)
    merged = dict(_DEFAULT_FOOTPRINT_SCREENSHOT)
    merged.update(raw)
    intervals = dict(_DEFAULT_FOOTPRINT_SCREENSHOT["intervals"])
    raw_intervals = raw.get("intervals")
    if isinstance(raw_intervals, dict):
        for key, value in raw_intervals.items():
            if isinstance(value, dict):
                base = dict(intervals.get(key, {}))
                base.update(value)
                intervals[key] = base
    merged["intervals"] = intervals
    clip = dict(_DEFAULT_FOOTPRINT_SCREENSHOT["clip"])
    raw_clip = raw.get("clip")
    if isinstance(raw_clip, dict):
        clip.update(raw_clip)
    merged["clip"] = clip
    return merged


def _interval_cfg(footprint_cfg: dict[str, Any], interval: str) -> dict[str, Any]:
    intervals = footprint_cfg.get("intervals") or {}
    if not isinstance(intervals, dict):
        raise ValueError("footprint_screenshot.intervals must be a mapping")
    entry = intervals.get(interval)
    if not isinstance(entry, dict):
        raise ValueError(f"footprint_screenshot.intervals missing {interval!r}")
    return dict(entry)


def _zoom_detail_chart(page: Page, base_cfg: dict[str, Any], interval_cfg: dict[str, Any]) -> int:
    detail = base_cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        detail = {}
    zoom_in_id = str(interval_cfg.get("zoom_in_button_id") or detail.get("zoom_in_button_id") or "zoomIn-button")
    zoom_clicks = int(interval_cfg.get("zoom_clicks", 10))
    delay_ms = int(interval_cfg.get("zoom_click_delay_ms") or detail.get("zoom_click_delay_ms") or 500)
    for _ in range(max(0, zoom_clicks)):
        _force_click_id(page, zoom_in_id, delay_ms=delay_ms)
    return zoom_clicks


def _refresh_detail_chart(page: Page, base_cfg: dict[str, Any], interval_cfg: dict[str, Any]) -> None:
    detail = base_cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        detail = {}
    refresh_id = str(interval_cfg.get("refresh_button_id") or detail.get("refresh_button_id") or "refresh-button")
    _force_click_id(page, refresh_id)


def _refresh_and_zoom_footprint_chart(
    page: Page,
    base_cfg: dict[str, Any],
    interval_cfg: dict[str, Any],
) -> int:
    """Refresh resets chart zoom — always re-apply zoomIn after refresh."""
    _refresh_detail_chart(page, base_cfg, interval_cfg)
    zoom_clicks = _zoom_detail_chart(page, base_cfg, interval_cfg)
    _wait_for_chart_before_export(page, base_cfg, section="detail_chart")
    return zoom_clicks


def _prepare_footprint_page(
    page: Page,
    base_cfg: dict[str, Any],
    interval_cfg: dict[str, Any],
    *,
    email: str,
    password: str,
) -> None:
    page_url = str(interval_cfg.get("page_url") or "").strip()
    if not page_url:
        raise ValueError("footprint interval page_url is required")

    width = int(interval_cfg.get("viewport_width", 500))
    height = int(interval_cfg.get("viewport_height", 1200))
    page.set_viewport_size({"width": width, "height": height})

    page.goto(page_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    _maybe_login_gocharting(page, base_cfg, email, password)

    zoom_clicks = _refresh_and_zoom_footprint_chart(page, base_cfg, interval_cfg)
    _log.info(
        "gocharting footprint: prepared page (refresh + zoomIn x%s, viewport %sx%s)",
        zoom_clicks,
        width,
        height,
    )


def _clip_screenshot(page: Page, clip: dict[str, int], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(dest), clip=clip)


def _capture_footprint_shot(
    tab: FootprintIntervalTab,
    base_cfg: dict[str, Any],
    footprint_cfg: dict[str, Any],
    *,
    dest: Path,
) -> None:
    if bool(footprint_cfg.get("refresh_before_capture", True)):
        zoom_clicks = _refresh_and_zoom_footprint_chart(tab.page, base_cfg, tab.cfg)
        _log.debug("gocharting footprint: refresh + zoomIn x%s before capture", zoom_clicks)
    clip = _clip_box_from_config(footprint_cfg.get("clip") or {})
    _clip_screenshot(tab.page, clip, dest)
    _log.info("gocharting footprint: saved %s", dest.name)


def _open_interval_tab(
    context: BrowserContext,
    base_cfg: dict[str, Any],
    footprint_cfg: dict[str, Any],
    *,
    interval: str,
    email: str,
    password: str,
) -> FootprintIntervalTab:
    interval_cfg = _interval_cfg(footprint_cfg, interval)
    page = context.new_page()
    _prepare_footprint_page(page, base_cfg, interval_cfg, email=email, password=password)
    return FootprintIntervalTab(
        interval=interval,
        interval_minutes=_interval_minutes(interval),
        page=page,
        cfg=interval_cfg,
    )


def _open_interval_tabs(
    context: BrowserContext,
    base_cfg: dict[str, Any],
    footprint_cfg: dict[str, Any],
    *,
    intervals: tuple[str, ...],
    email: str,
    password: str,
) -> list[FootprintIntervalTab]:
    return [
        _open_interval_tab(
            context,
            base_cfg,
            footprint_cfg,
            interval=interval,
            email=email,
            password=password,
        )
        for interval in intervals
    ]


def _close_interval_tabs(tabs: list[FootprintIntervalTab]) -> None:
    for tab in tabs:
        try:
            tab.page.close()
        except Exception:
            pass


def _trim_dedupe(captured: set[tuple[str, datetime]]) -> None:
    if len(captured) <= _DEDUPE_MAX_ENTRIES:
        return
    for key in sorted(captured, key=lambda item: item[1])[: len(captured) - _DEDUPE_MAX_ENTRIES]:
        captured.discard(key)


def run_footprint_gocharting_screenshot_daemon(
    *,
    gocharting_yaml: Path,
    charts_dir: Path,
    email: str,
    password: str,
    storage_state_path: Optional[Path] = None,
    save_storage_state: bool = True,
    headless: bool = True,
    require_browser_service: bool = True,
    intervals: tuple[str, ...] = _DEFAULT_INTERVALS,
    ocr_api_key: Optional[str] = None,
) -> None:
    storage = storage_state_path or default_storage_state_path()
    if not email or not password:
        if not storage.is_file():
            raise SystemExit(
                "GOCHARTING_EMAIL and GOCHARTING_PASSWORD are required "
                f"(or existing storage state at {storage})."
            )

    base_cfg = load_gocharting_yaml(gocharting_yaml)
    footprint_cfg = _footprint_screenshot_cfg(base_cfg)
    out_subdir = str(footprint_cfg.get("output_subdir") or "footprint_images").strip()
    out_dir = charts_dir / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol = str(footprint_cfg.get("symbol") or "COMEX:GC1!").strip()
    ocr_split_ratio = float(footprint_cfg.get("ocr_split_ratio", 0.5))
    delete_after_ocr = bool(footprint_cfg.get("delete_screenshot_after_ocr", False))
    clip_cfg = footprint_cfg.get("clip") or {}
    clip_width = max(1, int(clip_cfg.get("x2", 300)) - int(clip_cfg.get("x1", 50)))
    ocr_key = (ocr_api_key or "").strip()
    if not ocr_key:
        raise SystemExit(
            "OCR_SPACE_API_KEY is required for footprint-gocharting-screenshot "
            "(OCR clip → JSON on disk)."
        )

    captured: set[tuple[str, datetime]] = set()
    _log.info(
        "gocharting footprint daemon: start | out_dir=%s intervals=%s use_service=%s",
        out_dir,
        ",".join(intervals),
        require_browser_service,
    )

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p, force=require_browser_service)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
            _log.info("gocharting footprint: attached to browser service")
        elif require_browser_service:
            raise SystemExit(
                "footprint-gocharting-screenshot requires browser service but could not attach via CDP. "
                "Run: coinmap-automation browser up "
                f"(state file: {browser_service_state_path()})."
            )
        else:
            browser, context = launch_chrome_context(
                p,
                headless=headless,
                storage_state_path=storage if storage.is_file() else None,
                viewport_width=500,
                viewport_height=1200,
            )
            use_browser_service = False
            _log.info("gocharting footprint: launched standalone Chrome")

        try:
            print(f"footprint-gocharting-screenshot daemon running; output → {out_dir}", flush=True)
            while True:
                _wait_until_next_minute()
                now = datetime.now()
                for interval in intervals:
                    interval_min = _interval_minutes(interval)
                    if not _is_first_minute_of_candle(now, interval_min):
                        continue
                    closed_open = _closed_candle_open(now, interval_min)
                    dedupe_key = (interval, closed_open)
                    if dedupe_key in captured:
                        continue
                    dest = _footprint_image_path(out_dir, closed_open, interval)
                    tab: FootprintIntervalTab | None = None
                    try:
                        wait_until_gocharting_capture_idle(sleep_s=_CAPTURE_BUSY_WAIT_S)
                        tab = _open_interval_tab(
                            context,
                            base_cfg,
                            footprint_cfg,
                            interval=interval,
                            email=email,
                            password=password,
                        )
                        if save_storage_state and not use_browser_service:
                            try:
                                context.storage_state(path=str(storage))
                            except Exception:
                                _log.warning(
                                    "gocharting footprint: could not save storage state",
                                    exc_info=True,
                                )
                        _capture_footprint_shot(
                            tab,
                            base_cfg,
                            footprint_cfg,
                            dest=dest,
                        )
                        captured.add(dedupe_key)
                        _trim_dedupe(captured)
                        print(f"Captured {dest.name} at {now.strftime('%H:%M:%S')}", flush=True)
                        from automation_tool.gocharting_footprint_ocr import (
                            footprint_interval_json_path,
                            process_footprint_clip_image,
                        )

                        json_path = footprint_interval_json_path(out_dir, interval)
                        ocr_result = process_footprint_clip_image(
                            dest,
                            ocr_api_key=ocr_key,
                            closed_candle_open=closed_open,
                            image_width=clip_width,
                            out_json_path=json_path,
                            symbol=symbol,
                            timeframe=interval,
                            split_ratio=ocr_split_ratio,
                            delete_image_after=delete_after_ocr,
                        )
                        if ocr_result is None:
                            print(
                                f"OCR skip {dest.name} (no bid/ask pair lines)",
                                flush=True,
                            )
                            continue
                        candle, _doc = ocr_result
                        print(
                            f"OCR → {json_path.name} | time={candle['time']} "
                            f"levels={len(candle['price_levels'])}",
                            flush=True,
                        )
                    except Exception:
                        _log.exception(
                            "gocharting footprint: capture failed interval=%s closed_open=%s",
                            interval,
                            closed_open,
                        )
                    finally:
                        if tab is not None:
                            _close_interval_tabs([tab])
        except KeyboardInterrupt:
            _log.info("gocharting footprint daemon: stopped by user")
            print("\nfootprint-gocharting-screenshot daemon stopped.", flush=True)
        finally:
            if use_browser_service:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)
