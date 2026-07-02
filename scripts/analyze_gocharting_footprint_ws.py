#!/usr/bin/env python3
"""Fresh-profile GoCharting WS sniff: map FOOTPRINT/V2 requests/responses by session date."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.config import default_storage_state_path, load_all_dotenv  # noqa: E402
from automation_tool.gocharting_capture import _maybe_login_gocharting, load_gocharting_yaml  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    decode_footprint_for_date_response,
    decode_ohlc_bar_result,
    parse_ws_binary_envelope,
    proto_candle_time_key,
)
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context  # noqa: E402

_log = logging.getLogger(__name__)
_TZ_GMT7 = timezone(timedelta(hours=7))
_DEFAULT_URL = "https://gocharting.com/terminal/chart/GC435uijM"


def _frame_bytes(frame) -> bytes:
    payload = frame.payload if hasattr(frame, "payload") else frame
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return bytes(payload)


def _try_parse_json_ws(data: bytes) -> Any | None:
    if not data:
        return None
    if data[:1] in (b"{", b"["):
        try:
            return json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None
    try:
        text = zlib.decompress(data).decode("utf-8", errors="replace")
        return json.loads(text)
    except Exception:
        return None


def _decode_binary_frame(data: bytes) -> dict[str, Any] | None:
    parsed = parse_ws_binary_envelope(data)
    if parsed is None:
        return None
    type_str, payload = parsed
    out: dict[str, Any] = {"ws_type": type_str, "size": len(data)}
    parts = type_str.split("~")
    if parts[0].startswith("FOOTPRINT"):
        msg = decode_footprint_for_date_response(payload)
        req = msg.request
        candles = sorted(msg.candles, key=lambda c: c.date)
        first_t = proto_candle_time_key(candles[0].date) if candles else ""
        last_t = proto_candle_time_key(candles[-1].date) if candles else ""
        out.update(
            {
                "kind": "FOOTPRINT",
                "request_date": req.date,
                "session": req.session,
                "interval": req.interval,
                "symbol": f"{req.exchange}:{req.symbol}",
                "candles": len(candles),
                "candle_iso_dates": _candle_calendar_dates(msg),
                "is_complete": bool(msg.is_complete),
                "version": int(msg.version),
                "first_candle": first_t,
                "last_candle": last_t,
                "next_cursor_hint": parts[1] if len(parts) > 1 and parts[1] else None,
            }
        )
        return out
    if parts[0].startswith("TS/"):
        msg = decode_ohlc_bar_result(payload)
        session_dates = sorted(msg.intraday_candles.keys())
        candle_count = sum(len(b.candles) for b in msg.intraday_candles.values())
        out.update(
            {
                "kind": "TS",
                "session_dates": session_dates,
                "candles": candle_count,
                "count": int(msg.count),
            }
        )
        return out
    out["kind"] = "OTHER"
    return out


def _summarize_sent(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    cmd = str(obj.get("command") or "")
    if cmd not in ("FOOTPRINT/V2", "TS/V2"):
        return None
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    summary: dict[str, Any] = {
        "command": cmd,
        "request_id": obj.get("request_id"),
    }
    if cmd == "FOOTPRINT/V2":
        if "dates" in payload:
            summary["dates"] = payload.get("dates")
            summary["dates_with_version"] = payload.get("dates_with_version")
            summary["session"] = payload.get("session")
            summary["interval"] = payload.get("interval")
            summary["symbol"] = payload.get("symbol")
            summary["exchange"] = payload.get("exchange")
        if "ref" in payload:
            summary["ref"] = payload.get("ref")
    if cmd == "TS/V2":
        summary["msg_type"] = payload.get("msg_type")
        summary["hint"] = payload.get("hint")
        summary["interval"] = payload.get("interval")
        summary["session"] = payload.get("session")
    return summary


def _zoom_chart(page, *, clicks: int, delay_ms: int = 500) -> None:
    if clicks <= 0:
        return
    btn = page.locator("#zoomIn-button").first
    for _ in range(clicks):
        try:
            btn.click(timeout=3000)
        except Exception:
            break
        page.wait_for_timeout(delay_ms)


def _summarize_recv_json(obj: dict[str, Any]) -> dict[str, Any] | None:
    cmd = str(obj.get("command") or "")
    if "FOOTPRINT" not in cmd:
        return None
    out: dict[str, Any] = {"kind": "json_footprint", "command": cmd}
    for key in ("request_id", "next_cursor"):
        if key in obj:
            out[key] = obj[key]
    payload_in = obj.get("in")
    payload_out = obj.get("out")
    if isinstance(payload_in, dict):
        out["in"] = payload_in
    if isinstance(payload_out, dict):
        req = payload_out.get("request") if isinstance(payload_out.get("request"), dict) else payload_in
        if isinstance(req, dict):
            out["request_date"] = req.get("date")
        candles = payload_out.get("candles")
        if isinstance(candles, list):
            out["candles"] = len(candles)
    return out


def _candle_calendar_dates(msg) -> list[str]:
    from automation_tool.gocharting_ws_decode import proto_candle_time_key

    days: list[str] = []
    for candle in sorted(msg.candles, key=lambda c: c.date):
        if not candle.date:
            continue
        # session date from ISO timestamp (exchange local)
        days.append(candle.date[:10])
    return sorted(set(days))


def _pan_chart_left(page, *, steps: int = 3) -> None:
    root = page.locator("#chart-root-0").first
    if root.count() == 0:
        box = page.viewport_size
        if not box:
            return
        x0, x1, y = int(box["width"] * 0.2), int(box["width"] * 0.85), int(box["height"] * 0.5)
    else:
        box = root.bounding_box()
        if not box:
            return
        x0 = int(box["x"] + box["width"] * 0.15)
        x1 = int(box["x"] + box["width"] * 0.85)
        y = int(box["y"] + box["height"] * 0.5)
    for _ in range(steps):
        page.mouse.move(x1, y)
        page.mouse.down()
        page.mouse.move(x0, y, steps=20)
        page.mouse.up()
        page.wait_for_timeout(1500)


def _read_idb_keys(page) -> list[str]:
    js = """
    async () => {
      const openDb = () => new Promise((res, rej) => {
        const r = indexedDB.open('GoChartingData', 3);
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error);
      });
      try {
        const db = await openDb();
        const tx = db.transaction('BinaryFootprint', 'readonly');
        const keys = await new Promise((res, rej) => {
          const req = tx.objectStore('BinaryFootprint').getAllKeys();
          req.onsuccess = () => res(req.result || []);
          req.onerror = () => rej(req.error);
        });
        return keys.map(String);
      } catch (e) {
        return [];
      }
    }
    """
    keys: list[str] = []
    try:
        keys.extend(page.evaluate(js))
    except Exception:
        pass
    for worker in page.workers:
        try:
            keys.extend(worker.evaluate(js))
        except Exception:
            pass
    return sorted(set(keys))


def run_analysis(
    *,
    chart_url: str,
    gocharting_yaml: Path,
    wait_ms: int,
    pan_steps: int,
    zoom_clicks: int,
    headless: bool,
    out_dir: Path,
    fresh_profile: bool,
) -> Path:
    load_all_dotenv()
    if fresh_profile:
        os.environ.pop("PLAYWRIGHT_CHROME_USER_DATA_DIR", None)
    cfg = load_gocharting_yaml(gocharting_yaml)
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"footprint_ws_analysis_{stamp}.json"
    ws_bin_dir = out_dir / f"ws_frames_{stamp}"
    ws_bin_dir.mkdir(exist_ok=True)

    sent_events: list[dict[str, Any]] = []
    recv_events: list[dict[str, Any]] = []
    ws_opens: list[str] = []
    sent_raw_stats: list[dict[str, Any]] = []
    frame_recv_n = 0
    frame_sent_n = 0

    profile_mode = "fresh_ephemeral" if fresh_profile else "persistent_env"
    _log.info("Profile=%s storage_state=%s", profile_mode, storage)

    fp_cfg = cfg.get("footprint_screenshot") or {}
    iv_cfg = (fp_cfg.get("intervals") or {}).get("5m") or {}
    zoom = zoom_clicks if zoom_clicks >= 0 else int(iv_cfg.get("zoom_clicks") or 0)
    vw = int(iv_cfg.get("viewport_width") or 500)
    vh = int(iv_cfg.get("viewport_height") or 1500)
    recv_raw_stats: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser, ctx = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = ctx.new_page()

        def on_websocket(ws) -> None:
            nonlocal frame_recv_n, frame_sent_n
            ws_opens.append(ws.url)

            def on_sent(frame) -> None:
                nonlocal frame_sent_n
                frame_sent_n += 1
                data = _frame_bytes(frame)
                (ws_bin_dir / f"{frame_sent_n:04d}_sent.bin").write_bytes(data)
                sent_raw_stats.append({"n": frame_sent_n, "size": len(data)})
                obj = _try_parse_json_ws(data)
                summary = _summarize_sent(obj)
                if summary:
                    sent_events.append(summary)
                    _log.info("WS SENT %s", summary)
                elif isinstance(obj, dict) and obj.get("command") == "FOOTPRINT/V2":
                    sent_events.append({"command": "FOOTPRINT/V2", "raw": obj})

            def on_recv(frame) -> None:
                nonlocal frame_recv_n
                frame_recv_n += 1
                data = _frame_bytes(frame)
                (ws_bin_dir / f"{frame_recv_n:04d}_recv.bin").write_bytes(data)
                recv_raw_stats.append({"n": frame_recv_n, "size": len(data), "head": data[:8].hex() if data else ""})
                obj = _try_parse_json_ws(data)
                if isinstance(obj, dict):
                    fp_json = _summarize_recv_json(obj)
                    if fp_json:
                        recv_events.append(fp_json)
                        _log.info("WS RECV JSON FOOTPRINT %s", fp_json)
                    elif obj.get("command"):
                        recv_events.append({"kind": "json", "command": obj.get("command")})
                        return
                decoded = _decode_binary_frame(data)
                if decoded:
                    recv_events.append(decoded)
                    if decoded.get("kind") == "FOOTPRINT":
                        _log.info(
                            "WS RECV FOOTPRINT date=%s iso_dates=%s candles=%s %s..%s complete=%s cursor=%s",
                            decoded.get("request_date"),
                            decoded.get("candle_iso_dates"),
                            decoded.get("candles"),
                            decoded.get("first_candle"),
                            decoded.get("last_candle"),
                            decoded.get("is_complete"),
                            decoded.get("next_cursor_hint"),
                        )
                elif len(data) > 20 and data[:1] == b"m":
                    recv_events.append({"kind": "BINARY_UNDECODED", "size": len(data), "n": frame_recv_n})

            ws.on("framesent", on_sent)
            ws.on("framereceived", on_recv)

        page.on("websocket", on_websocket)

        _log.info("Opening %s", chart_url)
        page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
        _maybe_login_gocharting(page, cfg, email, password)
        page.wait_for_timeout(5000)
        if zoom > 0:
            _log.info("zoomIn x%d (same as footprint screenshot)", zoom)
            _zoom_chart(page, clicks=zoom)

        idb_after_load = _read_idb_keys(page)
        _log.info("IndexedDB keys after load: %d workers=%d", len(idb_after_load), len(page.workers))

        _log.info("Waiting %dms for WS (no pan)...", wait_ms)
        page.wait_for_timeout(wait_ms)

        if pan_steps > 0:
            _log.info("Panning chart left x%d...", pan_steps)
            _pan_chart_left(page, steps=pan_steps)
            page.wait_for_timeout(wait_ms)

        idb_final = _read_idb_keys(page)

        footprint_sent = [e for e in sent_events if e.get("command") == "FOOTPRINT/V2"]
        footprint_recv = [e for e in recv_events if e.get("kind") in ("FOOTPRINT", "json_footprint")]
        ts_sent = [e for e in sent_events if e.get("command") == "TS/V2"]
        ts_recv = [e for e in recv_events if e.get("kind") == "TS"]

        dates_requested: list[str] = []
        for e in footprint_sent:
            for d in e.get("dates") or []:
                if d not in dates_requested:
                    dates_requested.append(str(d))

        dates_received = sorted({
            str(e.get("request_date") or "")
            for e in footprint_recv
            if e.get("request_date")
        })

        today = datetime.now(_TZ_GMT7).date().isoformat()
        yesterday = (datetime.now(_TZ_GMT7).date() - timedelta(days=1)).isoformat()

        report = {
            "chart_url": chart_url,
            "profile_mode": profile_mode,
            "zoom_clicks": zoom,
            "pan_steps": pan_steps,
            "today_gmt7": today,
            "yesterday_gmt7": yesterday,
            "ws_urls": ws_opens,
            "ws_bin_dir": str(ws_bin_dir),
            "sent_frame_count": len(sent_raw_stats),
            "recv_frame_count": len(recv_raw_stats),
            "sent_events": sent_events,
            "recv_frame_sizes": recv_raw_stats[:60],
            "idb_keys_after_load": idb_after_load,
            "idb_keys_final": idb_final,
            "idb_keys_created_during_session": sorted(set(idb_final) - set(idb_after_load)),
            "footprint_sent_count": len(footprint_sent),
            "footprint_recv_count": len(footprint_recv),
            "ts_sent_count": len(ts_sent),
            "ts_recv_count": len(ts_recv),
            "dates_requested_in_ws": dates_requested,
            "dates_in_footprint_responses": dates_received,
            "footprint_sent": footprint_sent,
            "footprint_recv_summary": footprint_recv,
            "ts_sent": ts_sent,
            "ts_recv_summary": [
                {k: e.get(k) for k in ("session_dates", "candles", "count", "ws_type")}
                for e in ts_recv
            ],
            "undecoded_binary": [e for e in recv_events if e.get("kind") == "BINARY_UNDECODED"],
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _log.info("Report: %s", report_path)
        close_browser_and_context(browser, ctx)

    return report_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Analyze GoCharting footprint WS on fresh profile")
    parser.add_argument("--url", default=_DEFAULT_URL)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "network_sniff")
    parser.add_argument("--wait-ms", type=int, default=45_000)
    parser.add_argument("--pan-steps", type=int, default=0, help="0 = no pan (match UI default)")
    parser.add_argument("--zoom-clicks", type=int, default=-1, help="-1 = use gocharting.yaml 5m zoom")
    parser.add_argument("--fresh-profile", action="store_true", help="Empty IndexedDB (unset PLAYWRIGHT_CHROME_USER_DATA_DIR)")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    path = run_analysis(
        chart_url=args.url,
        gocharting_yaml=args.config,
        wait_ms=args.wait_ms,
        pan_steps=args.pan_steps,
        zoom_clicks=args.zoom_clicks,
        headless=args.headless,
        out_dir=args.out_dir,
        fresh_profile=args.fresh_profile,
    )
    data = json.loads(path.read_text())
    print(json.dumps({
        "report": str(path),
        "dates_requested": data.get("dates_requested_in_ws"),
        "dates_received": data.get("dates_in_footprint_responses"),
        "ts_session_dates": [s.get("session_dates") for s in data.get("ts_recv_summary") or []],
        "idb_created": data.get("idb_keys_created_during_session"),
        "footprint_frames": data.get("footprint_recv_count"),
    }, indent=2))


if __name__ == "__main__":
    main()
