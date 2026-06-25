#!/usr/bin/env python3
"""Passive sniff of GoCharting HTTP + WebSocket traffic (step 1 discovery)."""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.config import default_storage_state_path, load_all_dotenv  # noqa: E402
from automation_tool.gocharting_capture import (  # noqa: E402
    _maybe_login_gocharting,
    load_gocharting_yaml,
)
from automation_tool.playwright_browser import (  # noqa: E402
    close_browser_and_context,
    launch_chrome_context,
)

_log = logging.getLogger(__name__)

_DEFAULT_CHART_URL = "https://gocharting.com/terminal/chart/GC435uijM"


def _frame_bytes(frame) -> bytes:
    payload = frame.payload if hasattr(frame, "payload") else frame
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return bytes(payload)


def _preview_body(body: bytes | str, limit: int = 200) -> str:
    if isinstance(body, str):
        return body[:limit]
    if body[:1] in (b"{", b"["):
        try:
            return body.decode("utf-8", errors="replace")[:limit]
        except Exception:
            pass
    return f"<binary {len(body)} bytes> b64={base64.b64encode(body[:64]).decode()}"


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in text)
    return slug.strip("_")[:max_len] or "payload"


def _try_decode_protobuf(raw: bytes) -> list[dict]:
    from automation_tool.gocharting_ws_decode import (
        decode_footprint_for_date_response,
        parse_ws_binary_envelope,
    )

    results: list[dict] = []
    parsed = parse_ws_binary_envelope(raw)
    payloads: list[tuple[int, bytes]] = []
    if parsed is not None:
        _type_str, payload = parsed
        payloads.append((5 + raw[4], payload))
    else:
        for offset in (0, 1, 2, 4, 5, 8, 22):
            if offset < len(raw):
                payloads.append((offset, raw[offset:]))

    for offset, chunk in payloads:
        if not chunk:
            continue
        try:
            msg = decode_footprint_for_date_response(chunk)
            text = str(msg)
            if len(text) > 30:
                results.append(
                    {
                        "message": "FootPrintForDateResponse",
                        "offset": offset,
                        "candles": len(msg.candles),
                        "text": text[:500],
                    }
                )
        except Exception:
            continue
    return results


def run_sniff(
    *,
    chart_url: str,
    gocharting_yaml: Path,
    out_dir: Path,
    wait_ms: int,
    headless: bool,
) -> Path:
    load_all_dotenv()
    cfg = load_gocharting_yaml(gocharting_yaml)
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"sniff_{stamp}.jsonl"
    ws_bin_dir = out_dir / f"ws_frames_{stamp}"
    ws_bin_dir.mkdir(exist_ok=True)

    http_count = 0
    ws_count = 0

    def _write(record: dict) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    with sync_playwright() as p:
        vw = int(cfg.get("viewport_width", 1920))
        vh = int(cfg.get("viewport_height", 1080))
        browser, context = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = context.new_page()

        def on_response(response) -> None:
            nonlocal http_count
            url = response.url
            if "gocharting" not in url.lower():
                return
            if any(x in url.lower() for x in (".js", ".css", ".png", ".woff", ".svg", ".ico")):
                return
            try:
                body = response.body()
            except Exception as exc:
                body = f"<read error: {exc}>".encode()
            http_count += 1
            rec = {
                "type": "http",
                "n": http_count,
                "status": response.status,
                "url": url,
                "content_type": (response.headers.get("content-type") or "").lower(),
                "size": len(body) if isinstance(body, (bytes, bytearray)) else 0,
                "preview": _preview_body(body),
            }
            print(f"[HTTP #{http_count}] {response.status} {rec['size']}B {url[:120]}")
            _write(rec)
            if isinstance(body, (bytes, bytearray)) and len(body) > 500:
                fname = out_dir / f"http_{http_count}_{_safe_slug(url)}.bin"
                fname.write_bytes(body)

        page.on("response", on_response)

        def on_websocket(ws) -> None:
            ws_url = ws.url
            print(f"[WS OPEN] {ws_url}")
            _write({"type": "ws_open", "url": ws_url})

            def on_frame(frame, direction: str) -> None:
                nonlocal ws_count
                ws_count += 1
                data = _frame_bytes(frame)
                fname = ws_bin_dir / f"{ws_count:04d}_{direction}.bin"
                fname.write_bytes(data)
                decode_hints = _try_decode_protobuf(data) if len(data) > 20 else []
                rec = {
                    "type": "ws_frame",
                    "n": ws_count,
                    "direction": direction,
                    "ws_url": ws_url,
                    "size": len(data),
                    "preview": _preview_body(data),
                    "file": str(fname.relative_to(ROOT)),
                    "protobuf": decode_hints[:3],
                }
                hint = ""
                if decode_hints:
                    hint = f" proto={decode_hints[0]['message']}@{decode_hints[0]['offset']}"
                print(f"[WS #{ws_count} {direction}] {len(data)}B{hint} → {fname.name}")
                _write(rec)

            ws.on("framereceived", lambda frame: on_frame(frame, "recv"))
            ws.on("framesent", lambda frame: on_frame(frame, "send"))

        page.on("websocket", on_websocket)

        print(f"Opening {chart_url}")
        print(f"Log: {log_path}")
        page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
        _maybe_login_gocharting(page, cfg, email, password)
        page.wait_for_timeout(3000)

        print(f"Waiting {wait_ms}ms for WebSocket traffic...")
        page.wait_for_timeout(wait_ms)

        summary = {
            "type": "summary",
            "http_count": http_count,
            "ws_frame_count": ws_count,
            "log_path": str(log_path),
            "ws_bin_dir": str(ws_bin_dir),
        }
        _write(summary)
        print(
            f"\nDone. HTTP={http_count}, WS frames={ws_count}\n"
            f"JSONL: {log_path}\nWS bins: {ws_bin_dir}"
        )

        close_browser_and_context(browser, context)

    return log_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Sniff GoCharting network (WS + HTTP)")
    parser.add_argument(
        "--url",
        default=_DEFAULT_CHART_URL,
        help=f"Chart page URL (default: {_DEFAULT_CHART_URL})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "gocharting.yaml",
        help="Path to gocharting.yaml",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "data" / "network_sniff",
        help="Output directory for logs and binary frames",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=20_000,
        help="Milliseconds to wait after page load for WS traffic",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (default: headed)",
    )
    args = parser.parse_args()
    run_sniff(
        chart_url=args.url,
        gocharting_yaml=args.config,
        out_dir=args.out_dir,
        wait_ms=args.wait_ms,
        headless=args.headless,
    )


if __name__ == "__main__":
    main()
