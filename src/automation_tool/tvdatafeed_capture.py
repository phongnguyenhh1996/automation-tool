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
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

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
            "intervals": ["1 giờ", "15 phút"],
            "exchange": None,
            "tv_symbol": None,
        },
        {
            "symbol": "XAUUSD",
            "intervals": ["1 giờ", "15 phút", "5 phút"],
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


def _n_bars_for_interval_label(
    label: str,
    tvd: dict[str, Any],
    default_n_bars: int,
) -> int:
    """Resolve ``n_bars`` for ``get_hist``: optional ``tvdatafeed.n_bars_by_interval`` by exact label."""
    raw = tvd.get("n_bars_by_interval")
    if isinstance(raw, dict):
        key = str(label).strip()
        if key in raw:
            try:
                v = int(raw[key])
            except (TypeError, ValueError):
                return default_n_bars
            return max(1, min(v, 5000))
    return default_n_bars


def resolve_tvdatafeed_credentials(
    tv: dict[str, Any],
    *,
    tradingview_username: Optional[str],
    tradingview_password: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    Same resolution as :func:`run_tvdatafeed_export`: ``tvdatafeed`` / ``tradingview_capture`` yaml,
    then ``TRADINGVIEW_USERNAME``, ``COINMAP_EMAIL``, ``TRADINGVIEW_PASSWORD``.
    """
    if not isinstance(tv, dict):
        tv = {}
    tvd = tv.get("tvdatafeed")
    tvd = tvd if isinstance(tvd, dict) else {}
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
    return u, p


_TV_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def tradingview_signin_fetch_token(username: str, password: str) -> tuple[Optional[str], str]:
    """
    Sign in via HTTPS (browser-like headers + cookie warmup). Same endpoint as
    ``tvDatafeed.main.TvDatafeed.__auth``, but that method uses bare ``requests`` and
    often fails while this flow succeeds — token is then injected into ``TvDatafeed``
    via :func:`_tvdatafeed_construct_with_token`.

    Returns ``(auth_token_or_none, safe_detail)`` — never puts the raw token in ``detail``.
    """
    url = "https://www.tradingview.com/accounts/signin/"
    data = {"username": username, "password": password, "remember": "on"}
    post_headers = {
        "Referer": "https://www.tradingview.com/",
        "Origin": "https://www.tradingview.com",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers=_TV_BROWSER_HEADERS,
        ) as client:
            client.get("https://www.tradingview.com/")
            client.get("https://www.tradingview.com/accounts/signin/")
            r = client.post(url, data=data, headers=post_headers)
    except httpx.RequestError as e:
        return None, f"signin request failed: {type(e).__name__}: {e}"
    body = r.text or ""
    try:
        payload = r.json()
    except Exception:
        excerpt = body[:1500].replace("\n", " ")
        return None, f"signin HTTP {r.status_code}, non-JSON body (excerpt): {excerpt!r}"
    if isinstance(payload, str):
        hint = (
            " (TV thường trả câu này khi chặn/throttle request tự động — thử lại sau, "
            "đổi mạng/VPN, hoặc đăng nhập tay trên tradingview.com để xác minh tài khoản.)"
            if "trouble" in payload.lower()
            else ""
        )
        return None, f"signin HTTP {r.status_code}, no auth_token | response: {payload!r}{hint}"[
            :2800
        ]
    user = payload.get("user")
    if isinstance(user, dict) and user.get("auth_token"):
        tok = user.get("auth_token")
        if isinstance(tok, str) and tok:
            n = len(tok)
            return tok, f"signin HTTP {r.status_code}, auth_token received (length={n})"
    err = payload.get("error") or payload.get("detail") or payload.get("message")
    if err is None:
        err = payload
    hint = ""
    err_s = err if isinstance(err, str) else repr(err)
    if isinstance(err, str) and "trouble" in err.lower():
        hint = (
            " — Gợi ý: request script dễ bị TV từ chối; thư viện tvdatafeed dùng POST tối giản "
            "nên có thể vẫn lỗi dù diagnose khác. Thử lại sau, kiểm tra đăng nhập trên web."
        )
    return None, (
        f"signin HTTP {r.status_code}, no auth_token | response: {err_s!r}{hint}"
    )[:2800]


def tradingview_signin_diagnose(username: str, password: str) -> tuple[bool, str]:
    """
    Same as :func:`tradingview_signin_fetch_token` but returns ``(ok, detail)`` for CLI/tests.

    tvDatafeed only logs ``error while signin`` and swallows exceptions; this returns
    HTTP status and a safe excerpt (no token text).
    """
    tok, detail = tradingview_signin_fetch_token(username, password)
    return (tok is not None, detail)


def _tvdatafeed_construct_with_token(TvDatafeed_cls: Any, auth_token: str) -> Any:
    """
    Build ``TvDatafeed`` using ``auth_token`` from our httpx sign-in.

    The library's ``__auth`` uses ``requests.post`` without cookie warmup, so it often
    returns no token while :func:`tradingview_signin_fetch_token` succeeds. We temporarily
    replace the name-mangled ``__auth`` so ``TvDatafeed(u, p)`` receives the real token.
    """
    orig = TvDatafeed_cls._TvDatafeed__auth  # type: ignore[attr-defined]

    def _inject(self: Any, username: Any, password: Any) -> str:
        return auth_token

    TvDatafeed_cls._TvDatafeed__auth = _inject  # type: ignore[attr-defined]
    try:
        return TvDatafeed_cls("__injected__", "__injected__")
    finally:
        TvDatafeed_cls._TvDatafeed__auth = orig  # type: ignore[attr-defined]


def run_tvdatafeed_login_probe(
    *,
    tv: dict[str, Any],
    tradingview_username: Optional[str],
    tradingview_password: Optional[str],
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    interval_label: Optional[str] = None,
    n_bars: int = 3,
    verbose: bool = False,
) -> tuple[bool, str, int]:
    """
    Try ``TvDatafeed(user, password)`` and one ``get_hist``.

    Returns ``(ok, message, row_count)``.
    """
    TvDatafeed, _ = _load_tvdatafeed()
    if not isinstance(tv, dict):
        tv = {}
    tvd = tv.get("tvdatafeed")
    tvd = tvd if isinstance(tvd, dict) else {}

    u, p = resolve_tvdatafeed_credentials(
        tv,
        tradingview_username=tradingview_username,
        tradingview_password=tradingview_password,
    )
    if not u or not p:
        return (
            False,
            "FAIL: thiếu tài khoản tvdatafeed (cần TRADINGVIEW_PASSWORD và "
            "TRADINGVIEW_USERNAME hoặc COINMAP_EMAIL, hoặc tradingview_capture.tvdatafeed.username/password).",
            0,
        )

    chart_url = str(tv.get("chart_url") or "")
    chart_ex, chart_sym = parse_tradingview_chart_url(chart_url)
    rows = effective_tvdatafeed_plan(tv)
    row0: dict[str, Any] = rows[0] if rows else {}

    ex_s = (exchange or "").strip().upper()
    if not ex_s:
        if rows:
            ex_s = _resolve_row_exchange(rows[0], tvd, chart_ex)
        else:
            ex_tvd = tvd.get("exchange")
            ex_s = str(ex_tvd).strip().upper() if ex_tvd else ""
            if not ex_s:
                ex_s = (chart_ex or "OANDA").strip().upper()

    sym_s = (symbol or "").strip().upper()
    if not sym_s:
        if rows:
            sym_s = _resolve_tv_symbol(rows[0])
        else:
            sym_s = (chart_sym or "XAUUSD").strip().upper()

    label = (interval_label or "").strip()
    if not label:
        if rows and row0.get("intervals"):
            ivs = row0["intervals"]
            if isinstance(ivs, list) and ivs:
                label = str(ivs[0]).strip()
        if not label:
            label = "15 phút"

    nb = max(1, min(int(n_bars), 5000))
    interval_map = _merge_interval_map(tv, tvd)
    extended_session = bool(tvd.get("extended_session", False))

    tok, signin_detail = tradingview_signin_fetch_token(u, p)
    if not tok:
        _log.error("tvdatafeed-login: %s", signin_detail)
        return False, f"FAIL: {signin_detail}", 0

    _log.info("tvdatafeed-login: %s", signin_detail)
    _log.info("tvdatafeed-login: thử get_hist | %s:%s | %s | n_bars=%d", ex_s, sym_s, label, nb)
    try:
        client: Any = _tvdatafeed_construct_with_token(TvDatafeed, tok)
        iv = _interval_enum_from_label(label, interval_map=interval_map, row=row0 or None)
        df = client.get_hist(
            sym_s,
            ex_s,
            interval=iv,
            n_bars=nb,
            extended_session=extended_session,
        )
    except Exception as e:
        _log.error(
            "tvdatafeed-login: get_hist lỗi | %s:%s | %s | %s",
            ex_s,
            sym_s,
            label,
            e,
            exc_info=True,
        )
        msg = f"FAIL: {type(e).__name__}: {e}"
        if verbose:
            msg = msg + "\n" + traceback.format_exc()
        return False, msg, 0

    n = 0
    if df is not None:
        try:
            n = len(df)
        except Exception:
            n = 0

    if n == 0:
        return (
            False,
            f"FAIL: get_hist trả 0 nến | {ex_s}:{sym_s} | {label} (kiểm tra sàn/symbol/đăng nhập/mạng).",
            0,
        )
    iv_name = iv.name if hasattr(iv, "name") else str(iv)
    return (
        True,
        f"OK: đăng nhập tvdatafeed + get_hist | {ex_s}:{sym_s} | {label} → {iv_name} | rows={n}/{nb}",
        n,
    )


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
    tv_client: Any,
    tv_client_lock: threading.Lock,
) -> tuple[str, list[dict[str, Any]], str]:
    """Returns (interval_slug, records, interval_attr_name).

    ``tv_client`` is shared across worker threads; ``get_hist`` must run under
    ``tv_client_lock`` (TvDatafeed websocket is not thread-safe).
    """
    iv = _interval_enum_from_label(interval_label, interval_map=interval_map, row=row)
    iv_name = iv.name if hasattr(iv, "name") else str(iv)
    slug = _tradingview_interval_slug(interval_label, tv_cfg)
    with tv_client_lock:
        df = tv_client.get_hist(
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

    default_n_bars = int(tvd.get("n_bars") or 1000)
    default_n_bars = max(1, min(default_n_bars, 5000))
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
            nb = _n_bars_for_interval_label(label, tvd, default_n_bars)
            tasks.append(
                {
                    "row": row,
                    "exchange": ex,
                    "tv_symbol": tv_sym,
                    "file_sym_key": file_sym_key,
                    "label": label,
                    "n_bars": nb,
                }
            )

    u, p = resolve_tvdatafeed_credentials(
        tv,
        tradingview_username=tradingview_username,
        tradingview_password=tradingview_password,
    )

    TvDatafeed, _ = _load_tvdatafeed()
    _log.info(
        "tvdatafeed: khởi tạo một TvDatafeed (đăng nhập / nologin một lần) | đăng_nhập_tv=%s",
        "có" if (u and p) else "không (nologin — dữ liệu có thể giới hạn)",
    )
    if u and p:
        tok, signin_detail = tradingview_signin_fetch_token(u, p)
        if tok:
            _log.info("tvdatafeed: %s — TvDatafeed dùng auth_token từ sign-in httpx", signin_detail)
            shared_client = _tvdatafeed_construct_with_token(TvDatafeed, tok)
        else:
            _log.warning(
                "tvdatafeed: không lấy token qua httpx (%s) — fallback TvDatafeed(user, password)",
                signin_detail,
            )
            shared_client = TvDatafeed(u, p)
    else:
        shared_client = TvDatafeed()
    shared_lock = threading.Lock()

    def _run_task(meta: dict[str, Any]) -> Path:
        row = meta["row"]
        label = meta["label"]
        task_n_bars = int(meta["n_bars"])
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
                    n_bars=task_n_bars,
                    extended_session=extended_session,
                    row=row,
                    tv_cfg=tv,
                    tv_client=shared_client,
                    tv_client_lock=shared_lock,
                )
            except Exception as e:
                _log.error(
                    "tvdatafeed: LỖI khi get_hist | %s:%s | %s (%s) | requested=%d bars | %s",
                    ex_s,
                    sym_s,
                    label,
                    type(e).__name__,
                    task_n_bars,
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
            "n_bars_requested": task_n_bars,
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
                task_n_bars,
                path.name,
            )
        return path

    workers = min(parallel_max_workers, max(1, len(tasks)))
    if len(tasks) == 1:
        workers = 1

    _log.info(
        "tvdatafeed: bắt đầu export | stamp=%s | jobs=%d | parallel=%d | 1 TvDatafeed + lock cho get_hist",
        stamp,
        len(tasks),
        workers,
    )

    if workers == 1:
        for m in tasks:
            written.append(_run_task(m))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = []
            for m in tasks:
                futs.append(pool.submit(_run_task, m))
            for fut in as_completed(futs):
                written.append(fut.result())

    written.sort(key=lambda p: p.name)
    _log.info("tvdatafeed: hoàn tất export | stamp=%s | files=%d", stamp, len(written))
    return written
