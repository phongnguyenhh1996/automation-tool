from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Playwright

# Google Chrome installed on the system (not Playwright's bundled Chromium).
_CHROME_CHANNEL = "chrome"


def chrome_user_data_dir_from_env() -> Optional[Path]:
    """
    If ``PLAYWRIGHT_CHROME_USER_DATA_DIR`` is set, Playwright uses ``launch_persistent_context``
    so cookies, local storage, and logins persist in that folder (like a real Chrome profile).

    Use a **dedicated** directory (e.g. ``data/chrome_user_data``), not your main Chrome
    profile while everyday Chrome is open — that can corrupt the profile.
    """
    raw = os.getenv("PLAYWRIGHT_CHROME_USER_DATA_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def launch_chrome(p: Playwright, *, headless: bool) -> Browser:
    """
    Launch a Chromium-based browser with a fresh temporary profile.

    Preference order:
    1) Channel from PLAYWRIGHT_CHANNEL or "chrome"
    2) Bundled Playwright Chromium (fallback when channel is unavailable)
    """
    channel = (os.getenv("PLAYWRIGHT_CHANNEL") or _CHROME_CHANNEL).strip()
    try:
        return p.chromium.launch(channel=channel, headless=headless)
    except PlaywrightError as exc:
        # Common on locked-down VPS where Chrome/Edge channel is missing.
        print(
            f"[playwright_browser] channel='{channel}' unavailable; "
            f"falling back to bundled Chromium. Details: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return p.chromium.launch(headless=headless)


def launch_chrome_context(
    p: Playwright,
    *,
    headless: bool,
    storage_state_path: Optional[Path],
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> Tuple[Optional[Browser], BrowserContext]:
    """
    Return ``(browser, context)``. If using a persistent user-data dir from env,
    ``browser`` is ``None`` and only ``context`` must be closed.

    ``storage_state_path`` is applied only for the non-persistent path. Persistent mode
    ignores it; cookies and logins are stored only under the user-data directory.

    Without ``PLAYWRIGHT_CHROME_USER_DATA_DIR``, each run uses a **new empty profile**:
    same Chrome **app**, but not your usual bookmarks/extensions/logins — that is why
    it can feel like “another browser”.
    """
    viewport = {"width": viewport_width, "height": viewport_height}

    channel = (os.getenv("PLAYWRIGHT_CHANNEL") or _CHROME_CHANNEL).strip()
    user_data = chrome_user_data_dir_from_env()
    if user_data is not None:
        user_data.mkdir(parents=True, exist_ok=True)
        # launch_persistent_context does not accept storage_state in this Playwright API;
        # session data lives in user_data_dir only.
        try:
            ctx = p.chromium.launch_persistent_context(
                str(user_data),
                channel=channel,
                headless=headless,
                viewport=viewport,
            )
        except PlaywrightError as exc:
            print(
                f"[playwright_browser] persistent channel='{channel}' unavailable; "
                f"falling back to bundled Chromium. Details: {exc}",
                file=sys.stderr,
                flush=True,
            )
            ctx = p.chromium.launch_persistent_context(
                str(user_data),
                headless=headless,
                viewport=viewport,
            )
        return None, ctx

    ss = str(storage_state_path) if storage_state_path and storage_state_path.exists() else None
    try:
        browser = p.chromium.launch(channel=channel, headless=headless)
    except PlaywrightError as exc:
        print(
            f"[playwright_browser] channel='{channel}' unavailable; "
            f"falling back to bundled Chromium. Details: {exc}",
            file=sys.stderr,
            flush=True,
        )
        browser = p.chromium.launch(headless=headless)
    ctx = browser.new_context(viewport=viewport, storage_state=ss)
    return browser, ctx


def close_browser_and_context(browser: Optional[Browser], context: BrowserContext) -> None:
    context.close()
    if browser is not None:
        browser.close()
