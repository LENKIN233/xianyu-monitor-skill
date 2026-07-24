from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from spider import (
    CapturedSearchResponse,
    SearchRejectedError,
    SearchResponseCollector,
    StateFileError,
    XianyuSpider,
    _load_state_file,
    _redact_proxy,
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


def test_proxy_credentials_are_redacted() -> None:
    assert _redact_proxy("http://user:secret@127.0.0.1:7890") == "http://127.0.0.1:7890"


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
    assert overrides["viewport"] == {"width": 390, "height": 844}
    assert headers["Accept-Language"] == "zh-CN,zh;q=0.9"
    assert "Cookie" not in headers


def test_load_state_rejects_invalid_schema(tmp_path: Path) -> None:
    state_path = tmp_path / "bad.json"
    state_path.write_text("{}", encoding="utf-8")
    with pytest.raises(StateFileError, match="cookies array"):
        _load_state_file(str(state_path))


def test_search_response_collector_routes_exact_post_json() -> None:
    class Request:
        method = "POST"

    class Response:
        status = 200

        async def body(self) -> bytes:
            return b'{"ret":["SUCCESS::ok"],"data":{"resultList":[]}}'

    class Route:
        request = Request()
        fulfilled = False

        async def fetch(self) -> Response:
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            self.fulfilled = True

        async def continue_(self) -> None:
            raise AssertionError("POST search request should be fulfilled")

    async def exercise() -> None:
        collector = SearchResponseCollector()
        route = Route()
        await collector._handle_route(route)  # noqa: SLF001
        result = await collector.next(100)
        assert route.fulfilled is True
        assert result.status == 200
        assert result.payload == {
            "ret": ["SUCCESS::ok"],
            "data": {"resultList": []},
        }

    asyncio.run(exercise())


def test_advance_page_clicks_real_next_control() -> None:
    expected = CapturedSearchResponse(
        status=200,
        payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
    )

    class Locator:
        clicked = False

        async def count(self) -> int:
            return 1

        async def scroll_into_view_if_needed(self) -> None:
            return None

        async def click(self, **_kwargs: Any) -> None:
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
