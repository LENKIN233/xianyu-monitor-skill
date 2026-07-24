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
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

try:
    from playwright.async_api import (
        BrowserContext,
        Page,
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
PAGINATION_WAIT_MS = 5_000
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class SpiderError(RuntimeError):
    """Base error for a user-actionable spider failure."""


class DependencyError(SpiderError):
    """Raised when a required runtime dependency is missing."""


class StateFileError(SpiderError):
    """Raised when a login-state file is invalid."""


class SearchRejectedError(SpiderError):
    """Raised when Xianyu rejects a search request."""


class SearchCaptureError(SpiderError):
    """Raised when a page does not emit the expected search request."""


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
    """Capture only the exact GET or POST search endpoint.

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
        if request.method.upper() not in {"GET", "POST"}:
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
            if not isinstance(payload, dict):
                await self._queue.put(
                    CapturedSearchResponse(
                        status=response.status,
                        payload=None,
                        error="search API returned a non-object JSON payload",
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
            raise SearchCaptureError("timed out waiting for Xianyu search API") from exc


def build_proxy_settings(proxy: str) -> dict[str, str]:
    """Convert a proxy URL into Playwright's credential-safe launch shape."""

    raw = proxy.strip()
    if not raw:
        raise ValueError("proxy URL must not be empty")
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError("proxy scheme must be http, https, or socks5")
    if not parsed.hostname:
        raise ValueError("proxy URL must include a host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL must not include a path, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL has an invalid port") from exc

    has_credentials = parsed.username is not None or parsed.password is not None
    if scheme == "socks5" and has_credentials:
        raise ValueError("Playwright does not support authenticated SOCKS5 proxies")

    server_netloc = parsed.netloc.rsplit("@", 1)[-1]
    settings = {"server": f"{scheme}://{server_netloc}"}
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        settings["password"] = unquote(parsed.password)
    return settings


def _redact_proxy(proxy: str) -> str:
    """Return a proxy label that never includes credentials."""

    try:
        return build_proxy_settings(proxy)["server"]
    except ValueError:
        return "<configured proxy>"


def resolve_proxy(proxy: str | None, proxy_file: str | None = None) -> str | None:
    """Resolve proxy input without requiring credentials in process arguments."""

    if proxy:
        return proxy
    if proxy_file:
        path = Path(proxy_file).expanduser()
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"proxy file is empty: {path.resolve()}")
        return value
    value = os.getenv("XIANYU_PROXY", "").strip()
    return value or None


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
    intl = env.get("intl") if isinstance(env.get("intl"), dict) else {}

    overrides: dict[str, Any] = {}
    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    if isinstance(accept_language, str) and accept_language:
        overrides["locale"] = accept_language.split(",", 1)[0].strip()
    elif isinstance(navigator.get("language"), str):
        overrides["locale"] = navigator["language"]

    timezone = intl.get("timeZone")
    if isinstance(timezone, str) and timezone:
        overrides["timezone_id"] = timezone

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


def _default_context() -> dict[str, Any]:
    """Use a desktop context that matches Xianyu's PC search API."""

    return {
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "color_scheme": "light",
        "viewport": {"width": 1440, "height": 900},
    }


def _context_options(
    storage_state: dict[str, Any] | str | None,
    overrides: dict[str, Any],
    extra_headers: dict[str, str],
) -> dict[str, Any]:
    options = _default_context()
    # The captured endpoint is the PC search API. Preserve only regional
    # metadata from enhanced snapshots so a legacy mobile snapshot cannot
    # silently switch the browser back to the incompatible mobile route.
    options.update(
        {key: overrides[key] for key in ("locale", "timezone_id") if key in overrides}
    )
    if storage_state:
        options["storage_state"] = storage_state
    if extra_headers:
        options["extra_http_headers"] = extra_headers
    # Playwright routes cannot reliably observe requests handled by a service
    # worker, so blocking workers is part of the collector contract.
    options["service_workers"] = "block"
    return options


def _safe_page_location(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return "<unknown page>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_search_navigation(
    *,
    url: str,
    status: int | None,
    has_state: bool,
) -> None:
    if status is not None and status >= 400:
        raise SearchRejectedError(f"Xianyu search page returned HTTP {status}")

    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if hostname == "passport.goofish.com" or "login" in path:
        if has_state:
            raise StateFileError("Xianyu rejected the supplied login state")
        raise StateFileError("Xianyu login is required; provide --state")
    if hostname != "www.goofish.com" or not path.startswith("/search"):
        raise SearchRejectedError(
            f"unexpected Xianyu search navigation: {_safe_page_location(url)}"
        )


async def _navigate_to_search(
    page: Page,
    *,
    search_url: str,
    timeout_ms: int,
    has_state: bool,
    headless: bool,
) -> None:
    try:
        navigation = await page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        _validate_search_navigation(
            url=page.url,
            status=None,
            has_state=has_state,
        )
        headed_hint = "; retry once with --headed" if headless else ""
        raise SearchCaptureError(
            "timed out loading Xianyu search page; "
            f"final page: {_safe_page_location(page.url)}"
            f"{headed_hint}"
        ) from exc

    _validate_search_navigation(
        url=page.url,
        status=navigation.status if navigation else None,
        has_state=has_state,
    )


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
        verbose: bool = True,
    ):
        self.state_file = state_file
        self.proxy_settings = build_proxy_settings(proxy) if proxy else None
        self.headless = headless
        self.browser_channel = browser_channel
        self.timeout_ms = timeout_ms
        self.verbose = verbose
        self.rate_limiter = RateLimiter()
        self.pages_scraped = 0
        self.debug = False

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)

    def _safe_error_message(self, error: Exception) -> str:
        message = str(error)
        if self.proxy_settings:
            for field in ("username", "password"):
                credential = self.proxy_settings.get(field)
                if credential:
                    message = message.replace(credential, "<redacted>")
        return message

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

        last_error: str | None = None
        for attempt in range(max_retries):
            try:
                items = await self._search_once(keyword, pages)
                return self._filter_items(
                    items,
                    min_price=min_price,
                    max_price=max_price,
                    location=location,
                )
            except (
                SearchCaptureError,
                SearchRejectedError,
                StateFileError,
                DependencyError,
            ):
                raise
            except (PlaywrightError, SpiderError) as exc:
                last_error = self._safe_error_message(exc)
                if attempt >= max_retries - 1:
                    break
                wait_seconds = min(5 * (2**attempt), 30)
                self._log(
                    f"[retry] search failed; retrying in {wait_seconds}s "
                    f"({attempt + 1}/{max_retries}): {last_error}"
                )
                await asyncio.sleep(wait_seconds)

        raise SpiderError(
            f"search failed after {max_retries} attempt(s): {last_error}"
        ) from None

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
            self._log(
                "[warning] no login state supplied; Xianyu may require authentication"
            )

        self.pages_scraped = 0
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        async with async_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {"headless": self.headless}
            if self.proxy_settings:
                launch_kwargs["proxy"] = self.proxy_settings
                self._log(f"[proxy] using {self.proxy_settings['server']}")
            if self.browser_channel:
                launch_kwargs["channel"] = self.browser_channel

            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context_kwargs = _context_options(
                    storage_state,
                    context_overrides,
                    extra_headers,
                )

                context = await browser.new_context(**context_kwargs)
                collector = SearchResponseCollector()
                await collector.install(context)
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                search_url = f"{BASE_URL}/search?{urlencode({'q': keyword})}"
                self._log(f"[search] {keyword!r}, page 1/{pages}")
                await _navigate_to_search(
                    page,
                    search_url=search_url,
                    timeout_ms=self.timeout_ms,
                    has_state=bool(storage_state),
                    headless=self.headless,
                )
                try:
                    capture = await collector.next(self.timeout_ms)
                except SearchCaptureError as exc:
                    _validate_search_navigation(
                        url=page.url,
                        status=None,
                        has_state=bool(storage_state),
                    )
                    headed_hint = (
                        "; Xianyu may suppress search requests in headless mode; "
                        "retry once with --headed"
                        if self.headless
                        else ""
                    )
                    raise SearchCaptureError(
                        f"{exc}; final page: {_safe_page_location(page.url)}"
                        f"{headed_hint}"
                    ) from exc

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
        try:
            await next_button.wait_for(
                state="visible",
                timeout=min(self.timeout_ms, PAGINATION_WAIT_MS),
            )
        except PlaywrightTimeoutError:
            self._log("[pagination] reached the last page")
            return None

        await self.rate_limiter.wait()
        self._log(f"[search] requesting page {page_number}/{total_pages}")
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
            raise SearchCaptureError(capture.error)
        if capture.status is None or capture.status >= 400:
            raise SearchRejectedError(
                f"search API returned HTTP {capture.status or 'unknown'}"
            )
        if not capture.payload:
            raise SearchCaptureError("search API returned an empty payload")

        ret = capture.payload.get("ret", [])
        ret_values = ret if isinstance(ret, list) else [ret]
        failures = [
            str(value)
            for value in ret_values
            if value and not str(value).upper().startswith("SUCCESS")
        ]
        if failures:
            raise SearchRejectedError("; ".join(failures))

        data = capture.payload.get("data")
        if not isinstance(data, dict):
            raise SearchCaptureError("search API data is not an object")
        result_list = data.get("resultList", [])
        if not isinstance(result_list, list):
            raise SearchCaptureError("search API resultList is not a list")

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
            data = wrapper.get("data", {})
            if not isinstance(data, dict):
                return None

            item = data.get("item")
            main = item.get("main", {}) if isinstance(item, dict) else {}
            nested = isinstance(main, dict) and isinstance(main.get("exContent"), dict)
            if nested:
                ex_content = main["exContent"]
                click_args = main.get("clickParam", {}).get("args", {})
            else:
                main = data
                ex_content = data
                click_args = data.get("clickParam", {}).get("args", {})
            if not isinstance(ex_content, dict):
                return None
            if not isinstance(click_args, dict):
                click_args = {}

            item_id = str(
                ex_content.get("itemId") or ex_content.get("id") or ""
            ).strip()
            if not item_id:
                return None

            price_text = self._price_text(ex_content.get("price"))

            raw_link = str(
                main.get("targetUrl")
                or main.get("itemUrl")
                or ex_content.get("targetUrl")
                or ""
            )
            url = raw_link.replace("fleamarket://", f"{BASE_URL}/", 1)
            if not url:
                url = f"{BASE_URL}/item?id={item_id}"

            published = self._format_timestamp(
                click_args.get("publishTime") or ex_content.get("publishTime")
            )
            tags: list[str] = []
            if click_args.get("tag") == "freeship":
                tags.append("包邮")
            fish_tags = ex_content.get("fishTags")
            if isinstance(fish_tags, dict):
                rank_one = fish_tags.get("r1")
                tag_list = (
                    rank_one.get("tagList", []) if isinstance(rank_one, dict) else []
                )
            elif isinstance(fish_tags, list):
                tag_list = fish_tags
            else:
                tag_list = []
            if isinstance(tag_list, list):
                for tag_item in tag_list:
                    if isinstance(tag_item, dict):
                        tag_data = tag_item.get("data")
                        content = str(
                            (
                                tag_data.get("content")
                                if isinstance(tag_data, dict)
                                else None
                            )
                            or tag_item.get("content")
                            or ""
                        )
                    else:
                        content = str(tag_item)
                    if "验货宝" in content and "验货宝" not in tags:
                        tags.append("验货宝")

            return {
                "id": item_id,
                "title": str(ex_content.get("title") or ""),
                "price": self._parse_price(price_text),
                "url": url,
                "image": str(ex_content.get("picUrl") or ""),
                "location": str(ex_content.get("area") or ex_content.get("city") or ""),
                "seller": str(
                    ex_content.get("userNickName") or ex_content.get("userNick") or ""
                ),
                "publish_time": published,
                "wants": click_args.get("wantNum", ex_content.get("wantNum", 0)),
                "tags": tags,
            }
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _price_text(raw_price: Any) -> str:
        if isinstance(raw_price, list):
            return "".join(
                str(part.get("text", ""))
                for part in raw_price
                if isinstance(part, dict)
            )
        if isinstance(raw_price, dict):
            return str(
                raw_price.get("text")
                or raw_price.get("price")
                or raw_price.get("value")
                or ""
            )
        return str(raw_price or "")

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
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--proxy",
        help="HTTP(S) or SOCKS proxy URL; may be visible in process arguments",
    )
    proxy_group.add_argument(
        "--proxy-file",
        help="read proxy URL from a user-private UTF-8 file",
    )
    parser.add_argument(
        "--browser-channel",
        default=os.getenv("XIANYU_BROWSER_CHANNEL"),
        help="Playwright browser channel, for example chrome",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--debug", action="store_true", help="enable debug metadata")
    parser.add_argument(
        "--quiet", action="store_true", help="suppress routine diagnostic logs"
    )
    parser.add_argument("--retries", "-r", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spider = XianyuSpider(
            state_file=args.state,
            proxy=resolve_proxy(args.proxy, args.proxy_file),
            headless=not args.headed,
            browser_channel=args.browser_channel,
            verbose=not args.quiet,
        )
        spider.debug = args.debug
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
    except (OSError, SpiderError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "keyword": args.keyword,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
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
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
