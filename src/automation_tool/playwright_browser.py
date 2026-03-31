from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Playwright

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
    """Launch Chrome with a fresh temporary profile (isolated from your daily Chrome)."""
    return p.chromium.launch(channel=_CHROME_CHANNEL, headless=headless)


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

    user_data = chrome_user_data_dir_from_env()
    if user_data is not None:
        user_data.mkdir(parents=True, exist_ok=True)
        # launch_persistent_context does not accept storage_state in this Playwright API;
        # session data lives in user_data_dir only.
        ctx = p.chromium.launch_persistent_context(
            str(user_data),
            channel=_CHROME_CHANNEL,
            headless=headless,
            viewport=viewport,
        )
        return None, ctx

    ss = str(storage_state_path) if storage_state_path and storage_state_path.exists() else None
    browser = p.chromium.launch(channel=_CHROME_CHANNEL, headless=headless)
    ctx = browser.new_context(viewport=viewport, storage_state=ss)
    return browser, ctx


def close_browser_and_context(browser: Optional[Browser], context: BrowserContext) -> None:
    context.close()
    if browser is not None:
        browser.close()
