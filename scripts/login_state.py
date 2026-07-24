#!/usr/bin/env python3
"""Open a dedicated login and save a private candidate Playwright state."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
    )
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
    )
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

if __package__:
    from .create_state import _credential_output_path, _secure_write_json
    from .spider import BASE_URL, DependencyError
else:
    from create_state import _credential_output_path, _secure_write_json
    from spider import BASE_URL, DependencyError


def _is_authenticated(cookies: list[dict[str, Any]]) -> bool:
    return any(
        cookie.get("name") == "cookie2" and bool(cookie.get("value"))
        for cookie in cookies
        if isinstance(cookie, dict)
    )


def capture_login_state(
    output_file: str,
    *,
    browser_channel: str | None,
    timeout_seconds: int,
    force: bool,
) -> Path:
    if sync_playwright is None:
        raise DependencyError(
            "playwright is not installed; run: "
            "python -m pip install -r requirements.txt"
        )
    if timeout_seconds < 1:
        raise ValueError("timeout must be at least 1 second")
    output = _credential_output_path(output_file)
    if output.is_symlink():
        raise ValueError(f"refusing to write login state through a symlink: {output}")
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": False}
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(
                    BASE_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except PlaywrightTimeoutError:
                pass

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if _is_authenticated(context.cookies([BASE_URL])):
                    page.wait_for_timeout(2_000)
                    state = context.storage_state()
                    return _secure_write_json(str(output), state, force)
                page.wait_for_timeout(1_000)
        finally:
            browser.close()

    raise TimeoutError("timed out waiting for Xianyu login")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Log in to Xianyu and save private candidate Playwright state"
    )
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument(
        "--browser-channel",
        default=os.getenv("XIANYU_BROWSER_CHANNEL"),
        help="Playwright browser channel, for example chrome",
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="login timeout seconds"
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        json.dumps(
            {
                "status": "waiting-for-login",
                "site": "goofish.com",
                "output": str(Path(args.output).expanduser()),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    try:
        output = capture_login_state(
            args.output,
            browser_channel=args.browser_channel,
            timeout_seconds=args.timeout,
            force=args.force,
        )
    except (
        DependencyError,
        OSError,
        TimeoutError,
        ValueError,
        PlaywrightError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=True,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "status": "candidate-state-saved",
                "output": str(output),
                "verification": "run one controlled search",
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
