#!/usr/bin/env python3
"""Search Xianyu with Playwright and return normalized JSON results."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

try:
    from playwright.async_api import (
        BrowserContext,
        Page,
        Playwright,
        Route,
        async_playwright,
    )
    from playwright.async_api import (
        Error as PlaywrightError,
    )
    from playwright.async_api import (
        TimeoutError as PlaywrightTimeoutError,
    )
except ImportError:  # Keep the module importable for setup/help commands.
    BrowserContext = Any
    Page = Any
    Playwright = Any
    Route = Any
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


BASE_URL = "https://www.goofish.com"
SEARCH_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
SEARCH_API_ROUTE = f"**{SEARCH_API_FRAGMENT}**"
NEXT_PAGE_SELECTOR = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
DEFAULT_TIMEOUT_MS = 30_000
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class SpiderError(RuntimeError):
    """Base error for a user-actionable spider failure."""


class DependencyError(SpiderError):
    """Raised when a required runtime dependency is missing."""


class StateFileError(SpiderError):
    """Raised when a login-state file is invalid."""


class SearchRejectedError(SpiderError):
    """Raised when Xianyu rejects a search request."""


@dataclass(frozen=True)
class CapturedSearchResponse:
    """A search response captured before it reaches the page."""

    status: int | None
    payload: dict[str, Any] | None
    error: str | None = None


class RateLimiter:
    """Keep browser actions separated by a configurable interval."""

    def __init__(self, min_delay: float = 2.0, max_delay: float = 5.0):
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("invalid rate-limit interval")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_action = 0.0

    async def wait(self) -> None:
        import random

        delay = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last_action
        if self._last_action and elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_action = time.monotonic()


class SearchResponseCollector:
    """Capture only the exact POST search endpoint.

    Routing the request keeps the response body available even when Chromium's
    DevTools response cache discards it before an asynchronous event callback
    reads it.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[CapturedSearchResponse] = asyncio.Queue()

    async def install(self, context: BrowserContext) -> None:
        await context.route(SEARCH_API_ROUTE, self._handle_route)

    async def _handle_route(self, route: Route) -> None:
        request = route.request
        if request.method.upper() != "POST":
            await route.continue_()
            return

        try:
            response = await route.fetch()
            body = await response.body()
            await route.fulfill(response=response, body=body)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await self._queue.put(
                    CapturedSearchResponse(
                        status=response.status,
                        payload=None,
                        error=f"search API returned invalid JSON: {exc}",
                    )
                )
                return

            await self._queue.put(
                CapturedSearchResponse(status=response.status, payload=payload)
            )
        except PlaywrightError as exc:
            try:
                await route.continue_()
            except PlaywrightError:
                pass
            await self._queue.put(
                CapturedSearchResponse(
                    status=None,
                    payload=None,
                    error=f"failed to capture search API response: {exc}",
                )
            )

    async def next(
        self, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> CapturedSearchResponse:
        try:
            return await asyncio.wait_for(
                self._queue.get(), timeout=max(timeout_ms, 1) / 1000
            )
        except TimeoutError as exc:
            raise SpiderError("timed out waiting for Xianyu search API") from exc


def _redact_proxy(proxy: str) -> str:
    """Return a proxy label that never includes credentials."""

    parsed = urlsplit(proxy)
    if not parsed.hostname:
        return "<configured proxy>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}{host}{port}"


def _load_state_file(
    state_file: str | None,
) -> tuple[dict[str, Any] | str | None, dict[str, Any], dict[str, str]]:
    """Load standard Playwright state or an enhanced browser snapshot."""

    if not state_file:
        return None, {}, {}

    path = Path(state_file).expanduser()
    if not path.is_file():
        raise StateFileError(f"login state not found: {path}")

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateFileError(f"invalid login state {path}: {exc}") from exc

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cookies"), list):
        raise StateFileError(
            "login state must be a JSON object containing a cookies array"
        )

    # Standard Playwright storage state can be passed directly.
    enhanced = any(key in snapshot for key in ("env", "headers", "page", "storage"))
    if not enhanced:
        return snapshot, {}, {}

    storage = snapshot.get("storage")
    origins = snapshot.get("origins", [])
    if isinstance(storage, dict) and isinstance(storage.get("origins"), list):
        origins = storage["origins"]
    storage_state = {"cookies": snapshot["cookies"], "origins": origins}

    env = snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {}
    headers = (
        snapshot.get("headers") if isinstance(snapshot.get("headers"), dict) else {}
    )
    navigator = env.get("navigator") if isinstance(env.get("navigator"), dict) else {}
    screen = env.get("screen") if isinstance(env.get("screen"), dict) else {}
    intl = env.get("intl") if isinstance(env.get("intl"), dict) else {}

    overrides: dict[str, Any] = {}
    user_agent = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if isinstance(user_agent, str) and user_agent:
        overrides["user_agent"] = user_agent

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    if isinstance(accept_language, str) and accept_language:
        overrides["locale"] = accept_language.split(",", 1)[0].strip()
    elif isinstance(navigator.get("language"), str):
        overrides["locale"] = navigator["language"]

    timezone = intl.get("timeZone")
    if isinstance(timezone, str) and timezone:
        overrides["timezone_id"] = timezone

    width, height = screen.get("width"), screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    scale = screen.get("devicePixelRatio")
    if isinstance(scale, (int, float)) and scale > 0:
        overrides["device_scale_factor"] = float(scale)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    if isinstance(user_agent, str):
        lowered = user_agent.lower()
        overrides["is_mobile"] = any(
            marker in lowered for marker in ("mobile", "android", "iphone")
        )

    allowed_headers = {"accept", "accept-language", "cache-control", "pragma"}
    safe_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if isinstance(key, str)
        and value is not None
        and not key.startswith(":")
        and key.lower() in allowed_headers
    }
    return storage_state, overrides, safe_headers


def _device_context(playwright: Playwright) -> dict[str, Any]:
    """Use a coherent built-in device profile instead of mismatched random UAs."""

    device = dict(playwright.devices["Pixel 7"])
    device.pop("default_browser_type", None)
    device.update(
        {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "light",
        }
    )
    return device


class XianyuSpider:
    """Search Xianyu using a browser login state."""

    def __init__(
        self,
        state_file: str | None = None,
        proxy: str | None = None,
        *,
        headless: bool = True,
        browser_channel: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self.state_file = state_file
        self.proxy = proxy
        self.headless = headless
        self.browser_channel = browser_channel
        self.timeout_ms = timeout_ms
        self.rate_limiter = RateLimiter()
        self.pages_scraped = 0
        self.debug = False

    async def search(
        self,
        keyword: str,
        max_price: float | None = None,
        min_price: float | None = None,
        location: str | None = None,
        pages: int = 1,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """Search, paginate, normalize, deduplicate, and locally filter items."""

        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword must not be empty")
        if pages < 1:
            raise ValueError("pages must be at least 1")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if min_price is not None and min_price < 0:
            raise ValueError("min_price must not be negative")
        if max_price is not None and max_price < 0:
            raise ValueError("max_price must not be negative")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("min_price must not exceed max_price")

        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                items = await self._search_once(keyword, pages)
                return self._filter_items(
                    items,
                    min_price=min_price,
                    max_price=max_price,
                    location=location,
                )
            except (SearchRejectedError, StateFileError, DependencyError):
                raise
            except (PlaywrightError, SpiderError) as exc:
                last_error = exc
                if attempt >= max_retries - 1:
                    break
                wait_seconds = min(5 * (2**attempt), 30)
                print(
                    f"[retry] search failed; retrying in {wait_seconds}s "
                    f"({attempt + 1}/{max_retries}): {exc}",
                    file=sys.stderr,
                )
                await asyncio.sleep(wait_seconds)

        raise SpiderError(
            f"search failed after {max_retries} attempt(s): {last_error}"
        ) from last_error

    async def _search_once(self, keyword: str, pages: int) -> list[dict[str, Any]]:
        if async_playwright is None:
            raise DependencyError(
                "playwright is not installed; run: "
                "python -m pip install -r requirements.txt && "
                "python -m playwright install chromium"
            )

        storage_state, context_overrides, extra_headers = _load_state_file(
            self.state_file
        )
        if not storage_state:
            print(
                "[warning] no login state supplied; Xianyu may require authentication",
                file=sys.stderr,
            )

        self.pages_scraped = 0
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        async with async_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
                print(f"[proxy] using {_redact_proxy(self.proxy)}", file=sys.stderr)
            if self.browser_channel:
                launch_kwargs["channel"] = self.browser_channel

            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context_kwargs = _device_context(playwright)
                context_kwargs.update(context_overrides)
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                if extra_headers:
                    context_kwargs["extra_http_headers"] = extra_headers

                context = await browser.new_context(**context_kwargs)
                collector = SearchResponseCollector()
                await collector.install(context)
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                search_url = f"{BASE_URL}/search?{urlencode({'q': keyword})}"
                print(f"[search] {keyword!r}, page 1/{pages}", file=sys.stderr)
                await page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                capture = await collector.next(self.timeout_ms)

                for page_number in range(1, pages + 1):
                    page_items = self._parse_capture(capture)
                    self.pages_scraped += 1
                    for item in page_items:
                        item_id = item["id"]
                        if item_id not in seen_ids:
                            seen_ids.add(item_id)
                            items.append(item)

                    if page_number >= pages:
                        break
                    capture = await self._advance_page(
                        page, collector, page_number + 1, pages
                    )
                    if capture is None:
                        break
            finally:
                await browser.close()

        return items

    async def _advance_page(
        self,
        page: Page,
        collector: SearchResponseCollector,
        page_number: int,
        total_pages: int,
    ) -> CapturedSearchResponse | None:
        next_button = page.locator(NEXT_PAGE_SELECTOR).first
        if not await next_button.count():
            print("[pagination] reached the last page", file=sys.stderr)
            return None

        await self.rate_limiter.wait()
        print(f"[search] requesting page {page_number}/{total_pages}", file=sys.stderr)
        capture_task = asyncio.create_task(collector.next(self.timeout_ms))
        try:
            await next_button.scroll_into_view_if_needed()
            await next_button.click(timeout=self.timeout_ms)
            return await capture_task
        except Exception:
            capture_task.cancel()
            try:
                await capture_task
            except (asyncio.CancelledError, SpiderError):
                pass
            raise

    def _parse_capture(self, capture: CapturedSearchResponse) -> list[dict[str, Any]]:
        if capture.error:
            raise SpiderError(capture.error)
        if capture.status is None or capture.status >= 400:
            raise SearchRejectedError(
                f"search API returned HTTP {capture.status or 'unknown'}"
            )
        if not capture.payload:
            raise SpiderError("search API returned an empty payload")

        ret = capture.payload.get("ret", [])
        ret_values = ret if isinstance(ret, list) else [ret]
        failures = [
            str(value)
            for value in ret_values
            if value and not str(value).upper().startswith("SUCCESS")
        ]
        if failures:
            raise SearchRejectedError("; ".join(failures))

        result_list = capture.payload.get("data", {}).get("resultList", [])
        if not isinstance(result_list, list):
            raise SpiderError("search API resultList is not a list")

        parsed: list[dict[str, Any]] = []
        for wrapper in result_list:
            if not isinstance(wrapper, dict):
                continue
            item = self._parse_api_item(wrapper)
            if item:
                parsed.append(item)
        return parsed

    @staticmethod
    def _filter_items(
        items: list[dict[str, Any]],
        *,
        min_price: float | None,
        max_price: float | None,
        location: str | None,
    ) -> list[dict[str, Any]]:
        location_query = location.strip().casefold() if location else None
        filtered: list[dict[str, Any]] = []
        for item in items:
            price = item.get("price")
            if min_price is not None and (
                not isinstance(price, (int, float)) or price < min_price
            ):
                continue
            if max_price is not None and (
                not isinstance(price, (int, float)) or price > max_price
            ):
                continue
            if (
                location_query
                and location_query not in str(item.get("location", "")).casefold()
            ):
                continue
            filtered.append(item)
        return filtered

    def _parse_api_item(self, wrapper: dict[str, Any]) -> dict[str, Any] | None:
        try:
            main = wrapper.get("data", {}).get("item", {}).get("main", {})
            ex_content = main.get("exContent", {})
            click_args = main.get("clickParam", {}).get("args", {})
            if not isinstance(ex_content, dict):
                return None

            item_id = str(ex_content.get("itemId") or "").strip()
            if not item_id:
                return None

            price_parts = ex_content.get("price", [])
            if isinstance(price_parts, list):
                price_text = "".join(
                    str(part.get("text", ""))
                    for part in price_parts
                    if isinstance(part, dict)
                )
            else:
                price_text = str(price_parts or "")

            raw_link = str(main.get("targetUrl") or "")
            url = raw_link.replace("fleamarket://", f"{BASE_URL}/", 1)
            if not url:
                url = f"{BASE_URL}/item?id={item_id}"

            published = self._format_timestamp(click_args.get("publishTime"))
            tags: list[str] = []
            if click_args.get("tag") == "freeship":
                tags.append("包邮")
            tag_list = ex_content.get("fishTags", {}).get("r1", {}).get("tagList", [])
            if isinstance(tag_list, list):
                for tag_item in tag_list:
                    if not isinstance(tag_item, dict):
                        continue
                    content = str(tag_item.get("data", {}).get("content", ""))
                    if "验货宝" in content and "验货宝" not in tags:
                        tags.append("验货宝")

            return {
                "id": item_id,
                "title": str(ex_content.get("title") or ""),
                "price": self._parse_price(price_text),
                "url": url,
                "image": str(ex_content.get("picUrl") or ""),
                "location": str(ex_content.get("area") or ""),
                "seller": str(ex_content.get("userNickName") or ""),
                "publish_time": published,
                "wants": click_args.get("wantNum", 0),
                "tags": tags,
            }
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_price(price_text: str) -> int | float | None:
        import re

        match = re.search(r"\d+(?:\.\d+)?", price_text.replace(",", ""))
        if not match:
            return None
        value = float(match.group(0))
        return int(value) if value.is_integer() else value

    @staticmethod
    def _format_timestamp(raw_timestamp: Any) -> str:
        try:
            timestamp = int(raw_timestamp)
        except (TypeError, ValueError):
            return ""
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=SHANGHAI_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Xianyu and emit JSON")
    parser.add_argument("--keyword", "-k", required=True, help="search keyword")
    parser.add_argument("--max-price", type=float, help="maximum price")
    parser.add_argument("--min-price", type=float, help="minimum price")
    parser.add_argument("--location", help="location substring")
    parser.add_argument("--pages", "-p", type=int, default=1, help="pages to fetch")
    parser.add_argument("--state", "-s", help="Playwright login-state JSON")
    parser.add_argument("--proxy", help="HTTP(S) or SOCKS proxy URL")
    parser.add_argument(
        "--browser-channel",
        default=os.getenv("XIANYU_BROWSER_CHANNEL"),
        help="Playwright browser channel, for example chrome",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--debug", action="store_true", help="enable debug metadata")
    parser.add_argument("--retries", "-r", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spider = XianyuSpider(
        state_file=args.state,
        proxy=args.proxy,
        headless=not args.headed,
        browser_channel=args.browser_channel,
    )
    spider.debug = args.debug

    try:
        results = asyncio.run(
            spider.search(
                keyword=args.keyword,
                max_price=args.max_price,
                min_price=args.min_price,
                location=args.location,
                pages=args.pages,
                max_retries=args.retries,
            )
        )
    except (SpiderError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "keyword": args.keyword,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 2

    payload: dict[str, Any] = {
        "ok": True,
        "keyword": args.keyword,
        "count": len(results),
        "pages_scraped": spider.pages_scraped,
        "items": results,
    }
    if args.debug:
        payload["filters"] = {
            "min_price": args.min_price,
            "max_price": args.max_price,
            "location": args.location,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
