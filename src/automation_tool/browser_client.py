"""
Client for Browser worker: TCP control plane + helpers to attach via CDP.

State file: data/browser_service_state.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

from automation_tool.browser_protocol import encode_message
from automation_tool.config import default_data_dir

_STATE_FILENAME = "browser_service_state.json"
_log = logging.getLogger(__name__)


def browser_service_state_path() -> Path:
    return default_data_dir() / _STATE_FILENAME


def load_browser_service_state() -> Optional[dict[str, Any]]:
    p = browser_service_state_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def control_address_from_state(state: dict[str, Any]) -> Optional[Tuple[str, int]]:
    raw = str(state.get("control_tcp") or "").strip()
    if not raw:
        return None
    m = re.match(r"^(.+):(\d+)$", raw)
    if not m:
        return None
    return m.group(1), int(m.group(2))


class BrowserClient:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    @classmethod
    def from_state_file(cls) -> Optional[BrowserClient]:
        st = load_browser_service_state()
        if not st:
            return None
        addr = control_address_from_state(st)
        if not addr:
            return None
        return cls(addr[0], addr[1])

    def request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout_s: float = 120.0) -> dict[str, Any]:
        payload = {
            "type": "request",
            "request_id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
        line = encode_message(payload)
        sock = socket.create_connection((self._host, self._port), timeout=timeout_s)
        try:
            sock.settimeout(timeout_s)
            sock.sendall(line)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf.strip():
                raise RuntimeError("empty response from browser service")
            return json.loads(buf.decode("utf-8").strip())
        finally:
            sock.close()

    def ping(self) -> bool:
        r = self.request("ping", {}, timeout_s=5.0)
        return bool(r.get("ok")) and (r.get("result") or {}).get("pong") is True

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {}, timeout_s=30.0)
        except OSError:
            pass


def try_attach_playwright_via_service(
    p: Any,
    *,
    force: bool = False,
) -> Optional[Tuple[Any, Any]]:
    """
    If browser_service_state.json exists and cdp_http is reachable, connect_over_cdp.

    Returns (browser, default_context) or None to use normal launch_chrome_context instead.

    Sync Playwright API: ``browser.contexts[0]`` is the first context (persistent profile).

    Default behavior: attach whenever service state exists and is reachable.

    Set ``AUTOMATION_USE_BROWSER_SERVICE=0`` (or ``false``/``no``) to disable auto-attach.
    When ``force`` is True (e.g. ``capture --use-service``), attach if state exists even
    if the env var disables auto-attach.
    """
    st = load_browser_service_state()
    if not st:
        _log.info("playwright-attach skip | reason=no_state_file | state_path=%s", browser_service_state_path())
        return None

    # Best-effort health check via control plane (more reliable signal than CDP url alone).
    # If this fails, the state file is likely stale (service crashed/restarted, ports changed).
    try:
        addr = control_address_from_state(st)
        if addr is not None:
            c = BrowserClient(addr[0], addr[1])
            if not c.ping():
                _log.warning(
                    "playwright-attach skip | reason=service_not_responding | control_tcp=%s:%d | state_path=%s",
                    addr[0],
                    addr[1],
                    browser_service_state_path(),
                )
                return None
    except Exception as e:
        _log.warning(
            "playwright-attach skip | reason=service_ping_failed | err=%s | state_path=%s",
            str(e),
            browser_service_state_path(),
            exc_info=True,
        )
        return None

    url = str(st.get("cdp_http") or "").strip()
    if not url:
        _log.warning(
            "playwright-attach skip | reason=missing_cdp_http | state_path=%s keys=%s",
            browser_service_state_path(),
            sorted(list(st.keys())),
        )
        return None
    if not force:
        v = os.getenv("AUTOMATION_USE_BROWSER_SERVICE", "").strip().lower()
        if v in ("0", "false", "no", "off"):
            _log.info(
                "playwright-attach skip | reason=disabled_by_env | env.AUTOMATION_USE_BROWSER_SERVICE=%r | cdp_http=%s",
                v,
                url,
            )
            return None
    try:
        _log.info(
            "playwright-attach attempt | cdp_http=%s | force=%s | state_path=%s",
            url,
            force,
            browser_service_state_path(),
        )
        browser = p.chromium.connect_over_cdp(url)
        if not browser.contexts:
            _log.warning("playwright-attach failed | reason=no_contexts | cdp_http=%s", url)
            return None
        context = browser.contexts[0]
        _log.info(
            "playwright-attach ok | cdp_http=%s | contexts=%d",
            url,
            len(browser.contexts),
        )
        return browser, context
    except Exception as e:
        _log.warning("playwright-attach failed | cdp_http=%s | err=%s", url, str(e), exc_info=True)
        return None


def spawn_browser_service_detached(*, cwd: Optional[Path] = None) -> subprocess.Popen:
    """
    Start ``python -m automation_tool.browser_service`` in a new session (POSIX),
    inheriting env (PLAYWRIGHT_CHROME_USER_DATA_DIR, etc.).
    """
    cmd = [sys.executable, "-m", "automation_tool.browser_service"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": True,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    return subprocess.Popen(cmd, **kwargs)


def is_service_responding() -> bool:
    c = BrowserClient.from_state_file()
    if not c:
        return False
    try:
        return c.ping()
    except OSError:
        return False


def wait_for_state_file(*, timeout_s: float = 60.0, poll_s: float = 0.25) -> Optional[dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = load_browser_service_state()
        if st and st.get("cdp_http"):
            return st
        time.sleep(poll_s)
    return None
