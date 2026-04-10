"""
Browser worker: long-lived Playwright + TCP JSON-lines control plane.

Run: python -m automation_tool.browser_service

Writes data/browser_service_state.json with cdp_http for connect_over_cdp attach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, async_playwright

from automation_tool.browser_protocol import (
    METHOD_CLOSE_TAB,
    METHOD_EVAL,
    METHOD_GOTO,
    METHOD_CAPTURE_CHARTS,
    METHOD_OPEN_TAB,
    METHOD_PING,
    METHOD_QUERY_TEXT,
    METHOD_SHUTDOWN,
    METHOD_SUBSCRIBE_DOM,
    METHOD_TV_WATCHLIST_INIT,
    METHOD_TV_WATCHLIST_POLL,
    METHOD_UNSUBSCRIBE,
    decode_message_line,
)
from automation_tool.config import default_data_dir

_log = logging.getLogger("automation_tool.browser_service")


def _parse_capture_worker_stdout_json(out_b: bytes) -> dict[str, Any]:
    """
    ``capture_worker`` prints exactly one JSON object as its protocol, but the process may
    also write warnings to stdout (e.g. Playwright). Parse the last line that looks like
    a JSON object so we do not lose ``paths`` when ``json.loads`` on the full buffer fails.
    """
    text = (out_b or b"").decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            doc = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return {"ok": True, "paths": []}


_STATE_FILENAME = "browser_service_state.json"
_LOCK_FILENAME = "browser_service.lock"


def _state_path() -> Path:
    return default_data_dir() / _STATE_FILENAME


def _lock_path() -> Path:
    return default_data_dir() / _LOCK_FILENAME


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _release_stale_lock() -> None:
    p = _lock_path()
    if not p.is_file():
        return
    try:
        raw = p.read_text(encoding="utf-8").strip()
        pid = int(raw.split()[0])
    except (OSError, ValueError, IndexError):
        try:
            p.unlink()
        except OSError:
            pass
        return
    if not _pid_alive(pid):
        try:
            p.unlink()
        except OSError:
            pass


def _acquire_lock() -> None:
    """
    Exclusive lock file so two concurrent ``browser_service`` processes cannot both
    pass a check-then-write race (would launch two Chrome instances).
    """
    default_data_dir().mkdir(parents=True, exist_ok=True)
    _release_stale_lock()
    p = _lock_path()
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(
            f"Another browser service may be running (lock: {p}). "
            "Stop it with: coinmap-automation browser down"
        ) from None
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    finally:
        os.close(fd)


def _release_lock() -> None:
    p = _lock_path()
    try:
        if p.is_file():
            raw = p.read_text(encoding="utf-8").strip().split()
            if raw and int(raw[0]) == os.getpid():
                p.unlink()
    except (OSError, ValueError):
        pass


def chrome_user_data_dir_from_env() -> Optional[Path]:
    raw = os.getenv("PLAYWRIGHT_CHROME_USER_DATA_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


class BrowserServiceState:
    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._tabs: dict[str, Any] = {}
        self._subs: dict[str, asyncio.Task] = {}
        self._closing = False

    async def start(self, *, headless: bool, cdp_port: int) -> str:
        self._playwright = await async_playwright().start()
        p = self._playwright
        channel = (os.getenv("PLAYWRIGHT_CHANNEL") or "chrome").strip()
        viewport_w = int(os.getenv("PLAYWRIGHT_VIEWPORT_WIDTH") or "1920")
        viewport_h = int(os.getenv("PLAYWRIGHT_VIEWPORT_HEIGHT") or "1080")
        viewport = {"width": viewport_w, "height": viewport_h}
        user_data = chrome_user_data_dir_from_env()
        args = [f"--remote-debugging-port={cdp_port}"]

        try:
            if user_data is not None:
                user_data.mkdir(parents=True, exist_ok=True)
                self._browser = None
                self._context = await p.chromium.launch_persistent_context(
                    str(user_data),
                    channel=channel,
                    headless=headless,
                    viewport=viewport,
                    args=args,
                )
            else:
                self._browser = await p.chromium.launch(channel=channel, headless=headless, args=args)
                self._context = await self._browser.new_context(viewport=viewport)
        except PlaywrightError as exc:
            _log.warning("launch with channel failed, fallback bundled: %s", exc)
            if user_data is not None:
                self._browser = None
                self._context = await p.chromium.launch_persistent_context(
                    str(user_data),
                    headless=headless,
                    viewport=viewport,
                    args=args,
                )
            else:
                self._browser = await p.chromium.launch(headless=headless, args=args)
                self._context = await self._browser.new_context(viewport=viewport)

        return f"http://127.0.0.1:{cdp_port}"

    async def shutdown(self) -> None:
        self._closing = True
        for t in list(self._subs.values()):
            t.cancel()
        self._subs.clear()
        for page in list(self._tabs.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._tabs.clear()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser context not started")
        return self._context

    async def open_tab(self, url: str, tab_name: Optional[str] = None) -> dict[str, Any]:
        ctx = self._require_context()
        page = await ctx.new_page()
        tid = str(uuid.uuid4())
        self._tabs[tid] = page
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        out: dict[str, Any] = {"tab_id": tid, "url": page.url}
        if tab_name:
            out["tab_name"] = tab_name
        return out

    async def close_tab(self, tab_id: str) -> dict[str, Any]:
        page = self._tabs.pop(tab_id, None)
        if page is None:
            raise ValueError(f"unknown tab_id: {tab_id}")
        await page.close()
        return {"closed": True, "tab_id": tab_id}

    async def goto(self, tab_id: str, url: str) -> dict[str, Any]:
        page = self._tabs.get(tab_id)
        if page is None:
            raise ValueError(f"unknown tab_id: {tab_id}")
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        return {"url": page.url}

    async def query_text(self, tab_id: str, selector: str) -> dict[str, Any]:
        page = self._tabs.get(tab_id)
        if page is None:
            raise ValueError(f"unknown tab_id: {tab_id}")
        loc = page.locator(selector).first
        text = await loc.inner_text(timeout=30_000)
        return {"text": text}

    async def eval_js(self, tab_id: str, script: str, arg: Any = None) -> dict[str, Any]:
        page = self._tabs.get(tab_id)
        if page is None:
            raise ValueError(f"unknown tab_id: {tab_id}")
        value = await page.evaluate(script, arg)
        return {"value": value}

    async def tv_watchlist_init(
        self,
        *,
        chart_url: str,
        tv: dict[str, Any],
        email: Optional[str],
        password: Optional[str],
        initial_settle_ms: int,
    ) -> dict[str, Any]:
        """
        Open a TradingView tab inside the service browser (single CDP owner): goto, login, dark mode, watchlist.
        """
        from automation_tool.coinmap_tradingview_async import (
            maybe_tradingview_dark_mode_async,
            maybe_tradingview_login_async,
            tradingview_ensure_watchlist_open_async,
        )

        ctx = self._require_context()
        page = await ctx.new_page()
        tid = str(uuid.uuid4())
        self._tabs[tid] = page
        await page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
        await maybe_tradingview_login_async(page, tv, email, password)
        await page.wait_for_timeout(max(0, int(initial_settle_ms)))
        await maybe_tradingview_dark_mode_async(page, tv)
        await tradingview_ensure_watchlist_open_async(page, tv)
        return {"tab_id": tid}

    async def tv_watchlist_poll(
        self,
        *,
        tab_id: str,
        tv: dict[str, Any],
        symbol: str,
        timeout_ms: int,
        poll_ms: int,
    ) -> dict[str, Any]:
        from automation_tool.coinmap_tradingview_async import read_watchlist_last_price_wait_stable_async

        page = self._tabs.get(tab_id)
        if page is None:
            raise ValueError(f"unknown tab_id: {tab_id}")
        price = await read_watchlist_last_price_wait_stable_async(
            page,
            tv,
            symbol=str(symbol).strip().upper(),
            timeout_ms=int(timeout_ms),
            poll_ms=int(poll_ms),
        )
        return {"price": price}


class ServiceRuntime:
    def __init__(self) -> None:
        self.st = BrowserServiceState()
        self.server: Optional[Any] = None
        self._stop_evt = asyncio.Event()
        self._stopping = False


async def _handle_one_request(
    rt: ServiceRuntime,
    req: dict[str, Any],
) -> dict[str, Any]:
    method = str(req.get("method") or "")
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    rid = str(req.get("request_id") or "")

    st = rt.st

    if method == METHOD_PING:
        return {"type": "response", "request_id": rid, "ok": True, "result": {"pong": True}, "error": None}

    if method == METHOD_OPEN_TAB:
        r = await st.open_tab(
            url=str(params.get("url") or ""),
            tab_name=(str(params["tab_name"]) if params.get("tab_name") else None),
        )
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_CLOSE_TAB:
        r = await st.close_tab(str(params.get("tab_id") or ""))
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_GOTO:
        r = await st.goto(str(params.get("tab_id") or ""), str(params.get("url") or ""))
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_QUERY_TEXT:
        r = await st.query_text(str(params.get("tab_id") or ""), str(params.get("selector") or ""))
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_EVAL:
        r = await st.eval_js(
            str(params.get("tab_id") or ""),
            str(params.get("script") or ""),
            params.get("arg"),
        )
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_TV_WATCHLIST_INIT:
        tv_raw = params.get("tv")
        if not isinstance(tv_raw, dict):
            raise ValueError("tv_watchlist_init requires params.tv as object")
        r = await st.tv_watchlist_init(
            chart_url=str(params.get("chart_url") or ""),
            tv=tv_raw,
            email=str(params["email"]) if params.get("email") else None,
            password=str(params["password"]) if params.get("password") else None,
            initial_settle_ms=int(params.get("initial_settle_ms") or 3000),
        )
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_TV_WATCHLIST_POLL:
        tv_raw = params.get("tv")
        if not isinstance(tv_raw, dict):
            raise ValueError("tv_watchlist_poll requires params.tv as object")
        r = await st.tv_watchlist_poll(
            tab_id=str(params.get("tab_id") or ""),
            tv=tv_raw,
            symbol=str(params.get("symbol") or ""),
            timeout_ms=int(params.get("timeout_ms") or 10_000),
            poll_ms=int(params.get("poll_ms") or 250),
        )
        return {"type": "response", "request_id": rid, "ok": True, "result": r, "error": None}

    if method == METHOD_CAPTURE_CHARTS:
        # High-level capture: execute in a separate worker process to avoid mixing
        # Playwright async API (service) with sync API (existing capture code).
        #
        # Params are passed as a single JSON object string to the worker.
        import subprocess

        payload = dict(params)
        # If the client didn't specify headless, default to service mode.
        payload.setdefault("headless", bool(os.getenv("PLAYWRIGHT_HEADLESS", "1").strip() not in ("0", "false", "False")))

        cmd = [sys.executable, "-m", "automation_tool.capture_worker", "--payload", json.dumps(payload, ensure_ascii=False)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
        except Exception as e:
            return {
                "type": "response",
                "request_id": rid,
                "ok": False,
                "result": None,
                "error": {"message": f"capture_charts spawn failed: {e}"},
            }

        if proc.returncode != 0:
            msg = (err_b or b"").decode("utf-8", errors="replace").strip() or "capture_worker failed"
            return {"type": "response", "request_id": rid, "ok": False, "result": None, "error": {"message": msg}}

        doc = _parse_capture_worker_stdout_json(out_b)
        return {"type": "response", "request_id": rid, "ok": True, "result": doc, "error": None}

    if method == METHOD_SUBSCRIBE_DOM:
        return {
            "type": "response",
            "request_id": rid,
            "ok": False,
            "result": None,
            "error": {
                "message": "subscribe_dom not implemented in service v1; use query_text/eval from client or attach via cdp_http",
            },
        }

    if method == METHOD_UNSUBSCRIBE:
        return {
            "type": "response",
            "request_id": rid,
            "ok": True,
            "result": {"unsubscribed": str(params.get("sub_id") or "")},
            "error": None,
        }

    if method == METHOD_SHUTDOWN:
        asyncio.get_event_loop().create_task(_graceful_stop(rt))
        return {"type": "response", "request_id": rid, "ok": True, "result": {"shutdown": True}, "error": None}

    raise ValueError(f"unknown method: {method}")


async def _graceful_stop(rt: ServiceRuntime) -> None:
    if rt._stopping:
        return
    rt._stopping = True
    rt._stop_evt.set()
    await asyncio.sleep(0.05)
    try:
        await rt.st.shutdown()
    finally:
        if rt.server is not None:
            rt.server.close()
            await rt.server.wait_closed()
        try:
            p = _state_path()
            if p.is_file():
                p.unlink()
        except OSError:
            pass
        _release_lock()


async def _client_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    rt: ServiceRuntime,
) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = decode_message_line(line + b"\n")
            except Exception as e:
                err = {
                    "type": "response",
                    "request_id": "",
                    "ok": False,
                    "result": None,
                    "error": {"message": str(e)},
                }
                writer.write((json.dumps(err, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                continue
            try:
                resp = await _handle_one_request(rt, req)
                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                if req.get("method") == METHOD_SHUTDOWN:
                    break
            except Exception as e:
                rid = str(req.get("request_id") or "")
                resp = {
                    "type": "response",
                    "request_id": rid,
                    "ok": False,
                    "result": None,
                    "error": {"message": str(e)},
                }
                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "1").strip() not in ("0", "false", "False")
    control_port = _pick_free_port()
    cdp_port = _pick_free_port()
    while cdp_port == control_port:
        cdp_port = _pick_free_port()

    try:
        _acquire_lock()
    except SystemExit:
        raise

    rt = ServiceRuntime()

    try:
        cdp_http = await rt.st.start(headless=headless, cdp_port=cdp_port)
    except Exception:
        _release_lock()
        raise

    default_data_dir().mkdir(parents=True, exist_ok=True)
    state_doc = {
        "pid": os.getpid(),
        "cdp_http": cdp_http,
        "control_tcp": f"127.0.0.1:{control_port}",
        "user_data_dir": str(chrome_user_data_dir_from_env() or ""),
    }
    _state_path().write_text(json.dumps(state_doc, indent=2), encoding="utf-8")

    ready = {
        "type": "ready",
        "pid": os.getpid(),
        "cdp_http": cdp_http,
        "control_tcp": state_doc["control_tcp"],
    }
    print(json.dumps(ready, ensure_ascii=False), flush=True)

    server = await asyncio.start_server(
        lambda r, w: _client_handler(r, w, rt),
        host="127.0.0.1",
        port=control_port,
    )
    rt.server = server

    loop = asyncio.get_event_loop()

    def _schedule_stop(*_args: Any) -> None:
        asyncio.create_task(_graceful_stop(rt))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _schedule_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_a: _schedule_stop())
            except (OSError, ValueError):
                pass

    async with server:
        await rt._stop_evt.wait()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
