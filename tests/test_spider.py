from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from spider import (
    CapturedSearchResponse,
    PlaywrightTimeoutError,
    SearchCaptureError,
    SearchRejectedError,
    SearchResponseCollector,
    SpiderError,
    StateFileError,
    XianyuSpider,
    _context_options,
    _load_state_file,
    _navigate_to_search,
    _redact_proxy,
    _safe_page_location,
    _validate_search_navigation,
    build_proxy_settings,
    resolve_proxy,
)


def make_wrapper(
    *,
    item_id: str = "123",
    price: str = "当前价 4999.50",
    location: str = "上海",
    target_url: str = "",
) -> dict[str, Any]:
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "itemId": item_id,
                        "title": "测试商品",
                        "price": [{"text": price}],
                        "userNickName": "卖家",
                        "picUrl": "https://example.com/image.jpg",
                        "area": location,
                        "fishTags": {
                            "r1": {
                                "tagList": [
                                    {"data": {"content": "支持验货宝"}},
                                ]
                            }
                        },
                    },
                    "clickParam": {
                        "args": {
                            "publishTime": "1710000000000",
                            "wantNum": "8",
                            "tag": "freeship",
                        }
                    },
                    "targetUrl": target_url,
                }
            }
        }
    }


def test_parse_item_and_fallback_url() -> None:
    item = XianyuSpider()._parse_api_item(make_wrapper())

    assert item is not None
    assert item["id"] == "123"
    assert item["price"] == 4999.5
    assert item["url"] == "https://www.goofish.com/item?id=123"
    assert item["tags"] == ["包邮", "验货宝"]
    assert item["publish_time"]


def test_parse_flat_item_shape() -> None:
    item = XianyuSpider()._parse_api_item(
        {
            "data": {
                "id": "flat-1",
                "title": "扁平商品",
                "price": {"text": "¥ 88.50"},
                "picUrl": "https://example.com/flat.jpg",
                "city": "杭州",
                "userNick": "卖家",
                "wantNum": 3,
                "fishTags": [{"content": "支持验货宝"}],
            }
        }
    )

    assert item is not None
    assert item["id"] == "flat-1"
    assert item["price"] == 88.5
    assert item["location"] == "杭州"
    assert item["seller"] == "卖家"
    assert item["wants"] == 3
    assert item["tags"] == ["验货宝"]
    assert item["url"] == "https://www.goofish.com/item?id=flat-1"


def test_local_filters_are_strict() -> None:
    items = [
        {"id": "1", "price": 999, "location": "上海"},
        {"id": "2", "price": 1_500, "location": "北京"},
        {"id": "3", "price": None, "location": "上海"},
    ]

    result = XianyuSpider._filter_items(
        items, min_price=500, max_price=1_000, location="上海"
    )

    assert [item["id"] for item in result] == ["1"]


def test_parse_capture_accepts_success_and_rejects_risk_control() -> None:
    spider = XianyuSpider()
    success = CapturedSearchResponse(
        status=200,
        payload={
            "ret": ["SUCCESS::调用成功"],
            "data": {"resultList": [make_wrapper()]},
        },
    )
    rejected = CapturedSearchResponse(
        status=200,
        payload={"ret": ["RGV587_ERROR::被挤爆啦"], "data": {}},
    )

    assert len(spider._parse_capture(success)) == 1
    with pytest.raises(SearchRejectedError, match="RGV587"):
        spider._parse_capture(rejected)


def test_parse_capture_rejects_malformed_response_shapes() -> None:
    spider = XianyuSpider()

    with pytest.raises(SearchCaptureError, match="non-object JSON"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload=None,
                error="search API returned a non-object JSON payload",
            )
        )

    with pytest.raises(SearchCaptureError, match="data is not an object"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={"ret": ["SUCCESS::ok"], "data": []},
            )
        )


def test_proxy_credentials_are_redacted() -> None:
    assert _redact_proxy("http://user:secret@127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_authenticated_http_proxy_uses_playwright_credential_fields() -> None:
    assert build_proxy_settings("http://file-user:p%40ss@127.0.0.1:7890") == {
        "server": "http://127.0.0.1:7890",
        "username": "file-user",
        "password": "p@ss",
    }


def test_authenticated_socks5_proxy_is_rejected() -> None:
    with pytest.raises(ValueError, match="authenticated SOCKS5"):
        build_proxy_settings("socks5://user:secret@127.0.0.1:1080")

    assert build_proxy_settings("socks5://127.0.0.1:1080") == {
        "server": "socks5://127.0.0.1:1080"
    }


def test_proxy_credentials_are_removed_from_runtime_errors(
    monkeypatch: Any,
) -> None:
    spider = XianyuSpider(proxy="http://private-user:private-password@127.0.0.1:7890")

    async def fail(_keyword: str, _pages: int) -> list[dict[str, Any]]:
        raise SpiderError("private-user private-password")

    monkeypatch.setattr(spider, "_search_once", fail)

    with pytest.raises(SpiderError) as captured:
        asyncio.run(spider.search("test", max_retries=1))

    assert "private-user" not in str(captured.value)
    assert "private-password" not in str(captured.value)


def test_search_capture_error_is_not_retried(monkeypatch: Any) -> None:
    spider = XianyuSpider()
    attempts = 0

    async def fail(_keyword: str, _pages: int) -> list[dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        raise SearchCaptureError("missing search request")

    monkeypatch.setattr(spider, "_search_once", fail)

    with pytest.raises(SearchCaptureError, match="missing search request"):
        asyncio.run(spider.search("test", max_retries=3))

    assert attempts == 1


def test_proxy_input_precedence_avoids_required_argv_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text(
        "http://file-user:file-secret@127.0.0.1:7890\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "XIANYU_PROXY",
        "http://env-user:env-secret@127.0.0.1:7890",
    )

    assert resolve_proxy("socks5://cli:secret@localhost:1080", str(proxy_file)) == (
        "socks5://cli:secret@localhost:1080"
    )
    assert resolve_proxy(None, str(proxy_file)) == (
        "http://file-user:file-secret@127.0.0.1:7890"
    )
    assert resolve_proxy(None) == "http://env-user:env-secret@127.0.0.1:7890"


def test_load_standard_and_enhanced_state(tmp_path: Path) -> None:
    standard_path = tmp_path / "standard.json"
    standard = {"cookies": [], "origins": []}
    standard_path.write_text(json.dumps(standard), encoding="utf-8")
    storage, overrides, headers = _load_state_file(str(standard_path))
    assert storage == standard
    assert overrides == {}
    assert headers == {}

    enhanced_path = tmp_path / "enhanced.json"
    enhanced = {
        "cookies": [],
        "headers": {
            "User-Agent": "Mobile Test",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": "must-not-leak",
        },
        "env": {
            "navigator": {"maxTouchPoints": 5},
            "screen": {"width": 390, "height": 844, "devicePixelRatio": 3},
            "intl": {"timeZone": "Asia/Shanghai"},
        },
    }
    enhanced_path.write_text(json.dumps(enhanced), encoding="utf-8")
    storage, overrides, headers = _load_state_file(str(enhanced_path))
    assert storage == {"cookies": [], "origins": []}
    assert overrides == {
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }
    assert headers["Accept-Language"] == "zh-CN,zh;q=0.9"
    assert "Cookie" not in headers


def test_load_state_rejects_invalid_schema(tmp_path: Path) -> None:
    state_path = tmp_path / "bad.json"
    state_path.write_text("{}", encoding="utf-8")
    with pytest.raises(StateFileError, match="cookies array"):
        _load_state_file(str(state_path))


def test_standard_state_uses_desktop_routeable_context() -> None:
    state = {"cookies": [], "origins": []}

    options = _context_options(state, {}, {})

    assert options["storage_state"] == state
    assert options["viewport"] == {"width": 1440, "height": 900}
    assert options["service_workers"] == "block"
    assert "user_agent" not in options
    assert "is_mobile" not in options
    assert "has_touch" not in options


def test_enhanced_state_cannot_override_desktop_device_fields() -> None:
    options = _context_options(
        None,
        {
            "viewport": {"width": 390, "height": 844},
            "is_mobile": True,
            "has_touch": True,
            "user_agent": "Mobile Test",
            "locale": "zh-TW",
            "timezone_id": "Asia/Taipei",
        },
        {},
    )

    assert options["viewport"] == {"width": 1440, "height": 900}
    assert options["locale"] == "zh-TW"
    assert options["timezone_id"] == "Asia/Taipei"
    assert "user_agent" not in options
    assert "is_mobile" not in options
    assert "has_touch" not in options
    assert options["service_workers"] == "block"


def test_navigation_validation_rejects_login_without_leaking_query() -> None:
    with pytest.raises(StateFileError, match="rejected") as captured:
        _validate_search_navigation(
            url=(
                "https://passport.goofish.com/mini_login.htm"
                "?returnUrl=https%3A%2F%2Fwww.goofish.com%2Fsearch"
                "%3Fsign%3DSECRET"
            ),
            status=200,
            has_state=True,
        )

    assert "SECRET" not in str(captured.value)
    assert (
        _safe_page_location("https://www.goofish.com/search?q=test&sign=SECRET")
        == "https://www.goofish.com/search"
    )


def test_navigation_validation_rejects_http_failure() -> None:
    with pytest.raises(SearchRejectedError, match="HTTP 503"):
        _validate_search_navigation(
            url="https://www.goofish.com/search?q=test",
            status=503,
            has_state=True,
        )


def test_navigation_timeout_rechecks_final_login_url() -> None:
    class Page:
        url = (
            "https://passport.goofish.com/mini_login.htm"
            "?returnUrl=https%3A%2F%2Fwww.goofish.com%2Fsearch%3Fsign%3DSECRET"
        )

        async def goto(self, *_args: object, **_kwargs: object) -> None:
            raise PlaywrightTimeoutError("navigation timeout")

    async def exercise() -> None:
        with pytest.raises(StateFileError, match="rejected") as captured:
            await _navigate_to_search(
                Page(),
                search_url="https://www.goofish.com/search?q=test",
                timeout_ms=10,
                has_state=True,
                headless=True,
            )
        assert "SECRET" not in str(captured.value)

    asyncio.run(exercise())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_search_response_collector_routes_exact_json(method: str) -> None:
    class Request:
        def __init__(self, request_method: str):
            self.method = request_method

    class Response:
        status = 200

        async def body(self) -> bytes:
            return b'{"ret":["SUCCESS::ok"],"data":{"resultList":[]}}'

    class Route:
        def __init__(self, request_method: str):
            self.request = Request(request_method)
            self.fulfilled = False

        async def fetch(self) -> Response:
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            self.fulfilled = True

        async def continue_(self) -> None:
            raise AssertionError("search request should be fulfilled")

    async def exercise() -> None:
        collector = SearchResponseCollector()
        route = Route(method)
        await collector._handle_route(route)  # noqa: SLF001
        result = await collector.next(100)
        assert route.fulfilled is True
        assert result.status == 200
        assert result.payload == {
            "ret": ["SUCCESS::ok"],
            "data": {"resultList": []},
        }

    asyncio.run(exercise())


def test_search_response_collector_ignores_preflight() -> None:
    class Request:
        method = "OPTIONS"

    class Route:
        request = Request()
        continued = False

        async def continue_(self) -> None:
            self.continued = True

    async def exercise() -> None:
        collector = SearchResponseCollector()
        route = Route()
        await collector._handle_route(route)  # noqa: SLF001

        assert route.continued is True
        with pytest.raises(SpiderError, match="timed out"):
            await collector.next(1)

    asyncio.run(exercise())


def test_search_response_collector_rejects_non_object_json() -> None:
    class Request:
        method = "POST"

    class Response:
        status = 200

        async def body(self) -> bytes:
            return b"[]"

    class Route:
        request = Request()

        async def fetch(self) -> Response:
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            return None

    async def exercise() -> None:
        collector = SearchResponseCollector()
        await collector._handle_route(Route())  # noqa: SLF001
        result = await collector.next(100)

        assert result.payload is None
        assert result.error == "search API returned a non-object JSON payload"

    asyncio.run(exercise())


def test_advance_page_clicks_real_next_control() -> None:
    expected = CapturedSearchResponse(
        status=200,
        payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
    )

    class Locator:
        clicked = False
        appeared = False

        async def wait_for(self, **_kwargs: Any) -> None:
            await asyncio.sleep(0)
            self.appeared = True

        async def scroll_into_view_if_needed(self) -> None:
            assert self.appeared

        async def click(self, **_kwargs: Any) -> None:
            assert self.appeared
            self.clicked = True

    class LocatorResult:
        def __init__(self, locator: Locator):
            self.first = locator

    class Page:
        def __init__(self, locator: Locator):
            self._locator = locator

        def locator(self, _selector: str) -> LocatorResult:
            return LocatorResult(self._locator)

    class Collector:
        async def next(self, _timeout_ms: int) -> CapturedSearchResponse:
            return expected

    async def exercise() -> None:
        locator = Locator()
        result = await XianyuSpider()._advance_page(  # noqa: SLF001
            Page(locator), Collector(), 2, 3
        )
        assert result is expected
        assert locator.clicked is True

    asyncio.run(exercise())


def test_advance_page_stops_after_bounded_wait_when_next_is_absent() -> None:
    class Locator:
        async def wait_for(self, **kwargs: Any) -> None:
            assert kwargs["state"] == "visible"
            assert kwargs["timeout"] <= 5_000
            raise PlaywrightTimeoutError("not found")

    class LocatorResult:
        first = Locator()

    class Page:
        def locator(self, _selector: str) -> LocatorResult:
            return LocatorResult()

    class Collector:
        async def next(self, _timeout_ms: int) -> CapturedSearchResponse:
            raise AssertionError("collector must not be armed without a next button")

    async def exercise() -> None:
        result = await XianyuSpider(timeout_ms=20_000)._advance_page(  # noqa: SLF001
            Page(), Collector(), 2, 3
        )
        assert result is None

    asyncio.run(exercise())
