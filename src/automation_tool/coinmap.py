from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
import yaml
from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from automation_tool.coinmap_openai_slim import slim_coinmap_export_for_openai
from automation_tool.config import default_coinmap_bearer_cache_path, default_logs_dir
from automation_tool.browser_client import browser_service_state_path, try_attach_playwright_via_service
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context


def load_coinmap_yaml(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


_LEGACY_MAIN_PAIR = "XAUUSD"


def apply_main_chart_symbol_to_config(cfg: dict[str, Any], main_symbol: str) -> dict[str, Any]:
    """
    Replace ``XAUUSD`` with ``main_symbol`` in Coinmap/TradingView capture plans and TV ``chart_url``.
    Does not change DXY / USDINDEX rows.
    """
    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol(main_symbol)
    out = copy.deepcopy(cfg)
    old = _LEGACY_MAIN_PAIR

    cd = out.get("chart_download")
    if isinstance(cd, dict):
        plan = cd.get("capture_plan")
        if isinstance(plan, list):
            for row in plan:
                if isinstance(row, dict) and str(row.get("symbol") or "").strip().upper() == old:
                    row["symbol"] = sym

    tv = out.get("tradingview_capture")
    if isinstance(tv, dict):
        url = tv.get("chart_url")
        if isinstance(url, str) and old in url:
            tv["chart_url"] = url.replace(old, sym)

        # Watchlist monitor: keep in sync with main symbol as well.
        ws = tv.get("watchlist_symbol_short")
        if isinstance(ws, str):
            wss = ws.strip().upper()
            if wss == old:
                tv["watchlist_symbol_short"] = sym

        plan = tv.get("capture_plan")
        if isinstance(plan, list):
            for row in plan:
                if isinstance(row, dict) and str(row.get("symbol") or "").strip().upper() == old:
                    row["symbol"] = sym
    return out


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _maybe_save_capture_error_screenshot(
    page,
    *,
    logs_dir: Path,
    stamp: str,
    label: str,
) -> Optional[Path]:
    """
    Save a full-page PNG under logs/ when chart capture fails (modal, timeout, etc.).
    Best-effort: never raises; returns None if screenshot could not be written.
    """
    try:
        _ensure_dir(logs_dir)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", label).strip("_") or "error"
        path = logs_dir / f"{stamp}_capture_fail_{safe}.png"
        page.screenshot(path=str(path), full_page=True, timeout=15_000)
        return path
    except Exception:
        return None


def _clear_charts_dir(charts_dir: Path) -> None:
    """Delete existing image files in charts_dir (keeps .gitkeep and non-image files)."""
    if not charts_dir.is_dir():
        return
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".json", ".url"}
    for p in charts_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            p.unlink()
        except OSError:
            pass


_DEFAULT_COINMAP_GW_ENDPOINTS: dict[str, str] = {
    "getcandlehistory": "https://gw.coinmap.tech/cm-api/api/v1/getcandlehistory",
    "getorderflowhistory": "https://gw.coinmap.tech/cm-api/api/v1/getorderflowhistory",
    "getindicatorsvwap": "https://gw.coinmap.tech/cm-api/api/v1/getindicatorsvwap",
    "getcandlehistorycvd": "https://gw.coinmap.tech/cm-api/api/v1/getcandlehistorycvd",
}

_COINMAP_API_KEYS: tuple[str, ...] = (
    "getcandlehistory",
    "getorderflowhistory",
    "getindicatorsvwap",
    "getcandlehistorycvd",
)

# Chart UI rarely calls CVD; network_capture must not block waiting for it.
_COINMAP_NETWORK_CAPTURE_WAIT_KEYS: tuple[str, ...] = (
    "getcandlehistory",
    "getorderflowhistory",
    "getindicatorsvwap",
)


def _api_export_mode(api_cd: dict[str, Any]) -> str:
    raw = (api_cd.get("mode") or "bearer_request").strip().lower()
    if raw in ("request", "http", "fetch"):
        return "request"
    if raw in ("bearer_request", "request_bearer", "bearer"):
        return "bearer_request"
    return "network_capture"


def _coinmap_endpoint_key_from_response_url(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path or ""
    except Exception:
        return None
    path = path.rstrip("/")
    # Longer path suffixes first so ``getcandlehistorycvd`` wins over ``getcandlehistory``.
    for key in sorted(_COINMAP_API_KEYS, key=len, reverse=True):
        if path.endswith(key) or path.endswith("/" + key):
            return key
    return None


def _merge_coinmap_bar_arrays(bodies: list[Any]) -> list[Any]:
    """
    Merge multiple JSON array responses for the same endpoint (e.g. repeated getcandlehistory
    when the user pans the chart). Deduplicate by bar timestamp ``t``; later responses win
    for the same ``t``. Output order: newest first (descending ``t``), matching Coinmap arrays.
    """
    by_t: dict[Any, dict[str, Any]] = {}
    for body in bodies:
        if not isinstance(body, list):
            continue
        for item in body:
            if not isinstance(item, dict):
                continue
            t = item.get("t")
            if t is None:
                continue
            by_t[t] = item
    if not by_t:
        return []
    return [by_t[t] for t in sorted(by_t.keys(), reverse=True)]


def _filter_coinmap_api_array_by_step(
    body: Any,
    *,
    symbol: Optional[str],
    interval: Optional[str],
    relax_symbol_if_empty: bool = False,
) -> Any:
    """
    Keep only array rows whose ``i`` matches the chart step interval (and ``s`` matches symbol
    when both are set). Stops merged captures from mixing e.g. 5m and 15m in one export file.

    When ``relax_symbol_if_empty`` is true and the strict filter yields no rows, retry with
    interval-only matching (same ``i``). Helps when candle rows use a different ``s`` string than
    orderflow (e.g. index naming) while still avoiding mixed intervals.
    """
    if body is None or not interval:
        return body
    if not isinstance(body, list):
        return body
    iv = str(interval).strip()
    sym = (symbol or "").strip() or None
    out: list[Any] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        if item.get("i") != iv:
            continue
        if sym:
            it_s = item.get("s")
            if it_s is not None and it_s != sym:
                continue
        out.append(item)
    if not out and relax_symbol_if_empty and sym and iv:
        for item in body:
            if not isinstance(item, dict):
                continue
            if item.get("i") != iv:
                continue
            out.append(item)
    return out


def _relax_symbol_filter_from_api_cd(api_cd: Optional[dict[str, Any]]) -> bool:
    if api_cd is None:
        return False
    return bool(api_cd.get("relax_symbol_filter_if_empty", True))


def _coinmap_apply_export_symbol_to_payload(
    payload: dict[str, Any],
    *,
    internal_symbol: Optional[str],
    export_symbol: Optional[str],
) -> dict[str, Any]:
    """
    Set top-level ``symbol`` (and per-bar ``s``) to the TradingView-style label when
    ``export_symbol`` is set; keep ``coinmap_symbol`` when it differs from the watchlist id.
    """
    internal = (internal_symbol or "").strip() or None
    export = (export_symbol or "").strip() or None
    if not export or export == internal:
        return payload
    out = dict(payload)
    out["symbol"] = export
    if internal:
        out["coinmap_symbol"] = internal
    for key in _COINMAP_API_KEYS:
        block = out.get(key)
        if not isinstance(block, list):
            continue
        for row in block:
            if isinstance(row, dict) and row.get("s") == internal:
                row["s"] = export
    return out


class CoinmapNetworkCapture:
    """
    Records JSON bodies from chart-originated gateway responses (Authorization headers
    from the page), unlike context.request replay which often gets 401.
    """

    def __init__(self, page, api_cd: dict[str, Any]) -> None:
        self.page = page
        self.api_cd = api_cd
        self._records: list[dict[str, Any]] = []
        self._handler: Optional[Callable[..., None]] = None

    def install(self) -> None:
        def handler(response) -> None:
            self._on_response(response)

        self._handler = handler
        self.page.on("response", self._handler)

    def uninstall(self) -> None:
        if self._handler is not None:
            try:
                self.page.remove_listener("response", self._handler)
            except Exception:
                pass
            self._handler = None

    def _on_response(self, response) -> None:
        url = response.url
        if "gw.coinmap.tech" not in url and not self.api_cd.get("capture_any_host", False):
            return
        key = _coinmap_endpoint_key_from_response_url(url)
        if not key:
            return
        max_ch = max(256, int(self.api_cd.get("max_nonjson_body_chars") or 8000))
        try:
            status = response.status
            ok = 200 <= status < 300
            try:
                body: Any = response.json()
            except Exception:
                text = response.text()
                body = text if len(text) <= max_ch else text[:max_ch] + "...(truncated)"
            self._records.append(
                {"key": key, "url": url, "status": status, "ok": ok, "body": body}
            )
        except Exception as e:
            self._records.append(
                {"key": key, "url": url, "status": 0, "ok": False, "body": str(e)}
            )

    def consume_shot(
        self, start_index: int, step_ctx: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        wait_ms = max(0, int(self.api_cd.get("network_capture_wait_ms") or 12_000))
        poll_ms = max(50, int(self.api_cd.get("network_capture_poll_ms") or 300))
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            if self._shot_has_all_keys(start_index):
                break
            self.page.wait_for_timeout(poll_ms)
        slice_ = self._records[start_index:]
        return self._last_body_per_key(slice_, step_ctx=step_ctx)

    def _shot_has_all_keys(self, start_index: int) -> bool:
        slice_ = self._records[start_index:]
        seen = {r["key"] for r in slice_}
        return all(k in seen for k in _COINMAP_NETWORK_CAPTURE_WAIT_KEYS)

    def _last_body_per_key(
        self,
        slice_: list[dict[str, Any]],
        step_ctx: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        sym = (step_ctx or {}).get("symbol")
        iv = (step_ctx or {}).get("interval")
        merge = bool(self.api_cd.get("merge_repeated_endpoint_responses", True))
        max_per = int(self.api_cd.get("network_capture_max_responses_per_endpoint", 2))
        relax = _relax_symbol_filter_from_api_cd(self.api_cd)
        out: dict[str, Any] = {}
        for key in _COINMAP_API_KEYS:
            matching = [r for r in slice_ if r.get("key") == key]
            if max_per > 0 and len(matching) > max_per:
                matching = matching[:max_per]
            if not matching:
                out[key] = {
                    "ok": False,
                    "status": 0,
                    "body": "no matching response captured (timeout or chart did not call this endpoint)",
                }
                continue

            if merge:
                ok_lists: list[Any] = []
                last_list_record: Optional[dict[str, Any]] = None
                for r in matching:
                    if r.get("ok") and isinstance(r.get("body"), list):
                        filtered = _filter_coinmap_api_array_by_step(
                            r["body"],
                            symbol=sym,
                            interval=iv,
                            relax_symbol_if_empty=relax,
                        )
                        ok_lists.append(filtered)
                        last_list_record = r
                if ok_lists:
                    merged = _merge_coinmap_bar_arrays(ok_lists)
                    last = last_list_record or matching[-1]
                    entry: dict[str, Any] = {
                        "ok": True,
                        "status": int(last.get("status") or 200),
                        "body": merged,
                    }
                    if last.get("url"):
                        entry["url"] = last["url"]
                    out[key] = entry
                    continue

            last = matching[-1]
            raw_body = last.get("body")
            if isinstance(raw_body, list):
                raw_body = _filter_coinmap_api_array_by_step(
                    raw_body,
                    symbol=sym,
                    interval=iv,
                    relax_symbol_if_empty=relax,
                )
            entry = {
                "ok": bool(last.get("ok")),
                "status": int(last.get("status") or 0),
                "body": raw_body,
            }
            if last.get("url"):
                entry["url"] = last["url"]
            out[key] = entry
        return out


class CoinmapBearerCapture:
    """
    Records ``Authorization: Bearer …`` from outgoing requests to the Coinmap gateway
    so ``context.request`` calls can reuse the same token (avoids 401 without response capture).
    """

    def __init__(self, page, api_cd: dict[str, Any]) -> None:
        self.page = page
        self.api_cd = api_cd
        self._authorization: Optional[str] = None
        self._handler: Optional[Callable[..., None]] = None
        self._logged_first_token: bool = False

    def install(self) -> None:
        def handler(request) -> None:
            self._on_request(request)

        self._handler = handler
        self.page.on("request", self._handler)
        subs = self.api_cd.get("bearer_capture_url_substrings")
        sub_note = (
            f", url_substrings={subs!r}"
            if isinstance(subs, list) and len(subs) > 0
            else ", url_substrings=(any gw.coinmap.tech)"
        )
        _coinmap_bearer_log(
            self.api_cd,
            f"Installed gateway request listener{sub_note}",
        )

    def uninstall(self) -> None:
        if self._handler is not None:
            try:
                self.page.remove_listener("request", self._handler)
            except Exception:
                pass
            self._handler = None
            _coinmap_bearer_log(self.api_cd, "Removed gateway request listener")

    def _on_request(self, request) -> None:
        url = request.url
        if "gw.coinmap.tech" not in url and not self.api_cd.get("capture_any_host", False):
            return
        subs = self.api_cd.get("bearer_capture_url_substrings")
        if isinstance(subs, list) and len(subs) > 0:
            if not any(isinstance(s, str) and s and s in url for s in subs):
                return
        try:
            hdrs = request.headers
        except Exception:
            return
        auth: Optional[str] = None
        if isinstance(hdrs, dict):
            for k, v in hdrs.items():
                if str(k).lower() == "authorization" and isinstance(v, str) and v.strip():
                    auth = v.strip()
                    break
        if auth and auth.lower().startswith("bearer "):
            self._authorization = auth
            if not self._logged_first_token:
                self._logged_first_token = True
                n = len(auth)
                _coinmap_bearer_log(
                    self.api_cd,
                    f"Saw Authorization on outgoing request ({n} chars; URL host matches filter)",
                )
            if bool(self.api_cd.get("bearer_log_verbose")):
                _coinmap_bearer_log(self.api_cd, f"request: {url[:120]}{'…' if len(url) > 120 else ''}")

    def get_authorization(self) -> Optional[str]:
        return self._authorization

    def wait_for_authorization(self, timeout_ms: int) -> Optional[str]:
        poll_ms = max(50, int(self.api_cd.get("bearer_ready_poll_ms") or 100))
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        _coinmap_bearer_log(
            self.api_cd,
            f"Waiting for Authorization (timeout {timeout_ms}ms, poll {poll_ms}ms)",
        )
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            if self._authorization:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                _coinmap_bearer_log(
                    self.api_cd,
                    f"Authorization ready after {elapsed_ms}ms",
                )
                return self._authorization
            self.page.wait_for_timeout(poll_ms)
        _coinmap_bearer_log(
            self.api_cd,
            f"No Authorization within {timeout_ms}ms (poll {poll_ms}ms)",
        )
        return self._authorization


def _api_export_config(cd: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = cd.get("api_data_export")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    return raw


def _coinmap_bearer_log_enabled(api_cd: Optional[dict[str, Any]]) -> bool:
    """When ``api_data_export.bearer_log`` is false, skip ``[coinmap bearer]`` messages."""
    if api_cd is None:
        return True
    return api_cd.get("bearer_log", True) is not False


def _coinmap_bearer_log(api_cd: Optional[dict[str, Any]], message: str) -> None:
    """
    Log bearer flow lines on ``automation_tool`` (INFO) → stderr + ``TELEGRAM_LOG_CHAT_ID``
    when :func:`telegram_logging.setup_automation_logging` has run (CLI and capture workers).
    """
    if not _coinmap_bearer_log_enabled(api_cd):
        return
    logging.getLogger("automation_tool").info("[coinmap bearer] %s", message)


class CoinmapBearerCacheInvalid(Exception):
    """cm-api returned 401/403; cached bearer token should be refreshed via the browser."""


def _coinmap_bearer_token_cache_enabled(api_cd: dict[str, Any]) -> bool:
    return api_cd.get("bearer_token_cache", True) is not False


def _coinmap_bearer_token_cache_resolved_path(api_cd: dict[str, Any]) -> Path:
    raw = api_cd.get("bearer_token_cache_path")
    if isinstance(raw, str) and raw.strip():
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            from automation_tool.config import _root

            p = _root() / p
        return p
    return default_coinmap_bearer_cache_path()


def _coinmap_read_bearer_token_cache(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text


def _coinmap_write_bearer_token_cache(path: Path, authorization: str) -> None:
    auth = authorization.strip()
    if not auth:
        return
    _ensure_dir(path.parent)
    path.write_text(auth + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _coinmap_bearer_shot_has_auth_failure(shot: dict[str, Any]) -> bool:
    for key in _COINMAP_API_KEYS:
        block = shot.get(key)
        if isinstance(block, dict):
            st = block.get("status")
            if st in (401, 403):
                return True
    return False


_COINMAP_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "45m": 45,
    "1h": 60,
    "2h": 120,
    "3h": 180,
    "4h": 240,
    "1d": 1440,
}


def _coinmap_interval_minutes(interval: str) -> int:
    """Map capture plan interval (e.g. 5m, 15m, 1h) to Coinmap API ``resolution`` minutes."""
    s = (interval or "").strip().lower()
    if s in _COINMAP_INTERVAL_MINUTES:
        return _COINMAP_INTERVAL_MINUTES[s]
    m = re.match(r"^(\d+)m$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)h$", s)
    if m:
        return int(m.group(1)) * 60
    m = re.match(r"^(\d+)d$", s)
    if m:
        return int(m.group(1)) * 1440
    return 5


def _coinmap_interval_to_resolution_num(interval: str) -> str:
    return str(_coinmap_interval_minutes(interval))


def _vn_session_anchor_utc_ms(
    now_utc: datetime,
    *,
    tz_name: str,
    start_hour: int,
) -> int:
    """
    Epoch milliseconds of the most recent *session open* at ``start_hour`` in ``tz_name``
    that is not after ``now_utc`` (same instant). Used for Coinmap ``from`` in local
    trading-day style windows (default: 05:00 Vietnam time → converted to UTC).
    """
    try:
        tz = ZoneInfo((tz_name or "").strip() or "Asia/Ho_Chi_Minh")
    except Exception:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
    vn = now_utc.astimezone(tz)
    h = max(0, min(23, int(start_hour)))
    anchor = vn.replace(hour=h, minute=0, second=0, microsecond=0)
    if vn < anchor:
        anchor = anchor - timedelta(days=1)
    return int(anchor.astimezone(timezone.utc).timestamp() * 1000)


def _coinmap_auto_from_to_ms(api_cd: dict[str, Any], step: dict[str, Any]) -> tuple[int, int]:
    """
    Default (``auto_from_to_mode`` unset or ``vn_session``): ``to`` = UTC now (ms);
    ``from`` = same clock instant as 05:00 (configurable) in Vietnam timezone
    (``Asia/Ho_Chi_Minh``), i.e. the start of the current local session through now.

    Legacy: ``auto_from_to_mode: countback`` keeps ``from = to - countback * bar_ms``.
    """
    # Compute "now" in VN local time first, then convert to UTC epoch ms.
    # (Epoch time is invariant, but this keeps the logic aligned with the vn_session windowing config.)
    tz_name = str(api_cd.get("vn_session_timezone") or "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "Asia/Ho_Chi_Minh"
        tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz=tz)
    now_utc = now_local.astimezone(timezone.utc)
    to_ms = int(now_utc.timestamp() * 1000)
    mode = str(api_cd.get("auto_from_to_mode") or "vn_session").strip().lower()
    if mode in ("countback", "legacy"):
        countback = max(1, int(api_cd.get("auto_countback") or 1000))
        iv = str(step.get("interval") or "")
        bar_ms = _coinmap_interval_minutes(iv) * 60 * 1000
        span = countback * bar_ms
        pad = int(api_cd.get("auto_time_padding_ms") or 0)
        from_ms = to_ms - span - max(0, pad)
        return from_ms, to_ms

    start_hour = int(api_cd.get("vn_session_start_hour") or 5)
    from_ms = _vn_session_anchor_utc_ms(
        now_utc, tz_name=tz_name or "Asia/Ho_Chi_Minh", start_hour=start_hour
    )
    if from_ms > to_ms:
        from_ms = to_ms - 60_000
    return from_ms, to_ms


def _format_api_query_placeholders(
    template: str,
    step: dict[str, Any],
    api_cd: Optional[dict[str, Any]] = None,
) -> str:
    sym = str(step.get("symbol") or "")
    mapping = {
        "symbol": sym,
        "interval": str(step.get("interval") or ""),
        "watchlist_category": str(step.get("watchlist_category") or ""),
    }
    ex = step.get("export_symbol")
    mapping["export_symbol"] = (
        str(ex).strip() if isinstance(ex, str) and str(ex).strip() else sym
    )
    out = template
    for key, val in mapping.items():
        out = out.replace("{" + key + "}", val)
    if "{main_symbol}" in out:
        from automation_tool.images import get_active_main_symbol

        out = out.replace("{main_symbol}", get_active_main_symbol())
    if "{resolution}" in out:
        out = out.replace(
            "{resolution}",
            _coinmap_interval_to_resolution_num(str(step.get("interval") or "")),
        )
    if "{from_ms}" in out or "{to_ms}" in out:
        fm, tm = _coinmap_auto_from_to_ms(api_cd or {}, step)
        out = out.replace("{from_ms}", str(fm)).replace("{to_ms}", str(tm))
    if "{countback}" in out:
        cb = max(1, int((api_cd or {}).get("auto_countback") or 1000))
        out = out.replace("{countback}", str(cb))
    return out


def _merge_api_query_params(api_cd: dict[str, Any], step: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, Any] = {}
    for part in (api_cd.get("query_template"), api_cd.get("extra_query"), step.get("api_query")):
        if isinstance(part, dict):
            merged.update(part)
    out: dict[str, str] = {}
    for k, v in merged.items():
        if v is None:
            continue
        key = str(k)
        if isinstance(v, bool):
            out[key] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[key] = str(v)
        elif isinstance(v, str):
            out[key] = _format_api_query_placeholders(v, step, api_cd)
        else:
            out[key] = _format_api_query_placeholders(str(v), step, api_cd)
    return out


def _apply_endpoint_query_overrides(
    out: dict[str, str],
    api_cd: dict[str, Any],
    step: dict[str, Any],
    endpoint_key: str,
) -> dict[str, str]:
    """Merge ``api_data_export.endpoint_query.<endpoint_key>`` (omit_keys + params)."""
    eq = api_cd.get("endpoint_query")
    if not isinstance(eq, dict):
        return out
    spec = eq.get(endpoint_key)
    if not isinstance(spec, dict) or not spec:
        return out
    omit = spec.get("omit_keys")
    if isinstance(omit, (list, tuple)):
        for k in omit:
            out.pop(str(k), None)
    for k, v in spec.items():
        if k == "omit_keys":
            continue
        key = str(k)
        if v is None:
            out.pop(key, None)
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[key] = str(v)
        elif isinstance(v, str):
            out[key] = _format_api_query_placeholders(v, step, api_cd)
        else:
            out[key] = _format_api_query_placeholders(str(v), step, api_cd)
    return out


def _merge_api_query_params_for_endpoint(
    api_cd: dict[str, Any], step: dict[str, Any], endpoint_key: str
) -> dict[str, str]:
    """
    Query string per cm-api endpoint.

    ``getcandlehistorycvd`` uses ``cvd`` (from ``period`` when present) and drops
    ``period`` / ``source`` / ``bandsmultiplier`` which the CVD route does not use.
    Optional ``api_data_export.endpoint_query.<key>`` adds overrides (see
    ``_apply_endpoint_query_overrides``).
    """
    base = _merge_api_query_params(api_cd, step)
    out = dict(base)
    if endpoint_key == "getcandlehistorycvd":
        period_val = out.pop("period", None)
        out.pop("source", None)
        out.pop("bandsmultiplier", None)
        if "cvd" not in out:
            out["cvd"] = str(period_val if period_val is not None else "day")
    return _apply_endpoint_query_overrides(out, api_cd, step, endpoint_key)


def _merged_coinmap_api_endpoints(api_cd: dict[str, Any]) -> dict[str, str]:
    d = dict(_DEFAULT_COINMAP_GW_ENDPOINTS)
    over = api_cd.get("endpoints")
    if isinstance(over, dict):
        for k, v in over.items():
            if isinstance(v, str) and v.strip():
                d[str(k)] = v.strip()
    return d


def _coinmap_api_request_one(
    context,
    *,
    url: str,
    method: str,
    params: dict[str, str],
    headers: Optional[dict[str, str]],
    max_nonjson_body_chars: int,
) -> dict[str, Any]:
    method_u = (method or "GET").strip().upper() or "GET"
    req = context.request
    hdrs = {str(a): str(b) for a, b in (headers or {}).items()} if headers else None
    try:
        if method_u == "GET":
            r = req.get(url, params=params or None, headers=hdrs, timeout=90_000)
        elif method_u == "POST":
            r = req.post(url, params=params or None, headers=hdrs, timeout=90_000)
        else:
            return {"ok": False, "status": 0, "body": f"unsupported http_method {method_u!r}"}
        status = r.status
        text = r.text()
        ok = 200 <= status < 300
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError:
            lim = max(256, int(max_nonjson_body_chars))
            body = text if len(text) <= lim else text[:lim] + "...(truncated)"
        return {"ok": ok, "status": status, "body": body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


_COINMAP_HTTP_TIMEOUT_S = 90.0


def _coinmap_asyncio_run(coro):
    """
    Run *coro* to completion from synchronous code.

    ``asyncio.run`` fails with "cannot be called from a running event loop" when the
    capture worker already runs inside an event loop (e.g. RPC). In that case we
    execute ``asyncio.run`` in a worker thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _bearer_http_parallel_enabled(api_cd: dict[str, Any]) -> bool:
    """Parallel httpx calls to cm-api only; Playwright stays sequential. Default on."""
    return api_cd.get("bearer_http_parallel") is not False


async def _coinmap_api_request_httpx_async(
    client: httpx.AsyncClient,
    *,
    url: str,
    method: str,
    params: dict[str, str],
    headers: Optional[dict[str, str]],
    max_nonjson_body_chars: int,
) -> dict[str, Any]:
    method_u = (method or "GET").strip().upper() or "GET"
    hdrs = {str(a): str(b) for a, b in (headers or {}).items()} if headers else None
    try:
        if method_u == "GET":
            r = await client.get(url, params=params or None, headers=hdrs)
        elif method_u == "POST":
            r = await client.post(url, params=params or None, headers=hdrs)
        else:
            return {"ok": False, "status": 0, "body": f"unsupported http_method {method_u!r}"}
        status = r.status_code
        resp_text = r.text
        ok = 200 <= status < 300
        try:
            body: Any = json.loads(resp_text)
        except json.JSONDecodeError:
            lim = max(256, int(max_nonjson_body_chars))
            body = (
                resp_text
                if len(resp_text) <= lim
                else resp_text[:lim] + "...(truncated)"
            )
        return {"ok": ok, "status": status, "body": body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


async def _coinmap_collect_one_shot_api_data_httpx_async(
    client: httpx.AsyncClient,
    api_cd: dict[str, Any],
    step: dict[str, Any],
    *,
    bearer_authorization: Optional[str] = None,
) -> dict[str, Any]:
    endpoints = _merged_coinmap_api_endpoints(api_cd)
    method = str(api_cd.get("http_method") or "GET")
    headers = _coinmap_build_api_request_headers(
        api_cd, bearer_authorization=bearer_authorization
    )
    max_ch = int(api_cd.get("max_nonjson_body_chars") or 8000)
    fail = bool(api_cd.get("fail_on_api_error"))
    out: dict[str, Any] = {
        "symbol": step.get("symbol"),
        "interval": step.get("interval"),
        "watchlist_category": step.get("watchlist_category"),
    }
    ex = step.get("export_symbol")
    if isinstance(ex, str) and ex.strip():
        out["export_symbol"] = ex.strip()

    async def fetch_one(key: str) -> tuple[str, dict[str, Any]]:
        url = endpoints.get(key)
        if not url:
            return (
                key,
                {
                    "ok": False,
                    "status": 0,
                    "body": "missing endpoint in api_data_export.endpoints",
                },
            )
        q = _merge_api_query_params_for_endpoint(api_cd, step, key)
        res = await _coinmap_api_request_httpx_async(
            client,
            url=url,
            method=method,
            params=q,
            headers=headers,
            max_nonjson_body_chars=max_ch,
        )
        return key, res

    pairs = await asyncio.gather(*[fetch_one(k) for k in _COINMAP_API_KEYS])
    for key, res in pairs:
        out[key] = res
        if fail and not res["ok"]:
            raise SystemExit(
                f"api_data_export: request {key!r} failed status={res['status']} body={res['body']!r}"
            )
    return out


def _coinmap_collect_one_shot_api_data_httpx(
    api_cd: dict[str, Any],
    step: dict[str, Any],
    *,
    bearer_authorization: Optional[str] = None,
) -> dict[str, Any]:
    """Sync: cm-api GETs in parallel via httpx (no Playwright)."""

    async def _run() -> dict[str, Any]:
        timeout = httpx.Timeout(_COINMAP_HTTP_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await _coinmap_collect_one_shot_api_data_httpx_async(
                client,
                api_cd,
                step,
                bearer_authorization=bearer_authorization,
            )

    return _coinmap_asyncio_run(_run())


async def _coinmap_api_only_plan_gather_shots(
    api_cd: dict[str, Any],
    plan: list[dict[str, Any]],
    bearer_authorization: str,
) -> list[dict[str, Any]]:
    """Parallel plan steps; each step runs all configured cm-api GETs in parallel. No Playwright."""
    max_raw = api_cd.get("bearer_parallel_max_concurrency")
    max_c = int(max_raw) if isinstance(max_raw, int) and max_raw > 0 else 0
    timeout = httpx.Timeout(_COINMAP_HTTP_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as client:

        async def one_step(st: dict[str, Any]) -> dict[str, Any]:
            step_ctx: dict[str, Any] = {
                "symbol": st["symbol"],
                "interval": st["interval"],
                "watchlist_category": st.get("watchlist_category"),
                "api_query": st.get("api_query"),
            }
            exs = st.get("export_symbol")
            if isinstance(exs, str) and exs.strip():
                step_ctx["export_symbol"] = exs.strip()
            return await _coinmap_collect_one_shot_api_data_httpx_async(
                client,
                api_cd,
                step_ctx,
                bearer_authorization=bearer_authorization,
            )

        if max_c > 0:
            sem = asyncio.Semaphore(max_c)

            async def bounded(st: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    return await one_step(st)

            return list(await asyncio.gather(*[bounded(s) for s in plan]))
        return list(await asyncio.gather(*[one_step(s) for s in plan]))



def _coinmap_build_api_request_headers(
    api_cd: dict[str, Any], *, bearer_authorization: Optional[str] = None
) -> Optional[dict[str, str]]:
    rh = api_cd.get("request_headers")
    headers: dict[str, str] = {}
    if isinstance(rh, dict):
        headers = {str(a): str(b) for a, b in rh.items()}
    if bearer_authorization:
        headers["Authorization"] = bearer_authorization.strip()
        if api_cd.get("send_m_affiliate", True) and not any(
            k.lower() == "m_affiliate" for k in headers
        ):
            headers["m_affiliate"] = str(api_cd.get("m_affiliate") or "CM")
    return headers or None


def _coinmap_collect_one_shot_api_data(
    page,
    api_cd: dict[str, Any],
    step: dict[str, Any],
    *,
    bearer_authorization: Optional[str] = None,
) -> dict[str, Any]:
    context = page.context
    endpoints = _merged_coinmap_api_endpoints(api_cd)
    method = str(api_cd.get("http_method") or "GET")
    headers = _coinmap_build_api_request_headers(
        api_cd, bearer_authorization=bearer_authorization
    )
    max_ch = int(api_cd.get("max_nonjson_body_chars") or 8000)
    fail = bool(api_cd.get("fail_on_api_error"))
    out: dict[str, Any] = {
        "symbol": step.get("symbol"),
        "interval": step.get("interval"),
        "watchlist_category": step.get("watchlist_category"),
    }
    ex = step.get("export_symbol")
    if isinstance(ex, str) and ex.strip():
        out["export_symbol"] = ex.strip()
    for key in _COINMAP_API_KEYS:
        url = endpoints.get(key)
        if not url:
            out[key] = {"ok": False, "status": 0, "body": "missing endpoint in api_data_export.endpoints"}
            continue
        q = _merge_api_query_params_for_endpoint(api_cd, step, key)
        res = _coinmap_api_request_one(
            context,
            url=url,
            method=method,
            params=q,
            headers=headers,
            max_nonjson_body_chars=max_ch,
        )
        out[key] = res
        if fail and not res["ok"]:
            raise SystemExit(
                f"api_data_export: request {key!r} failed status={res['status']} body={res['body']!r}"
            )
    return out


def _coinmap_shot_from_network(
    net_capture: CoinmapNetworkCapture, start_idx: int, step_ctx: dict[str, Any]
) -> dict[str, Any]:
    grouped = net_capture.consume_shot(start_idx, step_ctx)
    out: dict[str, Any] = {
        "symbol": step_ctx.get("symbol"),
        "interval": step_ctx.get("interval"),
        "watchlist_category": step_ctx.get("watchlist_category"),
    }
    ex = step_ctx.get("export_symbol")
    if isinstance(ex, str) and ex.strip():
        out["export_symbol"] = ex.strip()
    out.update(grouped)
    return out


def _coinmap_maybe_fail_api_shot(api_cd: dict[str, Any], shot: dict[str, Any]) -> None:
    if not api_cd.get("fail_on_api_error"):
        return
    for key in _COINMAP_API_KEYS:
        block = shot.get(key)
        if isinstance(block, dict) and not block.get("ok"):
            raise SystemExit(f"api_data_export: {key!r} failed: {block!r}")


def _api_export_simplify_shot(
    shot: dict[str, Any], api_cd: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Strip ok/status/url; each API key maps to parsed body on success, else null."""
    out: dict[str, Any] = {}
    for k in ("symbol", "interval", "watchlist_category", "export_symbol"):
        if k in shot:
            out[k] = shot[k]
    sym = shot.get("symbol")
    iv = shot.get("interval")
    relax = _relax_symbol_filter_from_api_cd(api_cd)
    for key in _COINMAP_API_KEYS:
        block = shot.get(key)
        if isinstance(block, dict) and block.get("ok"):
            body = block.get("body")
            out[key] = _filter_coinmap_api_array_by_step(
                body, symbol=sym, interval=iv, relax_symbol_if_empty=relax
            )
        else:
            out[key] = None
    return out


def _api_export_slim_disk_enabled(api_cd: Optional[dict[str, Any]]) -> bool:
    """When true, trim 5m/15m arrays before writing JSON (see coinmap_openai_slim)."""
    if api_cd is None:
        return True
    if api_cd.get("use_merged_coinmap_for_openai") is True:
        return False
    return bool(api_cd.get("slim_export_on_disk", True))


def _write_coinmap_api_shot_json(
    charts_dir: Path,
    *,
    file_stem: str,
    stamp: str,
    shot: dict[str, Any],
    api_cd: Optional[dict[str, Any]] = None,
) -> Path:
    """One JSON per chart shot, same stem as the PNG (e.g. {stamp}_coinmap_USDINDEX_15m)."""
    _ensure_dir(charts_dir)
    path = charts_dir / f"{file_stem}.json"
    simplified = _api_export_simplify_shot(shot, api_cd=api_cd)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stamp": stamp,
        **simplified,
    }
    ex = shot.get("export_symbol")
    if isinstance(ex, str) and ex.strip():
        payload = _coinmap_apply_export_symbol_to_payload(
            payload,
            internal_symbol=shot.get("symbol"),
            export_symbol=ex.strip(),
        )
    payload.pop("export_symbol", None)
    if _api_export_slim_disk_enabled(api_cd):
        payload = slim_coinmap_export_for_openai(payload, path=path)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Coinmap API export: {path.resolve()}", flush=True)
    return path


def _maybe_switch_to_dark_mode(page, cd: dict[str, Any]) -> None:
    """
    Prefer dark mode: if the theme control does not contain a sun icon span, click it once.
    Runs before the light-theme confirmation modal.
    """
    if not cd.get("dark_mode_enabled", True):
        return
    btn_sel = (cd.get("dark_mode_theme_button_selector") or "").strip()
    if not btn_sel:
        btn_sel = '[class*="Header_menuIconTheme"]'
    sun_sel = (cd.get("dark_mode_sun_icon_selector") or "span.anticon-sun").strip()
    try:
        btn = page.locator(btn_sel).first
        btn.wait_for(state="visible", timeout=15_000)
        if btn.locator(sun_sel).count() == 0:
            btn.click(timeout=10_000)
            page.wait_for_timeout(int(cd.get("dark_mode_after_click_ms", 400)))
    except Exception:
        pass


def _maybe_dismiss_light_theme_modal(page, cd: dict[str, Any]) -> None:
    """If a 'use light theme' (or similar) confirmation appears, click Confirm and continue."""
    if not cd.get("light_theme_confirm_enabled", True):
        return
    sel = (cd.get("light_theme_confirm_selector") or "").strip()
    if not sel:
        sel = 'button:has-text("Confirm"), button:has-text("OK"), button:has-text("Continue")'
    wait_ms = int(cd.get("light_theme_modal_wait_ms", 2500))
    try:
        loc = page.locator(sel).first
        loc.wait_for(state="visible", timeout=wait_ms)
        loc.click(timeout=10_000)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _maybe_dismiss_coinmap_symbol_search_modal(page, cd: dict[str, Any]) -> None:
    """
    Coinmap may auto-open the symbol search modal on /chart. That layer blocks the
    right sidebar watchlist; we no longer open that modal from automation.
    """
    if not cd.get("dismiss_symbol_search_modal", True):
        return
    close_sel = (cd.get("symbol_search_modal_close_selector") or "").strip()
    after = int(cd.get("after_symbol_modal_dismiss_ms", 350))
    gap = int(cd.get("symbol_modal_escape_gap_ms", 180))
    if close_sel:
        try:
            page.locator(close_sel).first.click(timeout=5_000)
            page.wait_for_timeout(after)
        except Exception:
            pass
    presses = max(0, int(cd.get("symbol_search_modal_escape_presses", 2)))
    for _ in range(presses):
        page.keyboard.press("Escape")
        page.wait_for_timeout(gap)
    page.wait_for_timeout(after)


def _coinmap_press_escape_n(page, cd: dict[str, Any], *, presses: int) -> None:
    """Send Escape `presses` times with configurable gap (no-op if presses <= 0)."""
    n = max(0, int(presses))
    if n <= 0:
        return
    gap = max(0, int(cd.get("coinmap_fullscreen_exit_escape_gap_ms", 200)))
    for _ in range(n):
        page.keyboard.press("Escape")
        page.wait_for_timeout(gap)


def _coinmap_exit_fullscreen_after_capture(page, cd: dict[str, Any]) -> None:
    """
    After a fullscreen screenshot, leave Coinmap chart fullscreen so the next step can
    use the header and right-sidebar toggle. Coinmap may use a different header action
    index for exit than for enter (see coinmap_fullscreen_exit_action_button_index).
    """
    if bool(cd.get("coinmap_fullscreen_exit_use_toolbar_click", True)):
        exit_idx = int(cd.get("coinmap_fullscreen_exit_action_button_index", 0))
        _coinmap_click_fullscreen_button(
            page,
            cd,
            button_index=exit_idx,
            force=bool(cd.get("coinmap_fullscreen_exit_click_force", True)),
        )
        page.wait_for_timeout(int(cd.get("coinmap_fullscreen_exit_after_toolbar_click_ms", 450)))
    else:
        only = max(1, int(cd.get("coinmap_fullscreen_exit_escape_only_presses", 2)))
        _coinmap_press_escape_n(page, cd, presses=only)
        return
    extra = int(cd.get("coinmap_fullscreen_exit_escape_after_toolbar_presses", 0))
    _coinmap_press_escape_n(page, cd, presses=extra)


def _coinmap_unstick_fullscreen_loop_start(page, cd: dict[str, Any]) -> None:
    """
    Start of each multi-shot iteration: Escape only. Do not click the fullscreen toolbar
    button here — if we are not fullscreen, that would enter fullscreen and break the flow.
    """
    presses = max(0, int(cd.get("coinmap_fullscreen_loop_start_escape_presses", 1)))
    _coinmap_press_escape_n(page, cd, presses=presses)


def _login_form_is_visible(page, email_sel: str, password_sel: str, timeout_ms: int = 8000) -> bool:
    """True if email + password fields are on screen (user still needs to log in)."""
    try:
        page.locator(email_sel).first.wait_for(state="visible", timeout=timeout_ms)
        return page.locator(password_sel).first.is_visible()
    except Exception:
        return False


def _coinmap_maybe_relogin_if_login_form_visible(
    page,
    cd: dict[str, Any],
    *,
    email: Optional[str],
    password: Optional[str],
    login_cfg: Optional[dict[str, Any]],
    settle_ms: int,
) -> None:
    """
    Mid-flow: session may expire so the chart step shows the Coinmap login form again.
    If email + password fields are visible, submit then return to the chart URL if still on /login.
    """
    if not email or not password:
        return
    base = login_cfg if isinstance(login_cfg, dict) else {}
    email_sel = base.get("email_selector") or 'input[type="email"]'
    password_sel = base.get("password_selector") or 'input[type="password"]'
    submit_sel = base.get("submit_selector") or 'button[type="submit"]'
    form_timeout = int(base.get("mid_flow_login_form_detect_timeout_ms", 4_000))
    if not _login_form_is_visible(page, email_sel, password_sel, timeout_ms=form_timeout):
        return
    page.locator(email_sel).first.fill(email, timeout=15_000)
    page.locator(password_sel).first.fill(password, timeout=15_000)
    page.locator(submit_sel).first.click(timeout=15_000)
    page.wait_for_load_state("networkidle", timeout=60_000)
    page.wait_for_timeout(settle_ms)
    post_wait = (base.get("post_login_wait_selector") or "").strip()
    if post_wait:
        try:
            page.locator(post_wait).first.wait_for(state="visible", timeout=30_000)
        except Exception:
            pass
    chart_url = cd.get("chart_page_url") or "https://coinmap.tech/chart"
    u = (page.url or "").lower()
    if "login" in u:
        page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(settle_ms)
        _maybe_dismiss_coinmap_symbol_search_modal(page, cd)
        _maybe_switch_to_dark_mode(page, cd)
        _maybe_dismiss_light_theme_modal(page, cd)
        _maybe_dismiss_coinmap_symbol_search_modal(page, cd)


def _chart_drag_in_box(
    page,
    box: dict[str, float],
    *,
    x0r: float,
    y0r: float,
    x1r: float,
    y1r: float,
    drag_steps: int,
) -> None:
    x0 = box["x"] + box["width"] * x0r
    y0 = box["y"] + box["height"] * y0r
    x1 = box["x"] + box["width"] * x1r
    y1 = box["y"] + box["height"] * y1r
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=max(1, drag_steps))
    page.mouse.up()


def _apply_coinmap_chart_view_adjustments(page, cd: dict[str, Any]) -> None:
    """
    After fullscreen: optional drags — time pan at bottom edge (horizontal), price pan at
    right edge (vertical), then main chart pan — same gesture as chart drag, cursor position differs.

    Uses bounding_box + mouse.move instead of locator.hover(): Coinmap stacks react-stockcharts
    SVG/crosshair layers above the background canvas, so hovering the canvas times out.
    """
    if not cd.get("chart_view_adjustments_enabled", True):
        return
    sel = (cd.get("chart_interaction_selector") or "").strip()
    if not sel:
        sel = (
            "svg.react-stockchart-canvas, svg[class*='react-stockchart-canvas'], "
            "svg[class*='react-stockchart']"
        )
    try:
        chart = page.locator(sel).first
        chart.wait_for(state="visible", timeout=25_000)
    except Exception:
        return

    box = chart.bounding_box()
    if not box:
        page.wait_for_timeout(400)
        box = chart.bounding_box()
    if not box:
        return

    between_ms = int(cd.get("chart_between_pan_ms", 200))
    pan_y_default = float(cd.get("chart_pan_y_ratio", 0.52))
    edge_drag_steps = int(cd.get("chart_edge_pan_drag_steps", 12))

    if cd.get("chart_time_edge_pan_enabled", True):
        ty = float(cd.get("chart_time_edge_pan_y_ratio", 0.96))
        tsx = float(cd.get("chart_time_edge_pan_start_x_ratio", 0.38))
        tex = float(cd.get("chart_time_edge_pan_end_x_ratio", 0.58))
        time_repeats = max(1, int(cd.get("chart_time_edge_pan_repeats", 2)))
        for i in range(time_repeats):
            _chart_drag_in_box(
                page,
                box,
                x0r=tsx,
                y0r=ty,
                x1r=tex,
                y1r=ty,
                drag_steps=edge_drag_steps,
            )
            if between_ms and i < time_repeats - 1:
                page.wait_for_timeout(between_ms)
        if between_ms:
            page.wait_for_timeout(between_ms)

    if cd.get("chart_price_edge_pan_enabled", True):
        px = float(cd.get("chart_price_edge_pan_x_ratio", 0.93))
        psy = float(cd.get("chart_price_edge_pan_start_y_ratio", 0.22))
        pey = float(cd.get("chart_price_edge_pan_end_y_ratio", 0.32))
        _chart_drag_in_box(
            page,
            box,
            x0r=px,
            y0r=psy,
            x1r=px,
            y1r=pey,
            drag_steps=edge_drag_steps,
        )
        if between_ms:
            page.wait_for_timeout(between_ms)

    sx = float(cd.get("chart_pan_start_x_ratio", 0.28))
    ex = float(cd.get("chart_pan_end_x_ratio", 0.62))
    y_ratio = float(cd.get("chart_pan_y_ratio", pan_y_default))
    main_steps = int(cd.get("chart_pan_drag_steps", 14))
    _chart_drag_in_box(
        page,
        box,
        x0r=sx,
        y0r=y_ratio,
        x1r=ex,
        y1r=y_ratio,
        drag_steps=main_steps,
    )
    page.wait_for_timeout(int(cd.get("chart_after_adjust_ms", 700)))


def _maybe_click_layer_toggle_if_tooltip_tall(page, cd: dict[str, Any]) -> None:
    """
    If a layer tooltip div is taller than the threshold, click the layer-list toggle
    (e.g. collapse open panel) before screenshot.
    """
    if not cd.get("layer_tooltip_toggle_before_screenshot", True):
        return
    tooltip_sel = (cd.get("layer_tooltip_selector") or '[class*="layerTooltip"]').strip()
    toggle_sel = (cd.get("layer_list_toggle_button_selector") or '[class*="buttonToggleLayerList"]').strip()
    min_h = float(cd.get("layer_tooltip_min_height_px", 19))
    try:
        if page.locator(tooltip_sel).count() == 0:
            return
        tip = page.locator(tooltip_sel).first
        box = tip.bounding_box()
        if not box or box["height"] <= min_h:
            return
        page.locator(toggle_sel).first.click(timeout=15_000)
        page.wait_for_timeout(int(cd.get("after_layer_toggle_click_ms", 400)))
    except Exception:
        pass


def _coinmap_default_capture_plan() -> list[dict[str, Any]]:
    return [
        {"symbol": "XAUUSD", "interval": "15m", "watchlist_category": "forex 1"},
        {"symbol": "XAUUSD", "interval": "5m", "watchlist_category": "forex 1"},
    ]


def _coinmap_parse_capture_plan(cd: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    raw = cd.get("capture_plan")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip()
        iv = str(row.get("interval") or "").strip()
        if not sym or not iv:
            continue
        cat = row.get("watchlist_category")
        if cat is not None:
            cat = str(cat).strip()
            if not cat:
                cat = None
        entry: dict[str, Any] = {"symbol": sym, "interval": iv, "watchlist_category": cat}
        ex = row.get("export_symbol")
        if isinstance(ex, str) and ex.strip():
            entry["export_symbol"] = ex.strip()
        aq = row.get("api_query")
        if isinstance(aq, dict) and aq:
            entry["api_query"] = aq
        out.append(entry)
    return out or None


def _coinmap_resolve_capture_plan(cd: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    if not cd.get("multi_shot_enabled", False):
        return None
    parsed = _coinmap_parse_capture_plan(cd)
    if parsed:
        return parsed
    return _coinmap_default_capture_plan()


def _coinmap_filter_capture_plan_by_intervals(
    plan: Optional[list[dict[str, Any]]],
    intervals: Optional[Sequence[str]],
) -> Optional[list[dict[str, Any]]]:
    """
    Giữ lại các bước multi-shot có ``interval`` thuộc ``intervals`` (so khớp không phân biệt hoa thường).
    Dùng để luồng intraday chỉ tải ví dụ M5 thay vì cả M15.
    """
    if not plan or not intervals:
        return plan
    want = {str(x).strip().lower() for x in intervals if str(x).strip()}
    if not want:
        return plan
    out = [
        step
        for step in plan
        if str(step.get("interval") or "").strip().lower() in want
    ]
    return out or None


def _coinmap_toggle_right_sidebar(
    page,
    cd: dict[str, Any],
    *,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    login_cfg: Optional[dict[str, Any]] = None,
    settle_ms: int = 2000,
) -> None:
    """One click on the sidebar control (open or close depending on current state)."""
    _coinmap_maybe_relogin_if_login_form_visible(
        page,
        cd,
        email=login_email,
        password=login_password,
        login_cfg=login_cfg,
        settle_ms=settle_ms,
    )
    prefix = (cd.get("right_sidebar_container_class_prefix") or "ChartDesktopPage_rightSidebar__").strip()
    ms = int(cd.get("sidebar_toggle_after_click_ms", 450))
    force = bool(cd.get("right_sidebar_toggle_click_force", True))
    custom = (cd.get("right_sidebar_toggle_button_selector") or "").strip()
    if custom:
        btn = page.locator(custom).first
    else:
        action_pf = (cd.get("right_sidebar_action_button_class_prefix") or "ChartDesktopPage_actionButton").strip()
        btn = page.locator(f'[class*="{prefix}"] [class*="{action_pf}"]').first
    btn.wait_for(state="visible", timeout=30_000)
    btn.click(timeout=15_000, force=force)
    page.wait_for_timeout(ms)


def _coinmap_ensure_right_sidebar_open(
    page,
    cd: dict[str, Any],
    *,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    login_cfg: Optional[dict[str, Any]] = None,
    settle_ms: int = 2000,
) -> None:
    """
    Keep clicking the sidebar toggle until ChartWatchList title is visible.
    Handles: sidebar starts closed, narrow rail needs a second click, or wrong initial state.
    """
    wt = (cd.get("watchlist_title_class_prefix") or "ChartWatchList_title__").strip()
    title = page.locator(f'[class*="{wt}"]').first
    quick_ms = int(cd.get("right_sidebar_already_open_check_ms", 2_000))
    max_toggles = max(1, int(cd.get("right_sidebar_open_max_toggles", 4)))
    final_timeout = int(cd.get("watchlist_title_visible_timeout_ms", 20_000))

    for _ in range(max_toggles):
        _coinmap_maybe_relogin_if_login_form_visible(
            page,
            cd,
            email=login_email,
            password=login_password,
            login_cfg=login_cfg,
            settle_ms=settle_ms,
        )
        try:
            title.wait_for(state="visible", timeout=quick_ms)
            return
        except Exception:
            pass
        _coinmap_toggle_right_sidebar(
            page,
            cd,
            login_email=login_email,
            login_password=login_password,
            login_cfg=login_cfg,
            settle_ms=settle_ms,
        )

    title.wait_for(state="visible", timeout=final_timeout)


def _coinmap_click_ant_select_item_option(page, label: str, *, visible_timeout_ms: int = 15_000) -> None:
    """
    Ant Design / rc-select: options are div.ant-select-item-option with title=… and
    .ant-select-item-option-content text — not always exposed as role=option with name.
    """
    by_title = page.locator(f'.ant-select-item-option[title="{label}"]').first
    try:
        by_title.wait_for(state="visible", timeout=visible_timeout_ms)
        by_title.click(timeout=15_000)
        return
    except Exception:
        pass
    opt = page.locator(".ant-select-item-option").filter(
        has=page.locator(".ant-select-item-option-content").get_by_text(label, exact=True)
    ).first
    opt.wait_for(state="visible", timeout=visible_timeout_ms)
    opt.click(timeout=15_000)


def _coinmap_select_watchlist_category(page, cd: dict[str, Any], category_text: str) -> None:
    title_prefix = (cd.get("watchlist_title_class_prefix") or "ChartWatchList_title__").strip()
    after_open = int(cd.get("watchlist_dropdown_open_ms", 350))
    title = page.locator(f'[class*="{title_prefix}"]').first
    title.wait_for(state="visible", timeout=int(cd.get("watchlist_title_visible_timeout_ms", 20_000)))
    sel = title.locator(".ant-select").first
    sel.wait_for(state="visible", timeout=int(cd.get("watchlist_ant_select_visible_timeout_ms", 15_000)))
    sel.click(timeout=15_000)
    page.wait_for_timeout(after_open)
    opt_sel = (cd.get("watchlist_category_option_selector") or "").strip()
    if opt_sel:
        page.locator(opt_sel.format(text=category_text)).first.click(timeout=15_000)
    else:
        _coinmap_click_ant_select_item_option(
            page, category_text, visible_timeout_ms=int(cd.get("watchlist_option_visible_timeout_ms", 15_000))
        )
    page.wait_for_timeout(int(cd.get("after_watchlist_category_ms", 400)))


def _coinmap_select_watchlist_symbol(page, cd: dict[str, Any], symbol: str) -> None:
    name_prefix = (cd.get("watchlist_symbol_name_class_prefix") or "TableData_symbolNameContent__").strip()
    custom = (cd.get("watchlist_symbol_row_selector") or "").strip()
    if custom:
        page.locator(custom.format(symbol=symbol)).first.click(timeout=15_000)
    else:
        page.locator(f'[class*="{name_prefix}"]').get_by_text(symbol, exact=True).first.click(
            timeout=15_000
        )
    page.wait_for_timeout(int(cd.get("after_watchlist_symbol_click_ms", 700)))


def _coinmap_select_interval(page, cd: dict[str, Any], interval_text: str) -> None:
    iv_prefix = (cd.get("interval_select_class_prefix") or "IntervalSelect_intervalSelect__").strip()
    after_open = int(cd.get("interval_dropdown_open_ms", 350))
    root = page.locator(f'[class*="{iv_prefix}"]').first
    root.click(timeout=15_000)
    page.wait_for_timeout(after_open)
    opt_sel = (cd.get("interval_option_selector") or "").strip()
    if opt_sel:
        page.locator(opt_sel.format(text=interval_text)).first.click(timeout=15_000)
    else:
        _coinmap_click_ant_select_item_option(
            page, interval_text, visible_timeout_ms=int(cd.get("interval_option_visible_timeout_ms", 15_000))
        )
    page.wait_for_timeout(int(cd.get("after_interval_select_ms", 800)))


def _coinmap_resolve_api_bump_interval(bump: str, target_interval: str) -> Optional[str]:
    """
    Interval to select before ``target_interval`` so it is never equal to the target.
    Coinmap restores the **last** chart interval (not a fixed default); re-clicking the
    same interval often does not refire gateway APIs — we always switch away first, then back.
    """
    b = bump.strip()
    t = str(target_interval).strip()
    if not b:
        return None
    if b != t:
        return b
    # Config accidentally matches target (e.g. both 15m) — use another common interval.
    alternate: dict[str, str] = {
        "1m": "5m",
        "5m": "15m",
        "15m": "5m",
        "30m": "15m",
        "1h": "15m",
        "4h": "1h",
    }
    alt = alternate.get(t) or ("15m" if t != "15m" else "5m")
    return alt if alt != t else None


def _coinmap_maybe_bump_interval_before_target(
    page,
    cd: dict[str, Any],
    *,
    target_interval: str,
    settle_ms: int,
    for_api_capture: bool,
) -> None:
    """
    Before selecting the capture step's interval, optionally select a **different** interval
    so the chart issues fresh getcandlehistory / orderflow / vwap calls. If the UI already
    shows that interval, choosing it again may not hit the gateway.
    """
    if not for_api_capture:
        return
    raw = (cd.get("api_network_capture_bump_interval") or "").strip()
    bump = _coinmap_resolve_api_bump_interval(raw, target_interval)
    if not bump:
        return
    _coinmap_select_interval(page, cd, bump)
    page.wait_for_timeout(int(cd.get("after_interval_change_settle_ms", settle_ms)))


def _coinmap_click_fullscreen_button(
    page,
    cd: dict[str, Any],
    *,
    button_index: Optional[int] = None,
    force: Optional[bool] = None,
) -> None:
    action_prefix = cd.get("section_header_action_class_prefix") or "SectionHeader_actionButton"
    if button_index is not None:
        idx = int(button_index)
    else:
        idx = int(cd.get("action_button_index", 1))
    parent = (cd.get("section_header_action_parent_selector") or "").strip()
    if parent:
        action_loc = page.locator(parent).locator(f'[class*="{action_prefix}"]')
    else:
        action_loc = page.locator(f'[class*="{action_prefix}"]')
    btn = action_loc.nth(idx)
    btn.wait_for(state="visible", timeout=30_000)
    if force is None:
        force = bool(cd.get("fullscreen_action_button_click_force", False))
    btn.click(timeout=15_000, force=force)


def _coinmap_one_capture_fullscreen_esc(
    page,
    cd: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_slug: str,
    interval_slug: str,
) -> Path:
    """Pan/zoom chart, optional layer toggle, fullscreen, screenshot, exit fullscreen."""
    _apply_coinmap_chart_view_adjustments(page, cd)
    _maybe_click_layer_toggle_if_tooltip_tall(page, cd)
    _coinmap_click_fullscreen_button(page, cd)  # enter fullscreen
    page.wait_for_timeout(int(cd.get("fullscreen_screenshot_settle_ms", 1500)))
    full_page = bool(cd.get("fullscreen_screenshot_full_page", True))
    dest = charts_dir / f"{stamp}_coinmap_{symbol_slug}_{interval_slug}.png"
    page.screenshot(path=str(dest), full_page=full_page)
    _coinmap_exit_fullscreen_after_capture(page, cd)
    page.wait_for_timeout(int(cd.get("after_fullscreen_escape_ms", 600)))
    return dest


def _run_coinmap_multi_shot_flow(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    cd: dict[str, Any],
    plan: list[dict[str, Any]],
    api_cd: Optional[dict[str, Any]] = None,
    net_capture: Optional[CoinmapNetworkCapture] = None,
    bearer_authorization: Optional[str] = None,
    coinmap_email: Optional[str] = None,
    coinmap_password: Optional[str] = None,
    coinmap_login_cfg: Optional[dict[str, Any]] = None,
) -> list[Path]:
    written: list[Path] = []
    prev_symbol: Optional[str] = None
    for step in plan:
        net_start = len(net_capture._records) if net_capture is not None else 0
        # If exit-after-capture missed, Escape only (toolbar click would toggle ON when not fullscreen).
        _coinmap_unstick_fullscreen_loop_start(page, cd)
        sym = step["symbol"]
        interval = step["interval"]
        cat = step.get("watchlist_category")
        need_pick = cat is not None or prev_symbol != sym
        if need_pick:
            _coinmap_ensure_right_sidebar_open(
                page,
                cd,
                login_email=coinmap_email,
                login_password=coinmap_password,
                login_cfg=coinmap_login_cfg,
                settle_ms=settle_ms,
            )
            if cat:
                _coinmap_select_watchlist_category(page, cd, cat)
            _coinmap_select_watchlist_symbol(page, cd, sym)
            _coinmap_toggle_right_sidebar(
                page,
                cd,
                login_email=coinmap_email,
                login_password=coinmap_password,
                login_cfg=coinmap_login_cfg,
                settle_ms=settle_ms,
            )

        _coinmap_maybe_bump_interval_before_target(
            page,
            cd,
            target_interval=interval,
            settle_ms=settle_ms,
            for_api_capture=api_cd is not None
            and (net_capture is not None or bearer_authorization is not None),
        )
        _coinmap_select_interval(page, cd, interval)
        page.wait_for_timeout(int(cd.get("after_interval_change_settle_ms", settle_ms)))

        step_ctx: dict[str, Any] = {
            "symbol": sym,
            "interval": interval,
            "watchlist_category": cat,
            "api_query": step.get("api_query"),
        }
        ex = step.get("export_symbol")
        if isinstance(ex, str) and ex.strip():
            step_ctx["export_symbol"] = ex.strip()
        label = (ex.strip() if isinstance(ex, str) and ex.strip() else sym)
        sym_slug = re.sub(r"[^\w.-]+", "_", label).strip("_")[:40] or "sym"
        iv_slug = re.sub(r"[^\w]+", "_", interval).strip("_")[:20] or "iv"
        json_path: Optional[Path] = None
        if net_capture is not None and api_cd is not None:
            shot = _coinmap_shot_from_network(net_capture, net_start, step_ctx)
            _coinmap_maybe_fail_api_shot(api_cd, shot)
            stem = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
            json_path = _write_coinmap_api_shot_json(
                charts_dir, file_stem=stem, stamp=stamp, shot=shot, api_cd=api_cd
            )
        elif api_cd is not None:
            if bearer_authorization:
                _coinmap_bearer_log(
                    api_cd,
                    f"After UI step: cm-api HTTP (symbol={sym!r} interval={interval!r})",
                )
            if (
                bearer_authorization
                and _api_export_mode(api_cd) == "bearer_request"
                and _bearer_http_parallel_enabled(api_cd)
            ):
                shot = _coinmap_collect_one_shot_api_data_httpx(
                    api_cd,
                    step_ctx,
                    bearer_authorization=bearer_authorization,
                )
            else:
                shot = _coinmap_collect_one_shot_api_data(
                    page,
                    api_cd,
                    step_ctx,
                    bearer_authorization=bearer_authorization,
                )
            stem = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
            json_path = _write_coinmap_api_shot_json(
                charts_dir, file_stem=stem, stamp=stamp, shot=shot, api_cd=api_cd
            )

        shot_enabled = bool(cd.get("coinmap_screenshot_enabled", True))
        if shot_enabled:
            written.append(
                _coinmap_one_capture_fullscreen_esc(
                    page, cd, charts_dir, stamp, sym_slug, iv_slug
                )
            )
        else:
            _apply_coinmap_chart_view_adjustments(page, cd)
            page.wait_for_timeout(int(cd.get("chart_after_adjust_ms", 800)))
            if json_path is not None:
                written.append(json_path)
            else:
                written.append(
                    _coinmap_one_capture_fullscreen_esc(
                        page, cd, charts_dir, stamp, sym_slug, iv_slug
                    )
                )
        prev_symbol = sym
    return written


def _coinmap_run_bearer_api_only_exports(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    cd: dict[str, Any],
    api_cd: dict[str, Any],
    auth: str,
    only_retry_paths: Optional[list[Path]] = None,
    prefer_httpx: bool = False,
    abort_on_http_auth_error: bool = False,
    coinmap_capture_intervals: Optional[Sequence[str]] = None,
) -> list[Path]:
    """
    After ``auth`` is known, resolve capture plan and write cm-api JSON files (httpx and/or
    Playwright ``context.request`` per ``prefer_httpx`` / parallel settings).

    If ``abort_on_http_auth_error`` is true, any 401/403 on cm-api endpoints raises
    :class:`CoinmapBearerCacheInvalid` before writing files.
    """
    if not prefer_httpx and page is None:
        raise ValueError("coinmap bearer export: page is required when prefer_httpx is False")

    written: list[Path] = []
    plan = _coinmap_resolve_capture_plan(cd)
    if only_retry_paths:
        if not plan:
            raise SystemExit(
                "coinmap bearer retry requires multi_shot capture_plan "
                "(cannot target paths for single-shot fullscreen export)."
            )
        from automation_tool.chart_payload_validate import filter_coinmap_plan_for_retry_paths

        plan = filter_coinmap_plan_for_retry_paths(plan, stamp, list(only_retry_paths))
        if not plan:
            raise SystemExit(
                "coinmap bearer retry: no capture_plan steps match the target files "
                f"(stamp={stamp!r})."
            )
    if plan is not None and coinmap_capture_intervals:
        plan = _coinmap_filter_capture_plan_by_intervals(plan, coinmap_capture_intervals)
        if not plan:
            raise SystemExit(
                "coinmap_capture_intervals removed all capture_plan steps "
                f"(filter={list(coinmap_capture_intervals)!r})."
            )
    if plan:
        n = len(plan)
        use_parallel = _bearer_http_parallel_enabled(api_cd) or prefer_httpx
        if use_parallel:
            _coinmap_bearer_log(
                api_cd,
                f"Fetching cm-api for {n} capture_plan step(s) (httpx: parallel shots + endpoints)",
            )
            shots = _coinmap_asyncio_run(
                _coinmap_api_only_plan_gather_shots(api_cd, plan, auth)
            )
            for shot in shots:
                if abort_on_http_auth_error and _coinmap_bearer_shot_has_auth_failure(shot):
                    raise CoinmapBearerCacheInvalid()
            for idx, (step, shot) in enumerate(zip(plan, shots), start=1):
                sym = step["symbol"]
                interval = step["interval"]
                ex = step.get("export_symbol")
                label = (ex.strip() if isinstance(ex, str) and ex.strip() else sym)
                sym_slug = re.sub(r"[^\w.-]+", "_", label).strip("_")[:40] or "sym"
                iv_slug = re.sub(r"[^\w]+", "_", interval).strip("_")[:20] or "iv"
                _coinmap_bearer_log(
                    api_cd,
                    f"Step {idx}/{n}: wrote JSON (symbol={sym!r} interval={interval!r})",
                )
                _coinmap_maybe_fail_api_shot(api_cd, shot)
                stem = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
                json_path = _write_coinmap_api_shot_json(
                    charts_dir, file_stem=stem, stamp=stamp, shot=shot, api_cd=api_cd
                )
                written.append(json_path)
        else:
            _coinmap_bearer_log(api_cd, f"Fetching cm-api for {n} capture_plan step(s)")
            for idx, step in enumerate(plan, start=1):
                sym = step["symbol"]
                interval = step["interval"]
                cat = step.get("watchlist_category")
                step_ctx: dict[str, Any] = {
                    "symbol": sym,
                    "interval": interval,
                    "watchlist_category": cat,
                    "api_query": step.get("api_query"),
                }
                ex = step.get("export_symbol")
                if isinstance(ex, str) and ex.strip():
                    step_ctx["export_symbol"] = ex.strip()
                label = (ex.strip() if isinstance(ex, str) and ex.strip() else sym)
                sym_slug = re.sub(r"[^\w.-]+", "_", label).strip("_")[:40] or "sym"
                iv_slug = re.sub(r"[^\w]+", "_", interval).strip("_")[:20] or "iv"
                _coinmap_bearer_log(
                    api_cd,
                    f"Step {idx}/{n}: HTTP GET cm-api (symbol={sym!r} interval={interval!r})",
                )
                if prefer_httpx:
                    shot = _coinmap_collect_one_shot_api_data_httpx(
                        api_cd, step_ctx, bearer_authorization=auth
                    )
                else:
                    shot = _coinmap_collect_one_shot_api_data(
                        page, api_cd, step_ctx, bearer_authorization=auth
                    )
                if abort_on_http_auth_error and _coinmap_bearer_shot_has_auth_failure(shot):
                    raise CoinmapBearerCacheInvalid()
                _coinmap_maybe_fail_api_shot(api_cd, shot)
                stem = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
                json_path = _write_coinmap_api_shot_json(
                    charts_dir, file_stem=stem, stamp=stamp, shot=shot, api_cd=api_cd
                )
                written.append(json_path)
    else:
        _coinmap_bearer_log(
            api_cd,
            "Single-shot cm-api (no multi_shot capture_plan): stem chart_fullscreen",
        )
        use_parallel = _bearer_http_parallel_enabled(api_cd) or prefer_httpx
        if use_parallel:
            shot = _coinmap_collect_one_shot_api_data_httpx(
                api_cd,
                {"symbol": "", "interval": "", "watchlist_category": None},
                bearer_authorization=auth,
            )
        else:
            shot = _coinmap_collect_one_shot_api_data(
                page,
                api_cd,
                {"symbol": "", "interval": "", "watchlist_category": None},
                bearer_authorization=auth,
            )
        if abort_on_http_auth_error and _coinmap_bearer_shot_has_auth_failure(shot):
            raise CoinmapBearerCacheInvalid()
        _coinmap_maybe_fail_api_shot(api_cd, shot)
        json_path = _write_coinmap_api_shot_json(
            charts_dir,
            file_stem=f"{stamp}_chart_fullscreen",
            stamp=stamp,
            shot=shot,
            api_cd=api_cd,
        )
        written.append(json_path)
    _coinmap_bearer_log(
        api_cd,
        f"API-only export finished: wrote {len(written)} JSON file(s)",
    )
    return written


def _run_bearer_request_api_only_flow(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    cd: dict[str, Any],
    api_cd: dict[str, Any],
    bearer_capture: CoinmapBearerCapture,
    only_retry_paths: Optional[list[Path]] = None,
    coinmap_capture_intervals: Optional[Sequence[str]] = None,
) -> list[Path]:
    """
    Login already completed; capture Bearer from network, optional one-shot navigation,
    then fetch cm-api JSON via ``context.request`` (no sidebar / watchlist / pan UI).

    When ``bearer_skip_chart_ui: false``, this function is not used; the main chart flow
    runs multi-shot UI and passes ``bearer_authorization`` into ``_run_coinmap_multi_shot_flow``.
    """
    _coinmap_bearer_log(
        api_cd,
        "API-only bearer_request (no sidebar / watchlist / fullscreen chart UI)",
    )
    trigger = (
        api_cd.get("bearer_trigger_url") or cd.get("chart_page_url") or "https://coinmap.tech/chart"
    )
    trigger = str(trigger).strip() or "https://coinmap.tech/chart"
    _coinmap_bearer_log(api_cd, f"Navigating to bearer_trigger_url / chart: {trigger}")
    page.goto(trigger, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(settle_ms)
    timeout_ms = int(
        api_cd.get("bearer_ready_timeout_ms")
        or api_cd.get("network_capture_wait_ms")
        or 15_000
    )
    auth = bearer_capture.wait_for_authorization(timeout_ms)
    if not auth:
        raise SystemExit(
            "api_data_export bearer_request: no Authorization on requests to gw.coinmap.tech "
            f"within {timeout_ms}ms. Check login, bearer_trigger_url, or use mode network_capture."
        )

    written = _coinmap_run_bearer_api_only_exports(
        page,
        charts_dir=charts_dir,
        stamp=stamp,
        cd=cd,
        api_cd=api_cd,
        auth=auth,
        only_retry_paths=only_retry_paths,
        prefer_httpx=False,
        abort_on_http_auth_error=False,
        coinmap_capture_intervals=coinmap_capture_intervals,
    )
    if _coinmap_bearer_token_cache_enabled(api_cd):
        cache_path = _coinmap_bearer_token_cache_resolved_path(api_cd)
        _coinmap_write_bearer_token_cache(cache_path, auth)
        _coinmap_bearer_log(api_cd, f"Saved bearer token cache: {cache_path}")
    return written


def _run_chart_screenshot_flow(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    cd: dict[str, Any],
    coinmap_email: Optional[str] = None,
    coinmap_password: Optional[str] = None,
    coinmap_login_cfg: Optional[dict[str, Any]] = None,
    bearer_capture: Optional[CoinmapBearerCapture] = None,
    coinmap_bearer_only_retry_paths: Optional[list[Path]] = None,
    coinmap_capture_intervals: Optional[Sequence[str]] = None,
) -> list[Path]:
    """Open chart: multi-shot watchlist sidebar flow, or single fullscreen on current symbol (no modal search)."""
    api_cd = _api_export_config(cd)
    mode = _api_export_mode(api_cd) if api_cd else "network_capture"
    # bearer_request + API-only: no sidebar / fullscreen PNG (legacy fast path).
    if (
        mode == "bearer_request"
        and api_cd is not None
        and api_cd.get("bearer_skip_chart_ui") is not False
    ):
        _coinmap_bearer_log(
            api_cd,
            "chart_download: bearer_request branch = API-only (set bearer_skip_chart_ui: false for full UI)",
        )
        if bearer_capture is None:
            raise SystemExit(
                "api_data_export bearer_request requires bearer capture (internal error: bearer_capture missing)."
            )
        return _run_bearer_request_api_only_flow(
            page,
            charts_dir=charts_dir,
            stamp=stamp,
            settle_ms=settle_ms,
            cd=cd,
            api_cd=api_cd,
            bearer_capture=bearer_capture,
            only_retry_paths=coinmap_bearer_only_retry_paths,
            coinmap_capture_intervals=coinmap_capture_intervals,
        )

    net_capture: Optional[CoinmapNetworkCapture] = None
    if api_cd is not None and mode not in ("request", "bearer_request"):
        net_capture = CoinmapNetworkCapture(page, api_cd)
        net_capture.install()

    url = cd.get("chart_page_url") or "https://coinmap.tech/chart"
    bearer_auth: Optional[str] = None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(settle_ms)
        _maybe_dismiss_coinmap_symbol_search_modal(page, cd)
        _maybe_switch_to_dark_mode(page, cd)
        _maybe_dismiss_light_theme_modal(page, cd)
        _maybe_dismiss_coinmap_symbol_search_modal(page, cd)

        if (
            mode == "bearer_request"
            and api_cd is not None
            and api_cd.get("bearer_skip_chart_ui") is False
        ):
            _coinmap_bearer_log(
                api_cd,
                "chart_download: bearer_request + full UI — wait for Bearer after chart load, "
                "then multi-shot + cm-api per step",
            )
            if bearer_capture is None:
                raise SystemExit(
                    "api_data_export bearer_request with bearer_skip_chart_ui: false requires "
                    "bearer capture (internal error: bearer_capture missing)."
                )
            timeout_ms = int(
                api_cd.get("bearer_ready_timeout_ms")
                or api_cd.get("network_capture_wait_ms")
                or 15_000
            )
            _coinmap_bearer_log(
                api_cd,
                f"After chart modals: waiting for Bearer before multi-shot (timeout {timeout_ms}ms)",
            )
            bearer_auth = bearer_capture.wait_for_authorization(timeout_ms)
            if not bearer_auth:
                raise SystemExit(
                    "api_data_export bearer_request: no Authorization on requests to gw.coinmap.tech "
                    f"within {timeout_ms}ms. Check login, chart_page_url, or use mode network_capture."
                )

        plan = _coinmap_resolve_capture_plan(cd)
        if plan is not None and coinmap_capture_intervals:
            plan = _coinmap_filter_capture_plan_by_intervals(plan, coinmap_capture_intervals)
            if not plan:
                raise SystemExit(
                    "coinmap_capture_intervals removed all capture_plan steps "
                    f"(filter={list(coinmap_capture_intervals)!r})."
                )
        if plan:
            paths = _run_coinmap_multi_shot_flow(
                page,
                charts_dir=charts_dir,
                stamp=stamp,
                settle_ms=settle_ms,
                cd=cd,
                plan=plan,
                api_cd=api_cd,
                net_capture=net_capture,
                bearer_authorization=bearer_auth,
                coinmap_email=coinmap_email,
                coinmap_password=coinmap_password,
                coinmap_login_cfg=coinmap_login_cfg,
            )
            return paths

        json_path: Optional[Path] = None
        if net_capture is not None and api_cd is not None:
            shot = _coinmap_shot_from_network(
                net_capture,
                0,
                {"symbol": "", "interval": "", "watchlist_category": None},
            )
            _coinmap_maybe_fail_api_shot(api_cd, shot)
            json_path = _write_coinmap_api_shot_json(
                charts_dir,
                file_stem=f"{stamp}_chart_fullscreen",
                stamp=stamp,
                shot=shot,
                api_cd=api_cd,
            )
        elif api_cd is not None:
            if (
                bearer_auth
                and mode == "bearer_request"
                and _bearer_http_parallel_enabled(api_cd)
            ):
                shot = _coinmap_collect_one_shot_api_data_httpx(
                    api_cd,
                    {"symbol": "", "interval": "", "watchlist_category": None},
                    bearer_authorization=bearer_auth,
                )
            else:
                shot = _coinmap_collect_one_shot_api_data(
                    page,
                    api_cd,
                    {"symbol": "", "interval": "", "watchlist_category": None},
                    bearer_authorization=bearer_auth,
                )
            json_path = _write_coinmap_api_shot_json(
                charts_dir,
                file_stem=f"{stamp}_chart_fullscreen",
                stamp=stamp,
                shot=shot,
                api_cd=api_cd,
            )

        shot_enabled = bool(cd.get("coinmap_screenshot_enabled", True))
        if not shot_enabled:
            _apply_coinmap_chart_view_adjustments(page, cd)
            page.wait_for_timeout(int(cd.get("chart_after_adjust_ms", 800)))
            if json_path is not None:
                return [json_path]
            _coinmap_click_fullscreen_button(page, cd)
            fs_wait = int(cd.get("fullscreen_screenshot_settle_ms", 2000))
            page.wait_for_timeout(fs_wait)
            _apply_coinmap_chart_view_adjustments(page, cd)
            _maybe_click_layer_toggle_if_tooltip_tall(page, cd)
            full_page = bool(cd.get("fullscreen_screenshot_full_page", True))
            dest = charts_dir / f"{stamp}_chart_fullscreen.png"
            page.screenshot(path=str(dest), full_page=full_page)
            return [dest]

        _coinmap_click_fullscreen_button(page, cd)
        fs_wait = int(cd.get("fullscreen_screenshot_settle_ms", 2000))
        page.wait_for_timeout(fs_wait)
        _apply_coinmap_chart_view_adjustments(page, cd)
        _maybe_click_layer_toggle_if_tooltip_tall(page, cd)
        full_page = bool(cd.get("fullscreen_screenshot_full_page", True))
        dest = charts_dir / f"{stamp}_chart_fullscreen.png"
        page.screenshot(path=str(dest), full_page=full_page)
        return [dest]
    finally:
        if net_capture is not None:
            net_capture.uninstall()


def _maybe_tradingview_dark_mode(page, tv: dict[str, Any]) -> None:
    """
    TradingView: open top-left menu (class prefix topLeftButton-*), then if the
    theme row's switch input does not have aria-checked="true", click the label row
    to enable dark theme. The real input is under label > … > div.switchWrap > span
    > input#theme-switcher (not the first label child div, which is labelRow only).
    Uses force=True on the label click because TradingView sets aria-disabled on
    the switch while the menu is open.
    """
    if not tv.get("dark_mode_enabled", True):
        return
    prefix = (tv.get("dark_mode_menu_button_class_prefix") or "topLeftButton-").strip()
    label_for = (tv.get("theme_switcher_label_for") or "theme-switcher").strip()
    switch_sel = (tv.get("theme_switch_input_selector") or "input#theme-switcher").strip()
    open_ms = int(tv.get("dark_mode_menu_open_ms", 500))
    after_ms = int(tv.get("dark_mode_after_theme_click_ms", 600))
    menu_sel = f'[class*="{prefix}"]'
    menu_opened = False
    try:
        menu_btn = page.locator(menu_sel).first
        menu_btn.wait_for(state="visible", timeout=20_000)
        menu_btn.click(timeout=15_000)
        menu_opened = True
        page.wait_for_timeout(open_ms)
        label = page.locator(f'label[for="{label_for}"]').first
        label.wait_for(state="visible", timeout=10_000)
        switch_input = label.locator(switch_sel).first
        switch_input.wait_for(state="attached", timeout=10_000)
        aria_checked = (switch_input.get_attribute("aria-checked") or "").lower()
        if aria_checked != "true":
            label.click(timeout=10_000, force=True)
            page.wait_for_timeout(after_ms)
    except Exception:
        pass
    finally:
        if menu_opened:
            try:
                page.locator(menu_sel).first.click(timeout=15_000)
                page.wait_for_timeout(after_ms)
            except Exception:
                pass


def _maybe_tradingview_login(
    page,
    tv: dict[str, Any],
    email: Optional[str],
    password: Optional[str],
) -> None:
    """
    After chart load: open top-left menu, click "Đăng nhập" if shown (else already
    logged in), fill COINMAP_EMAIL + TRADINGVIEW_PASSWORD, submit. Login UI closes
    on its own; we then wait until the chart UI (intervals toolbar) is visible again.
    Closes the menu if it stays open after skipping login.
    """
    if not tv.get("login_enabled", True):
        return
    if not email or not password:
        return

    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip().lstrip("#")
    chart_ready_sel = (tv.get("login_chart_ready_selector") or "").strip() or f"#{intervals_id}"
    chart_ready_timeout_ms = int(tv.get("login_chart_ready_timeout_ms", 90_000))

    prefix = (tv.get("dark_mode_menu_button_class_prefix") or "topLeftButton-").strip()
    menu_sel = f'[class*="{prefix}"]'
    open_ms = int(tv.get("login_menu_open_ms", 500))
    sign_timeout = int(tv.get("login_sign_in_visible_timeout_ms", 5_000))
    after_sign_ms = int(tv.get("login_after_sign_in_click_ms", 1_500))
    method_timeout = int(tv.get("login_method_visible_timeout_ms", 8_000))
    after_method_ms = int(tv.get("login_after_method_click_ms", 1_000))
    post_submit_ms = int(tv.get("login_post_submit_settle_ms", 800))

    email_sel = (tv.get("login_email_selector") or "").strip() or (
        'input[type="email"], input#id_username, input[name="username"], '
        'input[name="email"], input[autocomplete="username"]'
    )
    pass_sel = (tv.get("login_password_selector") or 'input[type="password"]').strip()
    submit_sel = (tv.get("login_submit_selector") or "").strip() or (
        'button[type="submit"], button:has-text("Đăng nhập"), button:has-text("Sign in")'
    )

    sign_in_custom = (tv.get("login_sign_in_selector") or "").strip()
    sign_in_text = (tv.get("login_sign_in_text") or "Đăng nhập").strip()
    login_method_sel = (tv.get("login_email_method_selector") or "").strip()
    login_method_text = (tv.get("login_email_method_text") or "").strip()
    iframe_sel = (tv.get("login_iframe_selector") or "").strip()

    menu_opened = False
    try:
        menu_btn = page.locator(menu_sel).first
        menu_btn.wait_for(state="visible", timeout=45_000)
        menu_btn.click(timeout=15_000)
        menu_opened = True
        page.wait_for_timeout(open_ms)

        if sign_in_custom:
            sign_loc = page.locator(sign_in_custom).first
        else:
            sign_loc = page.get_by_text(sign_in_text, exact=True).first

        try:
            sign_loc.wait_for(state="visible", timeout=sign_timeout)
        except Exception:
            return

        sign_loc.click(timeout=15_000)
        menu_opened = False
        page.wait_for_timeout(after_sign_ms)

        if iframe_sel:
            fl = page.frame_locator(iframe_sel)
            if login_method_sel:
                method_loc = fl.locator(login_method_sel).first
                method_loc.wait_for(state="visible", timeout=method_timeout)
                method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    page.wait_for_timeout(after_method_ms)
            elif login_method_text:
                method_loc = fl.get_by_text(login_method_text, exact=True).first
                method_loc.wait_for(state="visible", timeout=method_timeout)
                method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    page.wait_for_timeout(after_method_ms)
            email_loc = fl.locator(email_sel).first
            pass_loc = fl.locator(pass_sel).first
            sub_loc = fl.locator(submit_sel).first
        else:
            if login_method_sel:
                method_loc = page.locator(login_method_sel).first
                method_loc.wait_for(state="visible", timeout=method_timeout)
                method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    page.wait_for_timeout(after_method_ms)
            elif login_method_text:
                method_loc = page.get_by_text(login_method_text, exact=True).first
                method_loc.wait_for(state="visible", timeout=method_timeout)
                method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    page.wait_for_timeout(after_method_ms)
            email_loc = page.locator(email_sel).first
            pass_loc = page.locator(pass_sel).first
            sub_loc = page.locator(submit_sel).first

        email_loc.wait_for(state="visible", timeout=45_000)
        email_loc.fill(email, timeout=15_000)
        pass_loc.fill(password, timeout=15_000)
        sub_loc.click(timeout=15_000)

        page.locator(chart_ready_sel).first.wait_for(
            state="visible",
            timeout=chart_ready_timeout_ms,
        )
        if post_submit_ms > 0:
            page.wait_for_timeout(post_submit_ms)

    finally:
        if menu_opened:
            try:
                page.locator(menu_sel).first.click(timeout=10_000)
                page.wait_for_timeout(open_ms)
            except Exception:
                pass


def _tradingview_interval_slug(label: str, tv: dict[str, Any]) -> str:
    overrides = tv.get("interval_filename_slugs")
    if isinstance(overrides, dict) and label in overrides:
        return str(overrides[label]).strip() or "interval"
    defaults: dict[str, str] = {
        "4 giờ": "4h",
        "1 giờ": "1h",
        "15 phút": "15m",
        "5 phút": "5m",
    }
    if label in defaults:
        return defaults[label]
    slug = re.sub(r"[^\w]+", "_", label.strip()).strip("_")[:40]
    return slug or "interval"


def _tradingview_parse_capture_plan(tv: dict[str, Any]) -> Optional[list[tuple[str, list[str]]]]:
    raw = tv.get("capture_plan")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[tuple[str, list[str]]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip()
        intervals = row.get("intervals") or row.get("intervals_aria")
        if not sym or not isinstance(intervals, list):
            continue
        labels = [str(x).strip() for x in intervals if str(x).strip()]
        if labels:
            out.append((sym, labels))
    return out or None


def _tradingview_parse_capture_plan_v2(
    tv: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    """
    Browser-only capture plan that can carry per-interval metadata.

    Format:
      tradingview_capture.capture_plan:
        - symbol: XAUUSD
          intervals:
            - "15 phút"
            - label: "15 phút"
              slug: "15m_ict"
              indicator_profile: "ict_killzones"
            - "5 phút"

    Backward compatible with the old list-of-strings intervals.
    """
    raw = tv.get("capture_plan")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip()
        intervals = row.get("intervals") or row.get("intervals_aria")
        if not sym or not isinstance(intervals, list):
            continue
        items: list[dict[str, Any]] = []
        for it in intervals:
            if isinstance(it, str) and it.strip():
                items.append({"label": it.strip()})
            elif isinstance(it, dict):
                label = str(it.get("label") or it.get("aria") or it.get("aria_label") or "").strip()
                if not label:
                    continue
                d = {"label": label}
                slug = str(it.get("slug") or "").strip()
                prof = str(it.get("indicator_profile") or "").strip()
                if slug:
                    d["slug"] = slug
                if prof:
                    d["indicator_profile"] = prof
                items.append(d)
        if items:
            out.append({"symbol": sym, "intervals": items})
    return out or None


def _tradingview_default_capture_plan() -> list[tuple[str, list[str]]]:
    return [
        ("DXY", ["4 giờ", "1 giờ", "15 phút"]),
        ("XAUUSD", ["4 giờ", "1 giờ", "15 phút", "5 phút"]),
    ]


def _tradingview_resolve_capture_plan(tv: dict[str, Any]) -> Optional[list[tuple[str, list[str]]]]:
    if not tv.get("multi_shot_enabled", True):
        return None
    parsed = _tradingview_parse_capture_plan(tv)
    if parsed:
        return parsed
    return _tradingview_default_capture_plan()


def _tradingview_resolve_capture_plan_v2(tv: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """Browser-only multi-shot plan (v2)."""
    if not tv.get("multi_shot_enabled", True):
        return None
    parsed = _tradingview_parse_capture_plan_v2(tv)
    if parsed:
        return parsed
    # Fallback: derive from v1 default
    base = _tradingview_default_capture_plan()
    return [{"symbol": s, "intervals": [{"label": x} for x in labels]} for s, labels in base]


def _tradingview_ensure_watchlist_open(page, tv: dict[str, Any]) -> None:
    primary = (tv.get("watchlist_button_aria_label") or "").strip()
    if not primary:
        primary = "Danh sách theo dõi, thông tin chi tiết và tin tức"
    fallback = (tv.get("watchlist_button_aria_label_fallback") or "").strip()
    if not fallback:
        fallback = "Watchlist, details, and news"
    ms = int(tv.get("watchlist_open_ms", 500))
    if primary == fallback:
        btn = page.locator(f'button[aria-label="{primary}"]').first
    else:
        btn = page.locator(
            f'button[aria-label="{primary}"], button[aria-label="{fallback}"]'
        ).first
    btn.wait_for(state="visible", timeout=30_000)
    pressed = (btn.get_attribute("aria-pressed") or "").lower()
    if pressed != "true":
        btn.click(timeout=15_000)
        page.wait_for_timeout(ms)


def _tradingview_select_symbol(page, tv: dict[str, Any], symbol: str) -> None:
    custom = (tv.get("symbol_list_item_selector") or "").strip()
    if custom:
        loc = page.locator(custom.format(symbol=symbol)).first
    else:
        prefix = (tv.get("symbol_name_class_prefix") or "symbolNameText-").strip()
        loc = page.locator(f'[class*="{prefix}"]').get_by_text(symbol, exact=True).first
    loc.wait_for(state="visible", timeout=25_000)
    loc.click(timeout=15_000)
    page.wait_for_timeout(int(tv.get("after_symbol_select_ms", 1_500)))


def _tradingview_select_interval(
    page,
    toolbar,
    tv: dict[str, Any],
    interval_aria: str,
    settle_ms: int,
) -> None:
    interval_btn = toolbar.locator(f'button[aria-label="{interval_aria}"]').first
    interval_btn.wait_for(state="attached", timeout=30_000)
    use_force = bool(tv.get("interval_button_click_force", True))
    interval_btn.click(timeout=15_000, force=use_force)
    page.wait_for_timeout(int(tv.get("after_interval_select_ms", settle_ms)))


def _tradingview_reset_chart_position(page, tv: dict[str, Any]) -> None:
    """
    Reset chart position before capturing screenshots.

    Default shortcut: Alt+R (user workflow). Can override via
    ``tradingview_reset_shortcut`` (e.g. "Alt+R") and wait via
    ``after_tradingview_reset_ms``.
    """
    shortcut = (tv.get("tradingview_reset_shortcut") or "Alt+R").strip()
    wait_ms = int(tv.get("after_tradingview_reset_ms", 400))
    if not shortcut:
        return
    try:
        page.keyboard.press(shortcut)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
    except Exception:
        # Best-effort: reset is helpful but should not fail capture.
        pass


def _tv_required_indicator_groups(tv: dict[str, Any]) -> list[list[str]]:
    """
    Return required indicator groups (aliases per indicator) to match legend-source-item text.

    Preferred config (extensible):
      tradingview_capture.required_indicators:
        - aliases: ["VSA Wyckoff Volume", "VSA Volume"]
          favorite_name: "VSA Volume"  # optional (used for recovery if favorite_indicator_names omitted)
        - aliases: ["LuxAlgo - Smart Money Concepts", "Smart Money Concepts (SMC) [LuxAlgo]"]
          favorite_name: "Smart Money Concepts (SMC) [LuxAlgo]"

    Backward compatible with:
      tradingview_capture.required_indicators_aliases: {key: [aliases...]}
    """
    raw = tv.get("required_indicators")
    if isinstance(raw, list) and raw:
        groups: list[list[str]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            aliases = row.get("aliases")
            if isinstance(aliases, list):
                names = [str(x).strip() for x in aliases if str(x).strip()]
            else:
                names = [str(aliases).strip()] if str(aliases).strip() else []
            if names:
                groups.append(names)
        if groups:
            return groups

    # Back-compat: dict of key -> aliases
    raw2 = tv.get("required_indicators_aliases")
    if isinstance(raw2, dict) and raw2:
        groups2: list[list[str]] = []
        for _k, v in raw2.items():
            if isinstance(v, list):
                names = [str(x).strip() for x in v if str(x).strip()]
            else:
                names = [str(v).strip()] if str(v).strip() else []
            if names:
                groups2.append(names)
        if groups2:
            return groups2

    return [
        ["VSA Wyckoff Volume", "VSA Volume"],
        ["LuxAlgo - Smart Money Concepts", "Smart Money Concepts (SMC) [LuxAlgo]"],
    ]


def _tv_apply_indicator_profile(tv: dict[str, Any], profile: str) -> dict[str, Any]:
    """
    Return a shallow-copied tv dict with overrides from `indicator_profiles[profile]`.
    """
    p = (profile or "").strip()
    if not p:
        return tv
    profiles = tv.get("indicator_profiles")
    if not isinstance(profiles, dict):
        return tv
    ov = profiles.get(p)
    if not isinstance(ov, dict) or not ov:
        return tv
    merged = dict(tv)
    merged.update(ov)
    return merged


def _tradingview_list_legend_item_texts(page, tv: dict[str, Any]) -> list[str]:
    sel = (tv.get("legend_item_selector") or '[data-qa-id="legend-source-item"]').strip()
    loc = page.locator(sel)
    n = loc.count()
    out: list[str] = []
    for i in range(int(n or 0)):
        try:
            t = (loc.nth(i).inner_text(timeout=1500) or "").strip()
        except Exception:
            t = ""
        if t:
            out.append(t)
    return out


def _tradingview_has_required_indicators(page, tv: dict[str, Any]) -> bool:
    groups = _tv_required_indicator_groups(tv)
    texts = _tradingview_list_legend_item_texts(page, tv)
    hay = "\n".join(texts).lower()

    def _has_any(names: list[str]) -> bool:
        for nm in names:
            if nm and nm.lower() in hay:
                return True
        return False

    # Require every indicator group to be present.
    if not groups:
        return True
    for g in groups:
        if not _has_any(list(g or [])):
            return False
    return True


def _tradingview_chart_center_xy(page, tv: dict[str, Any]) -> tuple[float, float]:
    """
    Compute a reliable point near the chart center for context-click.
    Falls back to viewport center if bounding box is unavailable.
    """
    raw = tv.get("chart_center_click_selector")
    sels: list[str] = []
    if isinstance(raw, str) and raw.strip():
        sels.append(raw.strip())
    elif isinstance(raw, list):
        sels.extend([str(x).strip() for x in raw if str(x).strip()])
    # Fallbacks (best-effort; TradingView DOM can vary).
    sels.extend(
        [
            'div[data-name="pane"]',
            '[data-qa-id="chart-container"]',
            "div.chart-container",
            "div.tv-chart",
        ]
    )
    for sel in sels:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            bb = loc.bounding_box()
            if bb and bb.get("width", 0) and bb.get("height", 0):
                x = float(bb["x"]) + float(bb["width"]) / 2.0
                y = float(bb["y"]) + float(bb["height"]) / 2.0
                return x, y
        except Exception:
            continue
    vp = page.viewport_size or {"width": 1600, "height": 900}
    return float(vp["width"]) / 2.0, float(vp["height"]) / 2.0


def _tradingview_open_context_menu_and_clear_indicators(page, tv: dict[str, Any]) -> None:
    texts = tv.get("context_menu_delete_indicators_texts")
    if isinstance(texts, list) and texts:
        candidates = [str(x).strip() for x in texts if str(x).strip()]
    else:
        candidates = ["Xóa 1 chỉ báo", "Xóa 2 chỉ báo"]

    x, y = _tradingview_chart_center_xy(page, tv)
    page.mouse.click(x, y, button="right")

    # Try to click the first visible matching menu item.
    for t in candidates:
        try:
            item = page.locator(
                'tr[data-role="menuitem"] [data-label="true"]',
                has_text=t,
            ).first
            item.wait_for(state="visible", timeout=1500)
            item.click(timeout=3000)
            break
        except Exception:
            continue

    page.wait_for_timeout(int(tv.get("after_indicator_clear_ms", 450)))


def _tradingview_add_required_indicators_from_favorites(page, tv: dict[str, Any]) -> None:
    btn_sel = (tv.get("favorite_indicators_button_selector") or 'button[data-name="show-favorite-indicators"]').strip()
    tpl = (tv.get("favorite_indicator_item_selector_template") or 'div[data-role="menuitem"][aria-label="{name}"]').strip()
    names = tv.get("favorite_indicator_names")
    if isinstance(names, list) and names:
        favs = [str(x).strip() for x in names if str(x).strip()]
    else:
        # If favorite_indicator_names isn't configured, infer from required_indicators[].favorite_name.
        inferred: list[str] = []
        raw_req = tv.get("required_indicators")
        if isinstance(raw_req, list):
            for row in raw_req:
                if not isinstance(row, dict):
                    continue
                fn = str(row.get("favorite_name") or "").strip()
                if fn:
                    inferred.append(fn)
        favs = inferred or ["Smart Money Concepts (SMC) [LuxAlgo]", "VSA Volume"]

    btn = page.locator(btn_sel).first
    # Some TradingView layouts keep the button in DOM but "hidden" (toolbar overflow / responsive).
    # Try a few best-effort click strategies (including clicking inside the button).
    try:
        btn.wait_for(state="visible", timeout=4000)
        btn.click(timeout=10_000)
    except Exception:
        try:
            btn.click(timeout=10_000, force=True)
        except Exception:
            # Click a child element inside the button (user reported this can be hit-testable).
            child = btn.locator(":scope div").first
            child.wait_for(state="attached", timeout=10_000)
            child.click(timeout=10_000, force=True)

    after_add = int(tv.get("after_indicator_add_ms", 500))
    for nm in favs:
        sel = tpl.format(name=nm)
        it = page.locator(sel).first
        it.wait_for(state="visible", timeout=10_000)
        it.click(timeout=10_000)
        if after_add > 0:
            page.wait_for_timeout(after_add)

    # Close menu (best-effort) by clicking the button again.
    try:
        page.locator(btn_sel).first.click(timeout=2000)
    except Exception:
        pass


def _tradingview_ensure_required_indicators(page, tv: dict[str, Any]) -> None:
    if not bool(tv.get("required_indicators_enabled", False)):
        return

    verify_timeout_ms = int(tv.get("indicator_verify_timeout_ms", 6000))
    deadline = time.monotonic() + max(0, verify_timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        if _tradingview_has_required_indicators(page, tv):
            return
        page.wait_for_timeout(200)

    # Recover: clear, add from favorites, then verify again.
    _tradingview_open_context_menu_and_clear_indicators(page, tv)
    _tradingview_add_required_indicators_from_favorites(page, tv)

    deadline2 = time.monotonic() + max(0, verify_timeout_ms) / 1000.0
    while time.monotonic() < deadline2:
        if _tradingview_has_required_indicators(page, tv):
            return
        page.wait_for_timeout(200)

    got = _tradingview_list_legend_item_texts(page, tv)
    raise SystemExit(
        "TradingView required indicators missing after recovery. "
        f"Expected VSA+SMC, got legend items: {got!r}"
    )


def _tradingview_snapshot_url_capture(
    page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_url_path: Optional[Path] = None,
) -> Path:
    """
    Toolbar screenshot → "Open image in new tab" → read ``img.tv-snapshot-image`` src.
    Writes ``{{stamp}}_tradingview_{{symbol}}_{{interval}}.url`` (one line, https) for OpenAI.
    If src is not http(s), falls back to PNG via element screenshot on the snapshot tab.
    """
    shot_sel = (tv.get("screenshot_button_selector") or "#header-toolbar-screenshot").strip()
    open_sel = (tv.get("snapshot_open_in_new_tab_selector") or '[data-qa-id="open-image-in-new-tab"]').strip()
    img_sel = (tv.get("snapshot_image_selector") or "img.tv-snapshot-image").strip()
    after_shot_ms = int(tv.get("after_screenshot_button_ms", 600))
    tab_timeout = int(tv.get("snapshot_new_tab_timeout_ms", 45_000))
    tab_settle_ms = int(tv.get("snapshot_tab_settle_ms", 1000))
    after_esc_ms = int(tv.get("after_snapshot_escape_ms", 500))

    page.locator(shot_sel).first.wait_for(state="visible", timeout=45_000)
    page.locator(shot_sel).first.click(timeout=15_000)
    page.wait_for_timeout(after_shot_ms)

    open_btn = page.locator(open_sel).first
    open_btn.wait_for(state="visible", timeout=20_000)

    context = page.context
    with context.expect_page(timeout=tab_timeout) as new_page_info:
        open_btn.click(timeout=15_000)
    snap_page = new_page_info.value
    dest_base = charts_dir / f"{stamp}_tradingview_{symbol_key}_{interval_slug}"
    out_url_path = dest_url_path or dest_base.with_suffix(".url")
    out_png_path = dest_base.with_suffix(".png")
    try:
        snap_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        snap_page.wait_for_timeout(tab_settle_ms)
        loc = snap_page.locator(img_sel).first
        loc.wait_for(state="visible", timeout=25_000)
        src = (loc.get_attribute("src") or "").strip()
        if src.startswith("https://") or src.startswith("http://"):
            out_url_path.parent.mkdir(parents=True, exist_ok=True)
            out_url_path.write_text(src + "\n", encoding="utf-8")
            return out_url_path
        # blob: or missing: PNG snapshot of the image (OpenAI cannot fetch blob URLs)
        loc.screenshot(path=str(out_png_path), timeout=30_000)
        return out_png_path
    finally:
        try:
            snap_page.close()
        except Exception:
            pass
        page.keyboard.press("Escape")
        page.wait_for_timeout(after_esc_ms)


def _tradingview_capture_one_chart_frame(
    page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_path: Optional[Path] = None,
    dest_url_path: Optional[Path] = None,
) -> Path:
    """One TradingView frame: snapshot-URL flow (default) or legacy fullscreen PNG."""
    if bool(tv.get("tradingview_snapshot_url_flow", True)):
        return _tradingview_snapshot_url_capture(
            page,
            tv,
            charts_dir,
            stamp,
            symbol_key,
            interval_slug,
            dest_url_path=dest_url_path,
        )
    return _tradingview_fullscreen_screenshot_then_escape(
        page,
        tv,
        charts_dir,
        stamp,
        symbol_key,
        interval_slug,
        dest_path=dest_path,
    )


def _tradingview_fullscreen_screenshot_then_escape(
    page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_path: Optional[Path] = None,
) -> Path:
    fs_sel = (tv.get("fullscreen_button_selector") or "#header-toolbar-fullscreen").strip()
    page.locator(fs_sel).first.wait_for(state="visible", timeout=45_000)
    page.locator(fs_sel).first.click(timeout=15_000)
    _wait_tradingview_fullscreen_notice_gone(page, tv)
    fs_wait = int(tv.get("fullscreen_settle_ms", 2000))
    page.wait_for_timeout(fs_wait)
    full_page = bool(tv.get("fullscreen_screenshot_full_page", True))
    dest = dest_path or (charts_dir / f"{stamp}_tradingview_{symbol_key}_{interval_slug}.png")
    page.screenshot(path=str(dest), full_page=full_page)
    page.keyboard.press("Escape")
    page.wait_for_timeout(int(tv.get("after_fullscreen_escape_ms", 800)))
    return dest


def _run_tradingview_multi_shot_flow(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    tv: dict[str, Any],
    plan: list[tuple[str, list[str]]],
) -> list[Path]:
    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip()
    toolbar = page.locator(f"#{intervals_id}")
    written: list[Path] = []
    for symbol, intervals in plan:
        _tradingview_ensure_watchlist_open(page, tv)
        _tradingview_select_symbol(page, tv, symbol)
        toolbar.wait_for(state="visible", timeout=60_000)
        sym_key = re.sub(r"[^\w.-]+", "_", symbol).strip("_")[:40] or "sym"
        for aria in intervals:
            slug = _tradingview_interval_slug(aria, tv)
            _tradingview_select_interval(page, toolbar, tv, aria, settle_ms)
            _tradingview_reset_chart_position(page, tv)
            _tradingview_ensure_required_indicators(page, tv)
            path = _tradingview_capture_one_chart_frame(
                page, tv, charts_dir, stamp, sym_key, slug
            )
            written.append(path)
    return written


def _run_tradingview_multi_shot_flow_v2(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    tv: dict[str, Any],
    plan: list[dict[str, Any]],
) -> list[Path]:
    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip()
    toolbar = page.locator(f"#{intervals_id}")
    written: list[Path] = []
    for row in plan:
        symbol = str(row.get("symbol") or "").strip()
        intervals = row.get("intervals")
        if not symbol or not isinstance(intervals, list):
            continue
        _tradingview_ensure_watchlist_open(page, tv)
        _tradingview_select_symbol(page, tv, symbol)
        toolbar.wait_for(state="visible", timeout=60_000)
        sym_key = re.sub(r"[^\w.-]+", "_", symbol).strip("_")[:40] or "sym"
        for it in intervals:
            if not isinstance(it, dict):
                continue
            label = str(it.get("label") or "").strip()
            if not label:
                continue
            slug = str(it.get("slug") or "").strip() or _tradingview_interval_slug(label, tv)
            prof = str(it.get("indicator_profile") or "").strip()
            tv_eff = _tv_apply_indicator_profile(tv, prof)
            _tradingview_select_interval(page, toolbar, tv, label, settle_ms)
            _tradingview_reset_chart_position(page, tv)
            _tradingview_ensure_required_indicators(page, tv_eff)
            path = _tradingview_capture_one_chart_frame(
                page, tv_eff, charts_dir, stamp, sym_key, slug
            )
            written.append(path)
    return written


def _wait_tradingview_fullscreen_notice_gone(page, tv: dict[str, Any]) -> None:
    """After TV fullscreen, wait for the 'panels hidden' toast container to disappear before screenshot."""
    notice_sel = (tv.get("tradingview_fullscreen_notice_selector") or "").strip()
    if not notice_sel:
        notice_sel = 'div[class*="container-default-"][class*="notice-"]'
    if tv.get("tradingview_fullscreen_notice_wait_disabled", False):
        return
    loc = page.locator(notice_sel).first
    visible_ms = int(tv.get("tradingview_fullscreen_notice_visible_timeout_ms", 10_000))
    hide_ms = int(tv.get("tradingview_fullscreen_notice_hide_timeout_ms", 45_000))
    try:
        loc.wait_for(state="visible", timeout=visible_ms)
    except Exception:
        pass
    try:
        loc.wait_for(state="hidden", timeout=hide_ms)
    except Exception:
        pass


def _run_tradingview_screenshot_flow(
    page,
    *,
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    tv: dict[str, Any],
    coinmap_email: Optional[str] = None,
    tradingview_password: Optional[str] = None,
) -> list[Path]:
    """
    Open TradingView: optional login, dark mode, then either multi-shot (watchlist →
    symbol → interval → snapshot toolbar → open image in new tab → save ``.url``, per
    capture_plan) or legacy single frame. Set ``tradingview_snapshot_url_flow: false``
    for fullscreen + Playwright PNG instead.
    """
    tw = int(tv.get("viewport_width", 0) or 0)
    th = int(tv.get("viewport_height", 0) or 0)
    if tw > 0 and th > 0:
        page.set_viewport_size({"width": tw, "height": th})

    url = tv.get("chart_url") or "https://vn.tradingview.com/chart/?symbol=OANDA%3AXAUUSD"
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    init_wait = int(tv.get("initial_settle_ms", settle_ms))
    page.wait_for_timeout(init_wait)

    _maybe_tradingview_login(page, tv, coinmap_email, tradingview_password)

    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip()
    toolbar = page.locator(f"#{intervals_id}")
    toolbar.wait_for(state="visible", timeout=90_000)

    _maybe_tradingview_dark_mode(page, tv)

    plan_v2 = _tradingview_resolve_capture_plan_v2(tv)
    if plan_v2:
        return _run_tradingview_multi_shot_flow_v2(
            page,
            charts_dir=charts_dir,
            stamp=stamp,
            settle_ms=settle_ms,
            tv=tv,
            plan=plan_v2,
        )

    interval_aria = (tv.get("interval_button_aria_label") or "15 phút").strip()
    interval_btn = toolbar.locator(f'button[aria-label="{interval_aria}"]').first
    interval_btn.wait_for(state="attached", timeout=30_000)
    use_force = bool(tv.get("interval_button_click_force", True))
    interval_btn.click(timeout=15_000, force=use_force)
    page.wait_for_timeout(int(tv.get("after_interval_select_ms", settle_ms)))

    legacy_png = charts_dir / f"{stamp}_tradingview_fullscreen.png"
    legacy_url = charts_dir / f"{stamp}_tradingview_fullscreen.url"
    if bool(tv.get("tradingview_snapshot_url_flow", True)):
        _tradingview_reset_chart_position(page, tv)
        _tradingview_ensure_required_indicators(page, tv)
        dest = _tradingview_capture_one_chart_frame(
            page,
            tv,
            charts_dir,
            stamp,
            "fullscreen",
            "single",
            dest_url_path=legacy_url,
        )
    else:
        _tradingview_reset_chart_position(page, tv)
        _tradingview_ensure_required_indicators(page, tv)
        dest = _tradingview_capture_one_chart_frame(
            page,
            tv,
            charts_dir,
            stamp,
            "fullscreen",
            "single",
            dest_path=legacy_png,
        )
    return [dest]


def _capture_charts_in_context(
    context: BrowserContext,
    *,
    cfg: dict[str, Any],
    charts_dir: Path,
    storage_state_path: Optional[Path],
    email: Optional[str],
    password: Optional[str],
    tradingview_password: Optional[str],
    save_storage_state: bool,
    stamp: str,
    tradingview_force_screenshot: bool = False,
    progress_hook: Optional[Callable[[], None]] = None,
    coinmap_only_retry_paths: Optional[list[Path]] = None,
    coinmap_capture_intervals: Optional[Sequence[str]] = None,
) -> list[Path]:
    """
    Run Coinmap (+ optional TradingView) capture using an existing browser context.
    Opens a dedicated page for Coinmap and closes it before return; does not close ``context``.
    """
    login_url = cfg.get("login_url") or "https://coinmap.tech/login"
    post_wait = (cfg.get("post_login_wait_selector") or "").strip()
    chart_selectors: list[str] = list(cfg.get("chart_selectors") or ["canvas"])
    fallback_full = bool(cfg.get("fallback_full_page", True))
    settle_ms = int(cfg.get("settle_ms", 2000))
    max_charts = int(cfg.get("max_charts", 0))

    email_sel = cfg.get("email_selector") or 'input[type="email"]'
    password_sel = cfg.get("password_selector") or 'input[type="password"]'
    submit_sel = cfg.get("submit_selector") or 'button[type="submit"]'

    written: list[Path] = []
    logs_dir = default_logs_dir()
    cd = cfg.get("chart_download") or {}
    if not isinstance(cd, dict):
        cd = {}
    coinmap_bearer_satisfied_from_cache = False
    api_cd_cache = _api_export_config(cd) if cd.get("enabled", False) else None
    if (
        api_cd_cache is not None
        and _api_export_mode(api_cd_cache) == "bearer_request"
        and api_cd_cache.get("bearer_skip_chart_ui") is not False
        and _coinmap_bearer_token_cache_enabled(api_cd_cache)
    ):
        cache_p = _coinmap_bearer_token_cache_resolved_path(api_cd_cache)
        if cache_p.is_file():
            cached_auth = _coinmap_read_bearer_token_cache(cache_p)
            if cached_auth:
                try:
                    cache_paths = _coinmap_run_bearer_api_only_exports(
                        None,
                        charts_dir=charts_dir,
                        stamp=stamp,
                        cd=cd,
                        api_cd=api_cd_cache,
                        auth=cached_auth,
                        only_retry_paths=coinmap_only_retry_paths,
                        prefer_httpx=True,
                        abort_on_http_auth_error=True,
                        coinmap_capture_intervals=coinmap_capture_intervals,
                    )
                    written.extend(cache_paths)
                    coinmap_bearer_satisfied_from_cache = True
                    _coinmap_bearer_log(
                        api_cd_cache,
                        "Bearer token cache hit — skipped Coinmap login/chart navigation for API-only export",
                    )
                except CoinmapBearerCacheInvalid:
                    try:
                        cache_p.unlink(missing_ok=True)
                    except OSError:
                        pass
                    _coinmap_bearer_log(
                        api_cd_cache,
                        "Bearer token cache rejected (401/403); refreshing via browser",
                    )
                except SystemExit:
                    raise
                except Exception as e:
                    _coinmap_bearer_log(
                        api_cd_cache,
                        f"Bearer token cache probe failed ({e!r}); falling back to browser",
                    )

    page = context.new_page()
    bearer_capture: Optional[CoinmapBearerCapture] = None
    cd_pre = cfg.get("chart_download")
    if (
        not coinmap_bearer_satisfied_from_cache
        and isinstance(cd_pre, dict)
        and cd_pre.get("enabled", False)
    ):
        api_pre = _api_export_config(cd_pre)
        if api_pre is not None and _api_export_mode(api_pre) == "bearer_request":
            bearer_capture = CoinmapBearerCapture(page, api_pre)
            bearer_capture.install()
    try:
        if progress_hook is not None:
            progress_hook()
        page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(settle_ms)
        if progress_hook is not None:
            progress_hook()

        if email and password:
            if _login_form_is_visible(page, email_sel, password_sel):
                page.locator(email_sel).first.fill(email, timeout=15_000)
                page.locator(password_sel).first.fill(password, timeout=15_000)
                page.locator(submit_sel).first.click(timeout=15_000)
                page.wait_for_load_state("networkidle", timeout=60_000)
                page.wait_for_timeout(settle_ms)
                if progress_hook is not None:
                    progress_hook()
            else:
                print(
                    "Already logged in (no login form visible); skipping credential submit.",
                    flush=True,
                )

        if post_wait:
            try:
                page.locator(post_wait).first.wait_for(state="visible", timeout=30_000)
            except Exception:
                pass

        if cd.get("enabled", False) and not coinmap_bearer_satisfied_from_cache:
            try:
                if progress_hook is not None:
                    progress_hook()
                dl_paths = _run_chart_screenshot_flow(
                    page,
                    charts_dir=charts_dir,
                    stamp=stamp,
                    settle_ms=settle_ms,
                    cd=cd,
                    coinmap_email=email,
                    coinmap_password=password,
                    coinmap_login_cfg=cfg,
                    bearer_capture=bearer_capture,
                    coinmap_bearer_only_retry_paths=coinmap_only_retry_paths,
                    coinmap_capture_intervals=coinmap_capture_intervals,
                )
                written.extend(dl_paths)
                if progress_hook is not None:
                    progress_hook()
            except Exception as e:
                snap = _maybe_save_capture_error_screenshot(
                    page, logs_dir=logs_dir, stamp=stamp, label="coinmap_chart_download"
                )
                hint = f" Failure screenshot: {snap}" if snap else ""
                raise SystemExit(
                    "chart_download flow failed. Check selectors in config/coinmap.yaml "
                    f"(multi_shot sidebar/watchlist/interval or single fullscreen). Error: {e}.{hint}"
                ) from e

        if coinmap_only_retry_paths:
            if save_storage_state and storage_state_path:
                _ensure_dir(storage_state_path.parent)
                context.storage_state(path=str(storage_state_path))
            return written

        tv = cfg.get("tradingview_capture") or {}
        if isinstance(tv, dict) and tv.get("enabled", False):
            tv_ds = str(tv.get("data_source") or "browser").strip().lower()
            if tradingview_force_screenshot and tv_ds == "tvdatafeed":
                tv_ds = "browser"
            if tv_ds == "tvdatafeed":
                from automation_tool.tvdatafeed_capture import run_tvdatafeed_export

                try:
                    if progress_hook is not None:
                        progress_hook()
                    tv_paths = run_tvdatafeed_export(
                        tv=tv,
                        charts_dir=charts_dir,
                        stamp=stamp,
                        tradingview_username=email,
                        tradingview_password=tradingview_password,
                    )
                    written.extend(tv_paths)
                    if progress_hook is not None:
                        progress_hook()
                except Exception as e:
                    raise SystemExit(
                        "tradingview_capture (tvdatafeed) failed. Check config/coinmap.yaml "
                        f"tradingview_capture.tvdatafeed (exchange, symbol_exchanges, interval_map). Error: {e}"
                    ) from e
            else:
                tv_page = context.new_page()
                try:
                    if progress_hook is not None:
                        progress_hook()
                    tv_paths = _run_tradingview_screenshot_flow(
                        tv_page,
                        charts_dir=charts_dir,
                        stamp=stamp,
                        settle_ms=settle_ms,
                        tv=tv,
                        coinmap_email=email,
                        tradingview_password=tradingview_password,
                    )
                    written.extend(tv_paths)
                    if progress_hook is not None:
                        progress_hook()
                except Exception as e:
                    snap = _maybe_save_capture_error_screenshot(
                        tv_page, logs_dir=logs_dir, stamp=stamp, label="tradingview_capture"
                    )
                    hint = f" Failure screenshot: {snap}" if snap else ""
                    raise SystemExit(
                        "tradingview_capture failed. Check config/coinmap.yaml "
                        f"(capture_plan, watchlist, symbols, intervals, fullscreen). Error: {e}.{hint}"
                    ) from e
                finally:
                    tv_page.close()

        screenshot_after = bool(cfg.get("screenshot_after_chart_download", True))
        skip_canvas = bool(cd.get("enabled")) and not screenshot_after
        if coinmap_bearer_satisfied_from_cache:
            # Chart page was never opened; avoid canvas/fullpage shots of the wrong view.
            skip_canvas = True

        if not skip_canvas:
            idx = 0
            for sel in chart_selectors:
                if max_charts and idx >= max_charts:
                    break
                locs = page.locator(sel)
                n = locs.count()
                for i in range(n):
                    if max_charts and idx >= max_charts:
                        break
                    path = charts_dir / f"{stamp}_chart_{idx:03d}.png"
                    try:
                        locs.nth(i).screenshot(path=str(path), timeout=20_000)
                        written.append(path)
                        idx += 1
                        if progress_hook is not None:
                            progress_hook()
                    except Exception:
                        continue

            if not written and fallback_full:
                path = charts_dir / f"{stamp}_fullpage.png"
                page.screenshot(path=str(path), full_page=True)
                written.append(path)
                if progress_hook is not None:
                    progress_hook()

        if save_storage_state and storage_state_path:
            _ensure_dir(storage_state_path.parent)
            context.storage_state(path=str(storage_state_path))
    finally:
        if bearer_capture is not None:
            try:
                bearer_capture.uninstall()
            except Exception:
                pass
        try:
            page.close()
        except Exception:
            pass

    if isinstance(cd, dict) and cd.get("enabled", False) and _api_export_config(cd) is not None:
        try:
            from automation_tool.coinmap_merged import run_coinmap_merged_writes

            merged_paths = run_coinmap_merged_writes(charts_dir, stamp)
            for p in merged_paths.values():
                written.append(p)
        except Exception as e:
            logging.getLogger(__name__).warning("coinmap_merged_after_capture: %s", e)

    return written


def capture_charts(
    *,
    coinmap_yaml: Path,
    charts_dir: Optional[Path] = None,
    storage_state_path: Optional[Path],
    email: Optional[str],
    password: Optional[str],
    tradingview_password: Optional[str] = None,
    save_storage_state: bool = True,
    headless: bool = True,
    reuse_browser_context: Optional[BrowserContext] = None,
    main_chart_symbol: Optional[str] = None,
    set_global_active_symbol: bool = True,
    enable_coinmap: Optional[bool] = None,
    enable_tradingview: Optional[bool] = None,
    clear_charts_before_capture: Optional[bool] = None,
    stamp_override: Optional[str] = None,
    progress_hook: Optional[Callable[[], None]] = None,
    require_browser_service: bool = False,
    coinmap_only_retry_paths: Optional[list[Path]] = None,
    coinmap_capture_intervals: Optional[Sequence[str]] = None,
    tradingview_force_screenshot: bool = False,
) -> list[Path]:
    """
    Optionally clear prior images in charts_dir, then log in (if credentials given),
    optionally run chart screenshot flow (see config chart_download), then optionally
    screenshot canvas elements. Saves files under charts_dir.

    If ``reuse_browser_context`` is provided (e.g. ``tv-journal-monitor`` already runs
    inside ``sync_playwright``), capture uses that context instead of starting a nested
    Playwright sync session.

    If ``require_browser_service`` is True, attaches via CDP to the running browser service
    with ``force=True`` and raises if attach fails (no local Chrome launch).

    If ``coinmap_capture_intervals`` is set (e.g. ``("5m",)``), only those Coinmap
    multi-shot / bearer API steps run; other intervals in the YAML are skipped.
    """
    from automation_tool.config import default_charts_dir
    from automation_tool.images import (
        get_active_main_symbol,
        set_active_main_symbol_file,
        normalize_main_chart_symbol,
        write_main_chart_symbol_marker,
    )

    cfg = load_coinmap_yaml(coinmap_yaml)
    if main_chart_symbol is not None and str(main_chart_symbol).strip():
        cfg = apply_main_chart_symbol_to_config(cfg, main_chart_symbol)

    if clear_charts_before_capture is not None:
        cfg["clear_charts_before_capture"] = bool(clear_charts_before_capture)
    if coinmap_only_retry_paths:
        cfg["clear_charts_before_capture"] = False
        st_ro = (stamp_override or "").strip()
        if not st_ro:
            raise SystemExit(
                "coinmap_only_retry_paths requires stamp_override to match the batch being fixed."
            )
        cd_retry = cfg.get("chart_download") or {}
        if not isinstance(cd_retry, dict) or not cd_retry.get("enabled", False):
            raise SystemExit("coinmap_only_retry_paths requires chart_download.enabled in YAML.")
        api_retry = _api_export_config(cd_retry)
        if api_retry is None or _api_export_mode(api_retry) != "bearer_request":
            raise SystemExit(
                "coinmap_only_retry_paths requires api_data_export.mode bearer_request (API-only flow)."
            )
        if api_retry.get("bearer_skip_chart_ui") is False:
            raise SystemExit(
                "coinmap_only_retry_paths is not supported when bearer_skip_chart_ui is false."
            )

    # For multi-symbol runs: allow disabling one side (Coinmap vs TradingView) per phase.
    # Also disable legacy canvas screenshots (stamp_chart_XXX.png) which are noisy for this workflow.
    if enable_coinmap is not None or enable_tradingview is not None:
        cfg["chart_selectors"] = []
        cfg["fallback_full_page"] = False
        cfg["screenshot_after_chart_download"] = False

    if enable_coinmap is not None:
        cd = cfg.get("chart_download")
        if not isinstance(cd, dict):
            cd = {}
            cfg["chart_download"] = cd
        cd["enabled"] = bool(enable_coinmap)
    if enable_tradingview is not None:
        tv = cfg.get("tradingview_capture")
        if not isinstance(tv, dict):
            tv = {}
            cfg["tradingview_capture"] = tv
        tv["enabled"] = bool(enable_tradingview)

    has_storage = bool(storage_state_path and storage_state_path.exists())
    if bool(email) != bool(password):
        raise SystemExit("Set both COINMAP_EMAIL and COINMAP_PASSWORD, or leave both empty and use storage state.")
    if not email and not password and not has_storage:
        raise SystemExit(
            "Coinmap capture needs COINMAP_EMAIL and COINMAP_PASSWORD in .env, "
            f"or an existing Playwright storage state file (e.g. {storage_state_path})."
        )

    if set_global_active_symbol:
        set_active_main_symbol_file(
            main_chart_symbol if (main_chart_symbol and str(main_chart_symbol).strip()) else None
        )
    charts_dir = charts_dir or default_charts_dir()
    _ensure_dir(charts_dir)
    if main_chart_symbol is not None and str(main_chart_symbol).strip():
        write_main_chart_symbol_marker(charts_dir, normalize_main_chart_symbol(main_chart_symbol))
    else:
        write_main_chart_symbol_marker(charts_dir, get_active_main_symbol())
    if bool(cfg.get("clear_charts_before_capture", True)):
        _clear_charts_dir(charts_dir)
    stamp = (stamp_override or "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")

    if reuse_browser_context is not None:
        return _capture_charts_in_context(
            reuse_browser_context,
            cfg=cfg,
            charts_dir=charts_dir,
            storage_state_path=storage_state_path,
            email=email,
            password=password,
            tradingview_password=tradingview_password,
            save_storage_state=save_storage_state,
            stamp=stamp,
            tradingview_force_screenshot=tradingview_force_screenshot,
            progress_hook=progress_hook,
            coinmap_only_retry_paths=coinmap_only_retry_paths,
            coinmap_capture_intervals=coinmap_capture_intervals,
        )

    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p, force=require_browser_service)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
        elif require_browser_service:
            raise SystemExit(
                "capture requires browser service but could not attach via CDP. "
                "Run: coinmap-automation browser up "
                f"(state file: {browser_service_state_path()})."
            )
        else:
            browser, context = launch_chrome_context(
                p,
                headless=headless,
                storage_state_path=storage_state_path,
                viewport_width=vw,
                viewport_height=vh,
            )
            use_browser_service = False
        try:
            return _capture_charts_in_context(
                context,
                cfg=cfg,
                charts_dir=charts_dir,
                storage_state_path=storage_state_path,
                email=email,
                password=password,
                tradingview_password=tradingview_password,
                save_storage_state=save_storage_state,
                stamp=stamp,
                tradingview_force_screenshot=tradingview_force_screenshot,
                progress_hook=progress_hook,
                coinmap_only_retry_paths=coinmap_only_retry_paths,
                coinmap_capture_intervals=coinmap_capture_intervals,
            )
        finally:
            if use_browser_service:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)


