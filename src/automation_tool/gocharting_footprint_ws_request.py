"""Request GoCharting footprint session dates via the trades Web Worker."""
from __future__ import annotations

import logging
from typing import Any

from playwright.sync_api import Page

_log = logging.getLogger(__name__)

_SUBSCRIBE_JS = """
async ([dates, feedKey]) => {
  if (!self.appManager) {
    return { ok: false, error: "appManager unavailable" };
  }
  const feeds = self.appManager.timeseriesFeeds || {};
  const keys = Object.keys(feeds);
  let feed = feedKey ? feeds[feedKey] : null;
  if (!feed) {
    feed = keys.length === 1 ? feeds[keys[0]] : null;
  }
  if (!feed || !feed.tsFeed || typeof feed.tsFeed.subscribeFootprint !== "function") {
    return { ok: false, error: "tsFeed.subscribeFootprint unavailable", feedKeys: keys };
  }
  try {
    await feed.tsFeed.subscribeFootprint(dates);
    return {
      ok: true,
      dates,
      feedKey: feedKey || keys[0] || null,
      interval: feed.interval,
      broker: feed.broker,
    };
  } catch (e) {
    return { ok: false, error: String(e), feedKeys: keys };
  }
}
"""

_DOWNLOAD_JS = """
async ([dates, interval, security, broker, sessionAlias]) => {
  if (!self.appManager || typeof self.appManager._downloadFootprint !== "function") {
    return { ok: false, error: "appManager._downloadFootprint unavailable" };
  }
  const msg = {
    security: security,
    sessionAlias: sessionAlias,
    interval: interval,
    broker: broker,
    dates: dates,
  };
  try {
    self.appManager._downloadFootprint(msg);
    const feeds = self.appManager.timeseriesFeeds || {};
    const keys = Object.keys(feeds);
    const suffix = `-${interval}-${sessionAlias}`;
    const matched = keys.find((k) => k.endsWith(suffix));
    return { ok: true, dates, interval, broker, feedKeys: keys, matchedFeedKey: matched || null };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""


def _pick_worker(page: Page, wait_for_worker_ms: int) -> Any:
    workers = list(page.workers)
    if not workers and wait_for_worker_ms > 0:
        page.wait_for_timeout(wait_for_worker_ms)
        workers = list(page.workers)
    return workers[0] if workers else None


def list_timeseries_feed_keys(page: Page, *, wait_for_worker_ms: int = 3000) -> list[str]:
    worker = _pick_worker(page, wait_for_worker_ms)
    if worker is None:
        return []
    result = worker.evaluate(
        "() => Object.keys((self.appManager && self.appManager.timeseriesFeeds) || {})"
    )
    return list(result) if isinstance(result, list) else []


def _resolve_footprint_security(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build ``security`` dict for ``_downloadFootprint`` fallback from config."""
    fp = cfg.get("footprint_screenshot") or {}
    sym = str(fp.get("symbol") or "").strip()
    if sym.upper().startswith("COMEX:"):
        symbol = sym.split(":", 1)[1].strip()
    elif sym:
        symbol = sym
    else:
        symbol = ""
    if not symbol:
        symbols = cfg.get("symbols") or {}
        block = symbols.get("XAUUSD") if isinstance(symbols, dict) else None
        if isinstance(block, dict):
            symbol = str(block.get("search_query") or "").strip()
    return {
        "exchange": "COMEX",
        "segment": "FUTURE",
        "symbol": symbol or "GC1!",
        "data_source_location": "nyc1",
    }


def request_footprint_dates_on_page(
    page: Page,
    dates: list[str],
    *,
    interval: str = "5m",
    security: dict[str, Any] | None = None,
    broker: str = "GoCharting",
    session_alias: str = "ETH",
    feed_key: str | None = None,
    wait_for_worker_ms: int = 5000,
) -> dict[str, Any]:
    """Trigger ``subscribeFootprint(dates)`` on the chart worker tsFeed."""
    if not dates:
        return {"ok": False, "error": "empty dates"}

    normalized = [str(d).strip() for d in dates if str(d).strip()]
    worker = _pick_worker(page, wait_for_worker_ms)
    if worker is None:
        return {"ok": False, "error": "no page.workers attached"}

    keys = list_timeseries_feed_keys(page, wait_for_worker_ms=0)
    chosen_key = feed_key
    if not chosen_key and keys:
        suffix = f"-{interval.strip().lower()}-{session_alias}"
        chosen_key = next((k for k in keys if k.endswith(suffix)), keys[0])

    if chosen_key:
        try:
            result = worker.evaluate(_SUBSCRIBE_JS, [normalized, chosen_key])
            if isinstance(result, dict) and result.get("ok"):
                _log.info(
                    "footprint_ws: subscribeFootprint dates=%s feed=%s",
                    normalized,
                    chosen_key,
                )
                return result
            _log.warning("footprint_ws: direct subscribe failed: %s", result)
        except Exception as exc:
            _log.warning("footprint_ws: direct subscribe error: %s", exc)

    sec = dict(security or {})
    sec.setdefault("exchange", "COMEX")
    sec.setdefault("segment", "FUTURE")
    if not sec.get("symbol"):
        sec["symbol"] = "GC1!"
    sec.setdefault("data_source_location", "nyc1")

    try:
        result = worker.evaluate(
            _DOWNLOAD_JS,
            [normalized, interval.strip().lower(), sec, broker, session_alias],
        )
        if isinstance(result, dict) and result.get("ok"):
            _log.info("footprint_ws: _downloadFootprint dates=%s broker=%s", normalized, broker)
        return result if isinstance(result, dict) else {"ok": False, "error": "invalid worker response"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
