"""
TradingView OHLC via rongardF/tvdatafeed (no browser).

Writes ``{stamp}_tradingview_{sym}_{interval_slug}.json`` compatible with
:func:`automation_tool.images.ordered_chart_openai_payloads`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from automation_tool.coinmap import (
    _tradingview_interval_slug,
    _tradingview_resolve_capture_plan,
)

_log = logging.getLogger("automation_tool.tvdatafeed_capture")

# tvDatafeed.Interval attribute names (see tvDatafeed.Interval)
_DEFAULT_INTERVAL_MAP: dict[str, str] = {
    "5 phút": "in_5_minute",
    "15 phút": "in_15_minute",
    "30 phút": "in_30_minute",
    "45 phút": "in_45_minute",
    "1 giờ": "in_1_hour",
    "2 giờ": "in_2_hour",
    "3 giờ": "in_3_hour",
    "4 giờ": "in_4_hour",
    "1 ngày": "in_daily",
    "1 tuần": "in_weekly",
    "1 tháng": "in_monthly",
    "5 minutes": "in_5_minute",
    "15 minutes": "in_15_minute",
    "30 minutes": "in_30_minute",
    "45 minutes": "in_45_minute",
    "1 hour": "in_1_hour",
    "2 hours": "in_2_hour",
    "3 hours": "in_3_hour",
    "4 hours": "in_4_hour",
    "1 day": "in_daily",
    "1 week": "in_weekly",
    "1 month": "in_monthly",
}

_thread_local = threading.local()

# Lazily loaded (tvDatafeed may be missing until `pip install -e .`)
_tvdatafeed_classes: tuple[Any, Any] | None = None


def _load_tvdatafeed() -> tuple[Any, Any]:
    """
    Import rongardF/tvdatafeed once. Raises SystemExit with install hints if the subprocess
    (e.g. capture_worker) uses a Python env without the package.
    """
    global _tvdatafeed_classes
    if _tvdatafeed_classes is not None:
        return _tvdatafeed_classes
    try:
        from tvDatafeed import Interval, TvDatafeed
    except ModuleNotFoundError as e:
        raise SystemExit(
            "tvdatafeed is not installed in this Python environment (the same one as "
            "browser_service / capture_worker: see sys.executable).\n"
            "Install:\n"
            "  pip install 'git+https://github.com/rongardF/tvdatafeed.git' 'pandas>=2.0.0'\n"
            "or from the repo root:\n"
            "  pip install -e .\n"
            "Then restart browser_service so workers pick up the venv."
        ) from e
    _tvdatafeed_classes = (TvDatafeed, Interval)
    return _tvdatafeed_classes


def parse_tradingview_chart_url(url: str) -> tuple[str, str]:
    """
    From ``https://...tradingview.com/chart/?symbol=OANDA%3AXAUUSD`` return
    ``("OANDA", "XAUUSD")``. Fallback ``("", "XAUUSD")`` if missing.
    """
    if not url or not str(url).strip():
        return "", "XAUUSD"
    try:
        q = parse_qs(urlparse(url).query)
        raw = (q.get("symbol") or [""])[0]
        raw = unquote(str(raw))
        if ":" in raw:
            ex, sym = raw.split(":", 1)
            return ex.strip().upper(), sym.strip().upper()
        return "", raw.strip().upper() or "XAUUSD"
    except Exception:
        return "", "XAUUSD"


def _merge_interval_map(tv: dict[str, Any], tvd: dict[str, Any]) -> dict[str, str]:
    m = dict(_DEFAULT_INTERVAL_MAP)
    raw = tvd.get("interval_map")
    if isinstance(raw, dict):
        for k, v in raw.items():
            ks = str(k).strip()
            vs = str(v).strip()
            if ks and vs:
                m[ks] = vs
    raw_tv = tv.get("interval_map")
    if isinstance(raw_tv, dict):
        for k, v in raw_tv.items():
            ks = str(k).strip()
            vs = str(v).strip()
            if ks and vs:
                m[ks] = vs
    return m


def _interval_enum_from_label(
    label: str,
    *,
    interval_map: dict[str, str],
    row: Optional[dict[str, Any]] = None,
) -> Any:
    _, Interval = _load_tvdatafeed()

    if row and isinstance(row.get("interval_tvdatafeed"), str):
        name = str(row["interval_tvdatafeed"]).strip()
        if name and hasattr(Interval, name):
            return getattr(Interval, name)
    name = interval_map.get(str(label).strip())
    if not name:
        raise ValueError(
            f"Unknown interval label {label!r} for tvdatafeed; add tradingview_capture.tvdatafeed.interval_map"
        )
    if not hasattr(Interval, name):
        raise ValueError(f"Invalid Interval.{name} for label {label!r}")
    return getattr(Interval, name)


def _parse_capture_plan_rows(tv: dict[str, Any]) -> list[dict[str, Any]]:
    raw = tv.get("capture_plan")
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip()
        intervals = row.get("intervals") or row.get("intervals_aria")
        if not sym or not isinstance(intervals, list):
            continue
        labels = [str(x).strip() for x in intervals if str(x).strip()]
        if not labels:
            continue
        ex = row.get("exchange")
        ex_s = str(ex).strip() if ex is not None else ""
        tv_sym = row.get("tv_symbol")
        tv_sym_s = str(tv_sym).strip() if tv_sym is not None else ""
        out.append(
            {
                "symbol": sym,
                "intervals": labels,
                "exchange": ex_s or None,
                "tv_symbol": tv_sym_s or None,
            }
        )
    return out


def _default_multi_rows() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "DXY",
            "intervals": ["4 giờ", "1 giờ", "15 phút"],
            "exchange": None,
            "tv_symbol": None,
        },
        {
            "symbol": "XAUUSD",
            "intervals": ["4 giờ", "1 giờ", "15 phút", "5 phút"],
            "exchange": None,
            "tv_symbol": None,
        },
    ]


def effective_tvdatafeed_plan(tv: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Same branching intent as browser TradingView: multi_shot + capture_plan vs single frame.
    """
    if not tv.get("multi_shot_enabled", True):
        ex, sym = parse_tradingview_chart_url(str(tv.get("chart_url") or ""))
        aria = (tv.get("interval_button_aria_label") or "15 phút").strip()
        return [
            {
                "symbol": sym,
                "intervals": [aria],
                "exchange": ex or None,
                "tv_symbol": None,
            }
        ]
    rows = _parse_capture_plan_rows(tv)
    if rows:
        return rows
    resolved = _tradingview_resolve_capture_plan(tv)
    if resolved:
        return [
            {
                "symbol": s,
                "intervals": labels,
                "exchange": None,
                "tv_symbol": None,
            }
            for s, labels in resolved
        ]
    return _default_multi_rows()


def _resolve_row_exchange(
    row: dict[str, Any],
    tvd: dict[str, Any],
    chart_default_exchange: str,
) -> str:
    if row.get("exchange"):
        return str(row["exchange"]).strip().upper()
    sym = str(row.get("symbol") or "")
    m = tvd.get("symbol_exchanges")
    if isinstance(m, dict) and sym in m:
        return str(m[sym]).strip().upper()
    ex = tvd.get("exchange")
    if ex is not None and str(ex).strip():
        return str(ex).strip().upper()
    return (chart_default_exchange or "OANDA").strip().upper()


def _resolve_tv_symbol(row: dict[str, Any]) -> str:
    if row.get("tv_symbol"):
        return str(row["tv_symbol"]).strip().upper()
    return str(row["symbol"]).strip().upper()


def _df_to_records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        out = df.reset_index()
        # pandas Timestamp columns → ISO strings in JSON
        return json.loads(out.to_json(orient="records", date_format="iso"))
    except Exception as e:
        _log.warning("tvdatafeed: dataframe to_json failed: %s", e)
        return []


def _init_thread_tv(username: Optional[str], password: Optional[str]) -> None:
    TvDatafeed, _ = _load_tvdatafeed()

    if username and password:
        _thread_local.tv = TvDatafeed(username, password)
    else:
        _thread_local.tv = TvDatafeed()


def _get_thread_tv() -> Any:
    tv = getattr(_thread_local, "tv", None)
    if tv is None:
        raise RuntimeError("tvdatafeed thread client not initialized")
    return tv


def _fetch_one_barset(
    *,
    tv_symbol: str,
    exchange: str,
    interval_label: str,
    interval_map: dict[str, str],
    n_bars: int,
    extended_session: bool,
    row: dict[str, Any],
    tv_cfg: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str]:
    """Returns (interval_slug, records, interval_attr_name)."""
    iv = _interval_enum_from_label(interval_label, interval_map=interval_map, row=row)
    iv_name = iv.name if hasattr(iv, "name") else str(iv)
    slug = _tradingview_interval_slug(interval_label, tv_cfg)
    client = _get_thread_tv()
    df = client.get_hist(
        tv_symbol,
        exchange,
        interval=iv,
        n_bars=n_bars,
        extended_session=extended_session,
    )
    rec = _df_to_records(df)
    return slug, rec, iv_name


def run_tvdatafeed_export(
    *,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    tradingview_username: Optional[str],
    tradingview_password: Optional[str],
) -> list[Path]:
    """
    Download OHLC via tvdatafeed and write one JSON per (symbol row × interval).

    Credentials: ``tradingview_username`` / ``tradingview_password`` (typically from env).
    If both missing, uses nologin ``TvDatafeed()`` (data may be limited).
    """
    _load_tvdatafeed()
    if not isinstance(tv, dict):
        return []
    tvd = tv.get("tvdatafeed") or {}
    if not isinstance(tvd, dict):
        tvd = {}

    n_bars = int(tvd.get("n_bars") or 1000)
    n_bars = max(1, min(n_bars, 5000))
    # Khi get_hist trả 0 nến: thử lại thêm tối đa N lần (tổng gọi = 1 + N).
    empty_bars_max_retries = int(tvd.get("empty_bars_max_retries") or 3)
    empty_bars_max_retries = max(0, min(empty_bars_max_retries, 20))
    empty_bars_retry_delay_ms = int(tvd.get("empty_bars_retry_delay_ms") or 800)
    empty_bars_retry_delay_ms = max(0, min(empty_bars_retry_delay_ms, 60_000))
    parallel_max_workers = int(tvd.get("parallel_max_workers") or 4)
    if parallel_max_workers < 1:
        parallel_max_workers = 1
    extended_session = bool(tvd.get("extended_session", False))

    chart_url = str(tv.get("chart_url") or "")
    chart_ex, _chart_sym = parse_tradingview_chart_url(chart_url)
    interval_map = _merge_interval_map(tv, tvd)

    rows = effective_tvdatafeed_plan(tv)
    if not rows:
        raise SystemExit("tvdatafeed: empty capture plan; check tradingview_capture.capture_plan")

    charts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    tasks: list[dict[str, Any]] = []
    for row in rows:
        ex = _resolve_row_exchange(row, tvd, chart_ex)
        tv_sym = _resolve_tv_symbol(row)
        file_sym = str(row["symbol"]).strip()
        file_sym_key = re.sub(r"[^\w.-]+", "_", file_sym).strip("_")[:40] or "sym"
        for label in row["intervals"]:
            tasks.append(
                {
                    "row": row,
                    "exchange": ex,
                    "tv_symbol": tv_sym,
                    "file_sym_key": file_sym_key,
                    "label": label,
                }
            )

    u = (
        (
            tvd.get("username")
            or os.getenv("TRADINGVIEW_USERNAME")
            or tradingview_username
            or os.getenv("COINMAP_EMAIL")
            or ""
        )
        .strip()
        or None
    )
    p = (tvd.get("password") or tradingview_password or os.getenv("TRADINGVIEW_PASSWORD") or "").strip() or None
    if u is None:
        u = (tv.get("username") or "").strip() or None
    if p is None:
        p = (tv.get("password") or "").strip() or None

    def _run_task(meta: dict[str, Any]) -> Path:
        row = meta["row"]
        label = meta["label"]
        ex_s = str(meta["exchange"])
        sym_s = str(meta["tv_symbol"])
        slug = ""
        records: list[dict[str, Any]] = []
        iv_name = ""
        for attempt in range(empty_bars_max_retries + 1):
            try:
                slug, records, iv_name = _fetch_one_barset(
                    tv_symbol=meta["tv_symbol"],
                    exchange=meta["exchange"],
                    interval_label=label,
                    interval_map=interval_map,
                    n_bars=n_bars,
                    extended_session=extended_session,
                    row=row,
                    tv_cfg=tv,
                )
            except Exception as e:
                _log.error(
                    "tvdatafeed: LỖI khi get_hist | %s:%s | %s (%s) | requested=%d bars | %s",
                    ex_s,
                    sym_s,
                    label,
                    type(e).__name__,
                    n_bars,
                    e,
                    exc_info=True,
                )
                raise
            if len(records) > 0:
                if attempt > 0:
                    _log.info(
                        "tvdatafeed: sau %d lần thử lại có dữ liệu | %s:%s | %s → n_bars=%d",
                        attempt,
                        ex_s,
                        sym_s,
                        label,
                        len(records),
                    )
                break
            if attempt < empty_bars_max_retries:
                _log.warning(
                    "tvdatafeed: 0 nến — thử lại %d/%d sau %dms | %s:%s | %s",
                    attempt + 1,
                    empty_bars_max_retries,
                    empty_bars_retry_delay_ms,
                    ex_s,
                    sym_s,
                    label,
                )
                if empty_bars_retry_delay_ms > 0:
                    time.sleep(empty_bars_retry_delay_ms / 1000.0)
        path = charts_dir / f"{stamp}_tradingview_{meta['file_sym_key']}_{slug}.json"
        payload = {
            "source": "tvdatafeed",
            "symbol": meta["tv_symbol"],
            "exchange": meta["exchange"],
            "interval": iv_name,
            "interval_label": label,
            "n_bars": len(records),
            "n_bars_requested": n_bars,
            "bars": records,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        nb = len(records)
        if nb == 0:
            _log.warning(
                "tvdatafeed: thành công nhưng 0 nến | %s:%s | %s → interval=%s | file=%s "
                "(TradingView/tvdatafeed không trả dữ liệu — kiểm tra sàn, đăng nhập, mạng)",
                ex_s,
                sym_s,
                label,
                iv_name,
                path.name,
            )
        else:
            _log.info(
                "tvdatafeed: OK | %s:%s | %s → interval=%s | n_bars=%d/%d | file=%s",
                ex_s,
                sym_s,
                label,
                iv_name,
                nb,
                n_bars,
                path.name,
            )
        return path

    # One TvDatafeed per worker thread (parallel get_hist); initializer sets thread-local client
    init_u = u
    init_p = p

    def _init() -> None:
        _init_thread_tv(init_u, init_p)

    workers = min(parallel_max_workers, max(1, len(tasks)))
    if len(tasks) == 1:
        workers = 1

    _log.info(
        "tvdatafeed: bắt đầu export | stamp=%s | jobs=%d | parallel=%d | đăng_nhập_tv=%s",
        stamp,
        len(tasks),
        workers,
        "có" if (init_u and init_p) else "không (nologin — dữ liệu có thể giới hạn)",
    )

    if workers == 1:
        _init_thread_tv(init_u, init_p)
        for m in tasks:
            written.append(_run_task(m))
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            initializer=_init,
        ) as pool:
            futs = []
            for m in tasks:
                futs.append(pool.submit(_run_task, m))
            for fut in as_completed(futs):
                written.append(fut.result())

    written.sort(key=lambda p: p.name)
    _log.info("tvdatafeed: hoàn tất export | stamp=%s | files=%d", stamp, len(written))
    return written
