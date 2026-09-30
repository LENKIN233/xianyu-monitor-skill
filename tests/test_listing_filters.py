from __future__ import annotations

import asyncio

import pytest
from listing_filters import exclude_titles, normalize_exclusions
from spider import XianyuSpider


def test_title_filters_are_literal_normalized_and_leave_unknown_titles() -> None:
    items = [
        {"id": "1", "title": "ｉＰｈｏｎｅ CASE"},
        {"id": "2", "title": "iPhone 手机", "tags": ["case"]},
        {"id": "3"},
        {"id": "4", "title": "求购手机"},
    ]
    terms = [" ＣＡＳＥ ", "case", "求购"]
    assert normalize_exclusions(terms) == ["case", "求购"]
    assert [item["id"] for item in exclude_titles(items, terms)] == ["2", "3"]


@pytest.mark.parametrize(
    "terms", ["配件", [None], [" "], ["x" * 81], ["a\nb"], ["x"] * 21]
)
def test_invalid_filters_are_rejected(terms: object) -> None:
    with pytest.raises(ValueError):
        normalize_exclusions(terms)


def test_search_combines_title_exclusion_with_existing_price_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spider = XianyuSpider()

    async def captured(_keyword: str, _pages: int) -> list[dict]:
        return [
            {"id": "1", "title": "相机", "price": 2000},
            {"id": "2", "title": "相机保护壳", "price": 100},
            {"id": "3", "title": "相机套装", "price": 4000},
        ]

    monkeypatch.setattr(spider, "_search_once", captured)
    result = asyncio.run(
        spider.search("相机", max_price=3000, exclude_keywords=["保护壳"])
    )
    assert [item["id"] for item in result] == ["1"]


def test_invalid_exclusions_fail_before_browser_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spider = XianyuSpider()

    async def forbidden(*_args: object) -> list:
        pytest.fail("invalid filters must fail before a browser search")

    monkeypatch.setattr(spider, "_search_once", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(spider.search("相机", exclude_keywords=[" "]))
