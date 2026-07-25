from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import spider
from spider import (
    BrowserCleanupError,
    CapturedSearchResponse,
    CaptureTicket,
    PlaywrightTimeoutError,
    SearchCancelledError,
    SearchCaptureError,
    SearchRejectedError,
    SearchResponseCollector,
    SpiderError,
    StateFileError,
    StorageStateValidationError,
    XianyuSpider,
    _context_options,
    _filter_goofish_storage_state,
    _has_storage_state_material,
    _load_state_file,
    _navigate_to_search,
    _redact_proxy,
    _safe_page_location,
    _validate_search_navigation,
    build_proxy_settings,
    resolve_proxy,
)


def _function_line(function: Any, marker: str) -> int:
    lines, first_line = inspect.getsourcelines(function)
    return first_line + next(
        index for index, line in enumerate(lines) if line.strip() == marker
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

    with pytest.raises(SearchCaptureError, match="malformed ret markers"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={"data": {"resultList": []}},
            )
        )

    with pytest.raises(SearchRejectedError, match="SUCCESS_BOGUS"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={
                    "ret": ["SUCCESS_BOGUS"],
                    "data": {"resultList": []},
                },
            )
        )

    with pytest.raises(SearchCaptureError, match="malformed ret markers"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={
                    "ret": ["SUCCESS::ok", None],
                    "data": {"resultList": []},
                },
            )
        )

    with pytest.raises(SearchCaptureError, match="resultList is missing"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={"ret": ["SUCCESS::ok"], "data": {}},
            )
        )

    with pytest.raises(SearchCaptureError, match="unrecognized listing"):
        spider._parse_capture(
            CapturedSearchResponse(
                status=200,
                payload={
                    "ret": ["SUCCESS::ok"],
                    "data": {"resultList": [{"unexpected": True}]},
                },
            )
        )


@pytest.mark.parametrize("status", [None, 199, 300, 399, 400])
def test_parse_capture_requires_2xx_status(status: int | None) -> None:
    capture = CapturedSearchResponse(
        status=status,
        payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
    )

    with pytest.raises(SearchRejectedError, match="HTTP"):
        XianyuSpider()._parse_capture(capture)


@pytest.mark.parametrize("status", [200, 201, 299])
def test_parse_capture_accepts_all_2xx_statuses(status: int) -> None:
    capture = CapturedSearchResponse(
        status=status,
        payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
    )

    assert XianyuSpider()._parse_capture(capture) == []


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


def test_retryable_error_with_cleanup_failure_is_terminal(
    monkeypatch: Any,
) -> None:
    spider_instance = XianyuSpider()
    attempts = 0

    async def fail_then_succeed(
        _keyword: str,
        _pages: int,
    ) -> list[dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            primary = spider.PlaywrightError("transient browser failure")
            primary.cleanup_failures = [
                "failed to close the dedicated search browser",
                "failed to stop the dedicated browser runtime",
            ]
            raise primary
        return []

    monkeypatch.setattr(spider_instance, "_search_once", fail_then_succeed)

    with pytest.raises(SpiderError, match="cleanup was incomplete") as captured:
        asyncio.run(spider_instance.search("test", max_retries=2))

    assert captured.value.cleanup_failures == [
        "failed to close the dedicated search browser",
        "failed to stop the dedicated browser runtime",
    ]
    assert attempts == 1


def test_cancelled_search_with_cleanup_failure_is_terminal(
    monkeypatch: Any,
) -> None:
    spider_instance = XianyuSpider()
    attempts = 0

    async def cancel(_keyword: str, _pages: int) -> list[dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        interruption = asyncio.CancelledError()
        interruption.cleanup_failures = [
            "failed to close the dedicated search browser",
        ]
        raise interruption

    monkeypatch.setattr(spider_instance, "_search_once", cancel)

    with pytest.raises(BrowserCleanupError, match="cleanup was incomplete") as captured:
        asyncio.run(spider_instance.search("test", max_retries=3))

    assert captured.value.search_passed is False
    assert captured.value.cleanup_failures == [
        "failed to close the dedicated search browser"
    ]
    assert attempts == 1


def test_interrupted_playwright_start_attempts_async_manager_cleanup(
    monkeypatch: Any,
) -> None:
    events: list[str] = []

    class Manager:
        async def start(self) -> None:
            events.append("start")
            raise asyncio.CancelledError

        async def __aexit__(self, *_args: object) -> None:
            events.append("manager-exit")

    monkeypatch.setattr(spider, "async_playwright", Manager)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(XianyuSpider()._search_once("test", 1))  # noqa: SLF001

    assert events == ["start", "manager-exit"]


def test_interrupted_playwright_start_preserves_failed_cleanup_evidence(
    monkeypatch: Any,
) -> None:
    class Manager:
        async def start(self) -> None:
            raise asyncio.CancelledError

        async def __aexit__(self, *_args: object) -> None:
            raise RuntimeError("simulated manager cleanup failure")

    monkeypatch.setattr(spider, "async_playwright", Manager)

    with pytest.raises(asyncio.CancelledError) as captured:
        asyncio.run(XianyuSpider()._search_once("test", 1))  # noqa: SLF001

    assert captured.value.cleanup_failures == [
        "failed to stop the partially started browser runtime"
    ]


def test_start_error_with_cleanup_cancellation_is_terminal_and_not_retried(
    monkeypatch: Any,
) -> None:
    starts = 0

    class Manager:
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            raise spider.PlaywrightError("simulated start failure")

        async def __aexit__(self, *_args: object) -> None:
            raise asyncio.CancelledError

    monkeypatch.setattr(spider, "async_playwright", Manager)

    with pytest.raises(
        SearchCancelledError,
        match="browser cleanup was incomplete",
    ) as captured:
        asyncio.run(XianyuSpider().search("test", max_retries=3))

    assert starts == 1
    assert captured.value.capability_status == "not-established"
    assert captured.value.cleanup_failures == [
        "failed to stop the partially started browser runtime"
    ]


@pytest.mark.parametrize(
    ("boundary", "expected_events"),
    [
        (
            'launch_kwargs: dict[str, Any] = {"headless": self.headless}',
            ["manager-start", "playwright-stop"],
        ),
        (
            "if browser is not None:",
            [
                "manager-start",
                "browser-launch",
                "browser-close",
                "playwright-stop",
            ],
        ),
    ],
)
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_async_acquisition_boundary_interruption_closes_acquired_resources(
    monkeypatch: Any,
    boundary: str,
    expected_events: list[str],
    interruption: type[BaseException],
) -> None:
    events: list[str] = []

    class Browser:
        async def close(self) -> None:
            events.append("browser-close")

    class Chromium:
        async def launch(self, **_kwargs: Any) -> Browser:
            events.append("browser-launch")
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            events.append("playwright-stop")

    class Manager:
        async def start(self) -> Playwright:
            events.append("manager-start")
            return Playwright()

    monkeypatch.setattr(spider, "async_playwright", Manager)
    target_line = _function_line(XianyuSpider._search_once, boundary)
    previous_trace = sys.gettrace()
    triggered = False

    def interrupt_at_boundary(
        frame: Any,
        event: str,
        _argument: Any,
    ) -> Any:
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is XianyuSpider._search_once.__code__
            and frame.f_lineno == target_line
        ):
            triggered = True
            sys.settrace(None)
            raise interruption
        return interrupt_at_boundary

    sys.settrace(interrupt_at_boundary)
    try:
        with pytest.raises(interruption):
            asyncio.run(
                XianyuSpider(verbose=False)._search_once("test", 1)  # noqa: SLF001
            )
    finally:
        sys.settrace(previous_trace)

    assert triggered is True
    assert events == expected_events


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
    standard = {
        "cookies": [
            {
                "name": "session",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            },
            {
                "name": "third-party",
                "value": "must-not-enter-context",
                "domain": ".taobao.com",
                "path": "/",
            },
        ],
        "origins": [
            {
                "origin": "https://login.taobao.com",
                "localStorage": [{"name": "secret", "value": "third-party"}],
            }
        ],
    }
    standard_path.write_text(json.dumps(standard), encoding="utf-8")
    storage, overrides, headers = _load_state_file(str(standard_path))
    assert storage == {
        "cookies": [
            {
                "name": "session",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    assert overrides == {}
    assert headers == {}

    enhanced_path = tmp_path / "enhanced.json"
    enhanced = {
        "cookies": [
            {
                "name": "session",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
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
    assert storage == {
        "cookies": [
            {
                "name": "session",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
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


def test_empty_state_is_not_treated_as_a_login_state(tmp_path: Path) -> None:
    state_path = tmp_path / "empty.json"
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    with pytest.raises(StateFileError, match="no usable Goofish"):
        _load_state_file(str(state_path))


def test_state_filter_rejects_spoofed_or_non_https_goofish_origins() -> None:
    state = {
        "cookies": [
            {"name": "site", "value": "ok", "domain": ".goofish.com", "path": "/"},
            {
                "name": "suffix",
                "value": "bad",
                "domain": "goofish.com.evil",
                "path": "/",
            },
        ],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [{"name": "site", "value": "ok"}],
            },
            {
                "origin": "http://www.goofish.com",
                "localStorage": [{"name": "http", "value": "bad"}],
            },
            {
                "origin": "https://www.goofish.com.evil",
                "localStorage": [{"name": "suffix", "value": "bad"}],
            },
        ],
    }

    assert _filter_goofish_storage_state(state) == {
        "cookies": [
            {"name": "site", "value": "ok", "domain": ".goofish.com", "path": "/"}
        ],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [{"name": "site", "value": "ok"}],
            }
        ],
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://user@www.goofish.com",
        "https://www.goofish.com:443",
        "https://www.goofish.com/",
        "https://www.goofish.com/path",
    ],
)
def test_state_filter_rejects_noncanonical_origin_shape(origin: str) -> None:
    with pytest.raises(StorageStateValidationError, match="origin is invalid"):
        _filter_goofish_storage_state(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": origin,
                        "localStorage": [],
                    }
                ],
            }
        )


@pytest.mark.parametrize("domain", ["..goofish.com", "\n.goofish.com"])
def test_state_filter_rejects_malformed_cookie_domain(domain: str) -> None:
    with pytest.raises(StorageStateValidationError, match="cookie has invalid domain"):
        _filter_goofish_storage_state(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "candidate",
                        "domain": domain,
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        )


def test_load_state_accepts_sanitized_indexed_db_only_snapshot(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "indexed-db.json"
    state_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.goofish.com",
                        "localStorage": [],
                        "indexedDB": [
                            {
                                "name": "candidate-db",
                                "version": 1,
                                "stores": [
                                    {
                                        "name": "session",
                                        "autoIncrement": False,
                                        "keyPath": "id",
                                        "records": [
                                            {
                                                "value": {
                                                    "id": "candidate",
                                                    "payload": "opaque",
                                                },
                                                "ignored": "must-not-propagate",
                                            }
                                        ],
                                        "indexes": [],
                                        "ignored": "must-not-propagate",
                                    }
                                ],
                                "ignored": "must-not-propagate",
                            }
                        ],
                        "ignored": "must-not-propagate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    storage, _, _ = _load_state_file(str(state_path))

    assert storage == {
        "cookies": [],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [],
                "indexedDB": [
                    {
                        "name": "candidate-db",
                        "version": 1,
                        "stores": [
                            {
                                "name": "session",
                                "autoIncrement": False,
                                "keyPath": "id",
                                "records": [
                                    {
                                        "value": {
                                            "id": "candidate",
                                            "payload": "opaque",
                                        }
                                    }
                                ],
                                "indexes": [],
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "origin_fields",
    [
        {"localStorage": "not-a-list"},
        {"localStorage": [], "indexedDB": "not-a-list"},
        {
            "localStorage": [],
            "indexedDB": [{"name": "db", "version": True, "stores": []}],
        },
    ],
)
def test_load_state_rejects_malformed_goofish_storage(
    tmp_path: Path,
    origin_fields: dict[str, Any],
) -> None:
    state_path = tmp_path / "malformed.json"
    state_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://www.goofish.com",
                        **origin_fields,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateFileError, match="invalid browser state schema"):
        _load_state_file(str(state_path))


def test_storage_material_does_not_claim_authentication() -> None:
    assert _has_storage_state_material(
        {"cookies": [{"name": "cookie2", "value": "anonymous"}], "origins": []}
    )
    assert _has_storage_state_material(
        {
            "cookies": [],
            "origins": [
                {
                    "origin": "https://www.goofish.com",
                    "localStorage": [{"name": "candidate", "value": "value"}],
                }
            ],
        }
    )
    assert not _has_storage_state_material({"cookies": [], "origins": []})


def test_standard_state_uses_desktop_routeable_context() -> None:
    state = {
        "cookies": [{"name": "session", "value": "candidate"}],
        "origins": [],
    }

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
    with pytest.raises(StateFileError, match="did not accept") as captured:
        _validate_search_navigation(
            url=(
                "https://passport.goofish.com/mini_login.htm"
                "?returnUrl=https%3A%2F%2Fwww.goofish.com%2Fsearch"
                "%3Fsign%3DSECRET"
            ),
            status=200,
            state_supplied=True,
        )

    assert "SECRET" not in str(captured.value)
    assert (
        _safe_page_location("https://www.goofish.com/search?q=test&sign=SECRET")
        == "https://www.goofish.com"
    )
    assert (
        _safe_page_location(
            "https://USERINFO_SENTINEL@www.goofish.com/"
            "PATH_SENTINEL?token=QUERY_SENTINEL"
        )
        == "https://www.goofish.com"
    )


def test_navigation_validation_rejects_http_failure() -> None:
    with pytest.raises(SearchRejectedError, match="HTTP 503"):
        _validate_search_navigation(
            url="https://www.goofish.com/search?q=test",
            status=503,
            state_supplied=True,
        )


def test_external_http_failure_is_not_misclassified_as_site_rejection() -> None:
    with pytest.raises(SearchCaptureError, match="unexpected"):
        _validate_search_navigation(
            url="https://example.com/error",
            status=503,
            state_supplied=True,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.goofish.com/search?q=test",
        "https://www.goofish.com:8443/search?q=test",
        "http://passport.goofish.com/mini_login.htm",
        "https://passport.goofish.com:8443/mini_login.htm",
    ],
)
def test_navigation_validation_rejects_noncanonical_origins(url: str) -> None:
    with pytest.raises(SearchCaptureError, match="unexpected"):
        _validate_search_navigation(
            url=url,
            status=200,
            state_supplied=True,
        )


def test_navigation_validation_accepts_exact_https_search_origin() -> None:
    _validate_search_navigation(
        url="https://www.goofish.com/search?q=test",
        status=200,
        state_supplied=True,
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
        with pytest.raises(StateFileError, match="did not accept") as captured:
            await _navigate_to_search(
                Page(),
                search_url="https://www.goofish.com/search?q=test",
                timeout_ms=10,
                state_supplied=True,
                headless=True,
            )
        assert "SECRET" not in str(captured.value)

    asyncio.run(exercise())


def test_success_output_reports_capability_without_identity(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 1
            self.debug = False

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)

    assert spider.main(["--keyword", "test"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["search_capability"]["status"] == "passed-for-this-run"
    assert payload["authentication"]["status"] == "not-evaluated"
    assert payload["identity"]["status"] == "not-evaluated"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_success_serialization_cancellation_reports_passed_capability(
    monkeypatch: Any,
    capsys: Any,
    interruption: type[BaseException],
    error_type: str,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 1
            self.debug = False
            self.last_capability_status = "not-established"

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            self.last_capability_status = "passed-for-this-run"
            return []

    original_dumps = spider.json.dumps

    def interrupt_success_payload(payload: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(payload, dict) and payload.get("ok") is True:
            raise interruption
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)
    monkeypatch.setattr(spider.json, "dumps", interrupt_success_payload)

    assert spider.main(["--keyword", "test"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error"] == "search cancelled"
    assert payload["error_type"] == error_type
    assert payload["search_capability"]["status"] == "passed-for-this-run"
    assert payload["cleanup"]["status"] == "complete-or-not-required"


def test_rgv_rejection_does_not_claim_identity(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.debug = False

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise SearchRejectedError("RGV587::request rejected")

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)

    assert spider.main(["--keyword", "test"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["search_capability"]["status"] == "rejected-for-this-run"
    assert payload["authentication"]["status"] == "not-evaluated"
    assert payload["identity"]["status"] == "not-evaluated"
    assert "authenticated" not in json.dumps(payload).lower()


def test_unexpected_navigation_does_not_claim_site_rejection(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.debug = False

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise SearchCaptureError("unexpected search navigation")

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)

    assert spider.main(["--keyword", "test"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_type"] == "SearchCaptureError"
    assert payload["search_capability"]["status"] == "not-established"


def test_cli_cancellation_is_structured(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.debug = False

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise asyncio.CancelledError

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)

    assert spider.main(["--keyword", "test"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_type"] == "CancelledError"
    assert payload["search_capability"]["status"] == "not-established"
    assert payload["cleanup"]["status"] == "complete-or-not-required"


def test_cli_cleanup_cancellation_preserves_rejected_capability(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    class FakeSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.debug = False
            self.last_capability_status = "rejected-for-this-run"

        async def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            error = SearchCancelledError(
                "search was cancelled; browser cleanup was incomplete",
                capability_status="rejected-for-this-run",
            )
            error.cleanup_failures = ["failed to close the dedicated search browser"]
            raise error

    monkeypatch.setattr(spider, "XianyuSpider", FakeSpider)

    assert spider.main(["--keyword", "test"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_type"] == "SearchCancelledError"
    assert payload["search_capability"]["status"] == "rejected-for-this-run"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the dedicated search browser"],
    }


def test_browser_close_failure_is_terminal_and_not_retried(
    monkeypatch: Any,
) -> None:
    launches = 0

    class Page:
        url = "https://www.goofish.com/search"

        def set_default_timeout(self, _timeout: int) -> None:
            return None

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **_kwargs: Any) -> Context:
            return Context()

        async def close(self) -> None:
            raise RuntimeError("close failed")

    class Chromium:
        async def launch(self, **_kwargs: Any) -> Browser:
            nonlocal launches
            launches += 1
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            raise RuntimeError("runtime stop failed")

    class Manager:
        async def start(self) -> Playwright:
            return Playwright()

    class Collector:
        def __init__(self, *_args: Any):
            pass

        async def install(self, _context: Context) -> None:
            return None

        def arm(self, expected_page: int) -> CaptureTicket:
            return CaptureTicket(1, expected_page)

        def disarm(self, _ticket: CaptureTicket) -> None:
            return None

        async def next(
            self,
            _ticket: CaptureTicket,
            _timeout: int,
        ) -> CapturedSearchResponse:
            return CapturedSearchResponse(
                status=200,
                payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
            )

    async def navigate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(spider, "async_playwright", lambda: Manager())
    monkeypatch.setattr(spider, "SearchResponseCollector", Collector)
    monkeypatch.setattr(spider, "_navigate_to_search", navigate)

    with pytest.raises(BrowserCleanupError, match="failed to close") as captured:
        asyncio.run(XianyuSpider().search("test", max_retries=2))

    assert captured.value.cleanup_failures == [
        "failed to close the dedicated search browser",
        "failed to stop the dedicated browser runtime",
    ]
    assert launches == 1


def test_close_failure_does_not_mask_or_retry_rgv(
    monkeypatch: Any,
) -> None:
    launches = 0

    class Page:
        url = "https://www.goofish.com/search"

        def set_default_timeout(self, _timeout: int) -> None:
            return None

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **_kwargs: Any) -> Context:
            return Context()

        async def close(self) -> None:
            raise RuntimeError("close failed")

    class Chromium:
        async def launch(self, **_kwargs: Any) -> Browser:
            nonlocal launches
            launches += 1
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            raise RuntimeError("runtime stop failed")

    class Manager:
        async def start(self) -> Playwright:
            return Playwright()

    class Collector:
        def __init__(self, *_args: Any):
            pass

        async def install(self, _context: Context) -> None:
            return None

        def arm(self, expected_page: int) -> CaptureTicket:
            return CaptureTicket(1, expected_page)

        def disarm(self, _ticket: CaptureTicket) -> None:
            return None

        async def next(
            self,
            _ticket: CaptureTicket,
            _timeout: int,
        ) -> CapturedSearchResponse:
            return CapturedSearchResponse(
                status=200,
                payload={"ret": ["RGV587::rejected"], "data": {}},
            )

    async def navigate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(spider, "async_playwright", lambda: Manager())
    monkeypatch.setattr(spider, "SearchResponseCollector", Collector)
    monkeypatch.setattr(spider, "_navigate_to_search", navigate)

    with pytest.raises(SearchRejectedError, match="RGV587") as captured:
        asyncio.run(XianyuSpider().search("test", max_retries=2))

    assert captured.value.cleanup_failures == [
        "failed to close the dedicated search browser",
        "failed to stop the dedicated browser runtime",
    ]
    assert launches == 1


def test_runtime_stop_failure_after_search_is_terminal_and_not_retried(
    monkeypatch: Any,
) -> None:
    launches = 0

    class Page:
        url = "https://www.goofish.com/search"

        def set_default_timeout(self, _timeout: int) -> None:
            return None

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **_kwargs: Any) -> Context:
            return Context()

        async def close(self) -> None:
            return None

    class Chromium:
        async def launch(self, **_kwargs: Any) -> Browser:
            nonlocal launches
            launches += 1
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            raise RuntimeError("runtime stop failed")

    class Manager:
        async def start(self) -> Playwright:
            return Playwright()

    class Collector:
        def __init__(self, *_args: Any):
            pass

        async def install(self, _context: Context) -> None:
            return None

        def arm(self, expected_page: int) -> CaptureTicket:
            return CaptureTicket(1, expected_page)

        def disarm(self, _ticket: CaptureTicket) -> None:
            return None

        async def next(
            self,
            _ticket: CaptureTicket,
            _timeout: int,
        ) -> CapturedSearchResponse:
            return CapturedSearchResponse(
                status=200,
                payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
            )

    async def navigate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(spider, "async_playwright", lambda: Manager())
    monkeypatch.setattr(spider, "SearchResponseCollector", Collector)
    monkeypatch.setattr(spider, "_navigate_to_search", navigate)

    with pytest.raises(BrowserCleanupError, match="failed to stop") as captured:
        asyncio.run(XianyuSpider().search("test", max_retries=2))

    assert captured.value.search_passed is True
    assert captured.value.cleanup_failures == [
        "failed to stop the dedicated browser runtime"
    ]
    assert launches == 1


def test_cleanup_cancellation_after_search_preserves_passed_capability(
    monkeypatch: Any,
) -> None:
    launches = 0

    class Page:
        url = "https://www.goofish.com/search"

        def set_default_timeout(self, _timeout: int) -> None:
            return None

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **_kwargs: Any) -> Context:
            return Context()

        async def close(self) -> None:
            raise asyncio.CancelledError

    class Chromium:
        async def launch(self, **_kwargs: Any) -> Browser:
            nonlocal launches
            launches += 1
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            raise RuntimeError("runtime stop failed")

    class Manager:
        async def start(self) -> Playwright:
            return Playwright()

    class Collector:
        def __init__(self, *_args: Any):
            pass

        async def install(self, _context: Context) -> None:
            return None

        def arm(self, expected_page: int) -> CaptureTicket:
            return CaptureTicket(1, expected_page)

        def disarm(self, _ticket: CaptureTicket) -> None:
            return None

        async def next(
            self,
            _ticket: CaptureTicket,
            _timeout: int,
        ) -> CapturedSearchResponse:
            return CapturedSearchResponse(
                status=200,
                payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
            )

    async def navigate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(spider, "async_playwright", lambda: Manager())
    monkeypatch.setattr(spider, "SearchResponseCollector", Collector)
    monkeypatch.setattr(spider, "_navigate_to_search", navigate)

    with pytest.raises(
        BrowserCleanupError,
        match="search completed but browser cleanup was interrupted",
    ) as captured:
        asyncio.run(XianyuSpider().search("test", max_retries=2))

    assert captured.value.search_passed is True
    assert captured.value.cleanup_failures == [
        "failed to close the dedicated search browser",
        "failed to stop the dedicated browser runtime",
    ]
    assert launches == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_search_response_collector_matches_decoded_request_data(
    method: str,
) -> None:
    keyword = "iPhone + 15/测试"
    encoded_data = urlencode(
        {
            "data": json.dumps(
                {"pageNumber": 2, "keyword": keyword},
                ensure_ascii=False,
            )
        }
    )

    class Request:
        def __init__(self, request_method: str):
            self.method = request_method
            self.url = (
                "https://h5api.m.goofish.com/h5/"
                "mtop.taobao.idlemtopsearch.pc.search/1.0/"
            )
            self.post_data = None
            if request_method == "GET":
                self.url = f"{self.url}?{encoded_data}"
            else:
                self.post_data = encoded_data

    class Response:
        status = 200

        async def body(self) -> bytes:
            return b'{"ret":["SUCCESS::ok"],"data":{"resultList":[]}}'

    class Route:
        def __init__(self, request_method: str):
            self.request = Request(request_method)
            self.fulfilled = False

        async def fetch(self, **kwargs: Any) -> Response:
            assert kwargs == {"max_redirects": 0}
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            self.fulfilled = True

        async def continue_(self) -> None:
            raise AssertionError("search request should be fulfilled")

    async def exercise() -> None:
        collector = SearchResponseCollector(keyword)
        route = Route(method)
        ticket = collector.arm(2)
        await collector._handle_route(route)  # noqa: SLF001
        result = await collector.next(ticket, 100)
        assert route.fulfilled is True
        assert result.status == 200
        assert result.page_number == 2
        assert result.payload == {
            "ret": ["SUCCESS::ok"],
            "data": {"resultList": []},
        }

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        ("http://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"),
        (
            "https://h5api.m.goofish.com:8443/h5/"
            "mtop.taobao.idlemtopsearch.pc.search/1.0/"
        ),
        (
            "https://h5api.m.goofish.com.evil/h5/"
            "mtop.taobao.idlemtopsearch.pc.search/1.0/"
        ),
        (
            "https://h5api.m.goofish.com/h5/"
            "mtop.taobao.idlemtopsearch.pc.search/1.0/extra"
        ),
    ],
)
def test_search_response_collector_rejects_noncanonical_urls(url: str) -> None:
    encoded_data = urlencode({"data": json.dumps({"pageNumber": 1, "keyword": "test"})})

    class Request:
        method = "POST"
        post_data = encoded_data

        def __init__(self) -> None:
            self.url = url

    class Route:
        request = Request()
        continued = False
        fetched = False

        async def continue_(self) -> None:
            self.continued = True

        async def fetch(self, **_kwargs: Any) -> None:
            self.fetched = True
            raise AssertionError("noncanonical request must not be fetched")

    async def exercise() -> None:
        collector = SearchResponseCollector("test")
        route = Route()
        ticket = collector.arm(1)
        await collector._handle_route(route)  # noqa: SLF001

        assert route.continued is True
        assert route.fetched is False
        with pytest.raises(SearchCaptureError, match="timed out"):
            await collector.next(ticket, 1)

    asyncio.run(exercise())


def test_search_response_collector_skips_mismatched_keyword_and_page() -> None:
    class Request:
        method = "GET"
        post_data = None

        def __init__(self, keyword: str, page_number: int):
            data = urlencode(
                {
                    "data": json.dumps(
                        {
                            "pageNumber": page_number,
                            "keyword": keyword,
                        }
                    )
                }
            )
            self.url = (
                "https://h5api.m.goofish.com/h5/"
                "mtop.taobao.idlemtopsearch.pc.search/1.0/"
                f"?{data}"
            )

    class Response:
        status = 200

        def __init__(self, marker: str):
            self.marker = marker

        async def body(self) -> bytes:
            return json.dumps(
                {
                    "ret": ["SUCCESS::ok"],
                    "data": {"resultList": [], "marker": self.marker},
                }
            ).encode()

    class Route:
        def __init__(self, keyword: str, page_number: int, marker: str):
            self.request = Request(keyword, page_number)
            self.response = Response(marker)
            self.continued = False

        async def fetch(self, **_kwargs: Any) -> Response:
            return self.response

        async def fulfill(self, **_kwargs: Any) -> None:
            return None

        async def continue_(self) -> None:
            self.continued = True

    async def exercise() -> None:
        collector = SearchResponseCollector("expected")
        stale = Route("expected", 1, "stale")
        wrong_keyword = Route("other", 2, "wrong")
        expected = Route("expected", 2, "expected")

        # A matching response observed before this action is not eligible for
        # the new capture window.
        await collector._handle_route(stale)  # noqa: SLF001
        ticket = collector.arm(2)
        for route in (wrong_keyword, expected):
            await collector._handle_route(route)  # noqa: SLF001

        second_page = await collector.next(ticket, 100)
        assert second_page.payload is not None
        assert second_page.payload["data"]["marker"] == "expected"
        assert stale.continued is True
        assert wrong_keyword.continued is True

    asyncio.run(exercise())


def test_search_response_collector_rejects_late_response_from_old_generation() -> None:
    started = asyncio.Event()
    release_old = asyncio.Event()

    class Request:
        method = "GET"
        post_data = None

        def __init__(self) -> None:
            data = urlencode(
                {"data": json.dumps({"pageNumber": 2, "keyword": "expected"})}
            )
            self.url = (
                "https://h5api.m.goofish.com/h5/"
                "mtop.taobao.idlemtopsearch.pc.search/1.0/"
                f"?{data}"
            )

    class Response:
        status = 200

        def __init__(self, marker: str):
            self.marker = marker

        async def body(self) -> bytes:
            return json.dumps(
                {
                    "ret": ["SUCCESS::ok"],
                    "data": {"resultList": [], "marker": self.marker},
                }
            ).encode()

    class Route:
        request = Request()

        def __init__(self, marker: str, *, blocked: bool):
            self.marker = marker
            self.blocked = blocked

        async def fetch(self, **_kwargs: Any) -> Response:
            if self.blocked:
                started.set()
                await release_old.wait()
            return Response(self.marker)

        async def fulfill(self, **_kwargs: Any) -> None:
            return None

        async def continue_(self) -> None:
            return None

    async def exercise() -> None:
        collector = SearchResponseCollector("expected")
        old_ticket = collector.arm(2)
        old_handler = asyncio.create_task(
            collector._handle_route(Route("old", blocked=True))  # noqa: SLF001
        )
        await started.wait()

        collector.disarm(old_ticket)
        new_ticket = collector.arm(2)
        release_old.set()
        await old_handler
        await collector._handle_route(Route("fresh", blocked=False))  # noqa: SLF001

        capture = await collector.next(new_ticket, 100)
        assert capture.payload is not None
        assert capture.payload["data"]["marker"] == "fresh"

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
        collector = SearchResponseCollector("test")
        route = Route()
        ticket = collector.arm(1)
        await collector._handle_route(route)  # noqa: SLF001

        assert route.continued is True
        with pytest.raises(SpiderError, match="timed out"):
            await collector.next(ticket, 1)

    asyncio.run(exercise())


def test_search_response_collector_rejects_non_object_json() -> None:
    class Request:
        method = "POST"
        url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
        post_data = urlencode(
            {"data": json.dumps({"pageNumber": 1, "keyword": "test"})}
        )

    class Response:
        status = 200

        async def body(self) -> bytes:
            return b"[]"

    class Route:
        request = Request()

        async def fetch(self, **_kwargs: Any) -> Response:
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            return None

    async def exercise() -> None:
        collector = SearchResponseCollector("test")
        ticket = collector.arm(1)
        await collector._handle_route(Route())  # noqa: SLF001
        result = await collector.next(ticket, 100)

        assert result.payload is None
        assert result.error == "search API returned a non-object JSON payload"

    asyncio.run(exercise())


def test_search_response_capture_error_never_leaks_request_secrets() -> None:
    encoded_data = urlencode({"data": json.dumps({"pageNumber": 1, "keyword": "test"})})

    class Request:
        method = "POST"
        post_data = encoded_data
        url = (
            "https://h5api.m.goofish.com/h5/"
            "mtop.taobao.idlemtopsearch.pc.search/1.0/"
            "?sign=QUERY_SENTINEL"
        )

    class Route:
        request = Request()

        async def fetch(self, **_kwargs: Any) -> None:
            raise spider.PlaywrightError(
                "https://h5api.m.goofish.com/?sign=QUERY_SENTINEL "
                "http://PROXY_USER:PROXY_PASS@127.0.0.1:7890"
            )

        async def continue_(self) -> None:
            return None

    async def exercise() -> None:
        collector = SearchResponseCollector("test")
        ticket = collector.arm(1)
        await collector._handle_route(Route())  # noqa: SLF001
        result = await collector.next(ticket, 100)

        assert result.error == "failed to capture search API response"
        assert "QUERY_SENTINEL" not in result.error
        assert "PROXY_USER" not in result.error
        assert "PROXY_PASS" not in result.error

    asyncio.run(exercise())


def test_search_response_collector_never_follows_redirects() -> None:
    class Request:
        method = "POST"
        post_data = urlencode(
            {"data": json.dumps({"pageNumber": 1, "keyword": "test"})}
        )
        url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"

    class Response:
        status = 302

        async def body(self) -> bytes:
            return b'{"ret":["SUCCESS::ok"],"data":{"resultList":[]}}'

    class Route:
        request = Request()
        fetch_options: dict[str, Any] | None = None

        async def fetch(self, **kwargs: Any) -> Response:
            self.fetch_options = kwargs
            return Response()

        async def fulfill(self, **_kwargs: Any) -> None:
            return None

    async def exercise() -> None:
        collector = SearchResponseCollector("test")
        route = Route()
        ticket = collector.arm(1)
        await collector._handle_route(route)  # noqa: SLF001
        capture = await collector.next(ticket, 100)

        assert route.fetch_options == {"max_redirects": 0}
        with pytest.raises(SearchRejectedError, match="HTTP 302"):
            XianyuSpider()._parse_capture(capture)

    asyncio.run(exercise())


def test_advance_page_clicks_real_next_control() -> None:
    expected = CapturedSearchResponse(
        status=200,
        payload={"ret": ["SUCCESS::ok"], "data": {"resultList": []}},
    )

    events: list[str] = []

    class Locator:
        clicked = False
        appeared = False

        async def wait_for(self, **_kwargs: Any) -> None:
            await asyncio.sleep(0)
            self.appeared = True

        async def scroll_into_view_if_needed(self) -> None:
            assert self.appeared
            events.append("scroll")

        async def click(self, **_kwargs: Any) -> None:
            assert self.appeared
            events.append("click")
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
        def arm(self, expected_page: int) -> CaptureTicket:
            events.append("arm")
            return CaptureTicket(1, expected_page)

        def disarm(self, _ticket: CaptureTicket) -> None:
            events.append("disarm")

        async def next(
            self,
            _ticket: CaptureTicket,
            _timeout_ms: int,
        ) -> CapturedSearchResponse:
            events.append("next")
            return expected

    async def exercise() -> None:
        locator = Locator()
        result = await XianyuSpider()._advance_page(  # noqa: SLF001
            Page(locator), Collector(), 2, 3
        )
        assert result is expected
        assert locator.clicked is True
        assert events == ["scroll", "arm", "click", "next", "disarm"]

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
        def arm(self, _expected_page: int) -> CaptureTicket:
            raise AssertionError("collector must not be armed without a next button")

    async def exercise() -> None:
        result = await XianyuSpider(timeout_ms=20_000)._advance_page(  # noqa: SLF001
            Page(), Collector(), 2, 3
        )
        assert result is None

    asyncio.run(exercise())
