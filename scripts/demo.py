#!/usr/bin/env python3
"""Show the product workflow with synthetic data and no network or local writes."""

from __future__ import annotations

import copy
import json
from typing import Any

if __package__:
    from . import analyze, deliver, evaluate
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .task_manager import outbox_generation_key
else:
    import analyze
    import deliver
    import evaluate
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from task_manager import outbox_generation_key

DEMO_SCHEMA_VERSION = 1
DEMO_TASK_ID = "task_demo_offline"
DEMO_ENDPOINT = "https://demo.invalid/xianyu-webhook"


def _synthetic_search() -> dict[str, Any]:
    return {
        "ok": True,
        "keyword": "MacBook Air M2",
        "criteria": "优先 16GB、上海自提；缺少电池信息时保留不确定性",
        "count": 3,
        "pages_scraped": 1,
        "items": [
            {
                "id": "demo-16gb-local",
                "title": "MacBook Air M2 16GB 512GB 上海自提",
                "price": 5299,
                "location": "上海",
                "publish_time": "2026-08-27 09:30",
                "wants": "18",
                "tags": ["16GB", "自提"],
                "seller": "synthetic-seller-not-forwarded",
                "image": "https://images.invalid/not-forwarded.jpg",
                "url": "javascript:not-forwarded",
            },
            {
                "id": "demo-8gb-remote",
                "title": "MacBook Air M2 8GB 256GB",
                "price": 3999,
                "location": "杭州",
                "publish_time": "2026-08-27 08:15",
                "wants": "6",
                "tags": ["顺丰"],
            },
            {
                "id": "demo-accessory",
                "title": "适用 MacBook Air M2 保护壳",
                "price": 39,
                "location": "广州",
                "publish_time": "2026-08-27 07:40",
                "wants": "2",
                "tags": ["配件"],
            },
        ],
        "search_capability": {"status": "synthetic-demo"},
        "authentication": {"status": "not-used"},
        "cleanup": {"status": "complete-or-not-required"},
    }


def _synthetic_model_output() -> dict[str, Any]:
    return {
        "summary": "第一条最符合 16GB 与上海自提条件；其余证据不足或明显是配件。",
        "items": [
            {
                "source_index": 0,
                "id": "demo-16gb-local",
                "score": 92,
                "match_level": "high_match",
                "observed_evidence": ["标题标明 16GB、512GB 和上海自提"],
                "uncertainties": ["未观察到电池健康与维修历史"],
                "risk_signals": [],
            },
            {
                "source_index": 1,
                "id": "demo-8gb-remote",
                "score": 38,
                "match_level": "low_match",
                "observed_evidence": ["标题标明 8GB，地点为杭州"],
                "uncertainties": ["未观察到电池健康与维修历史"],
                "risk_signals": [],
            },
            {
                "source_index": 2,
                "id": "demo-accessory",
                "score": 3,
                "match_level": "low_match",
                "observed_evidence": ["标题和标签表明这是保护壳配件"],
                "uncertainties": [],
                "risk_signals": ["价格与目标整机不在同一量级"],
            },
        ],
    }


def _demo_event(item: dict[str, Any]) -> dict[str, Any]:
    item_id = str(item["id"])
    key = outbox_generation_key(DEMO_TASK_ID, item_id, 0)
    return {
        "idempotency_key": key,
        "task_id": DEMO_TASK_ID,
        "item_id": item_id,
        "delivery_generation": 0,
        "created_at": "2026-08-27T00:00:00+00:00",
        "payload": {
            "task_id": DEMO_TASK_ID,
            "keyword": "MacBook Air M2",
            "criteria": "优先 16GB、上海自提；缺少电池信息时保留不确定性",
            "item": {
                field: item[field]
                for field in (
                    "id",
                    "title",
                    "price",
                    "location",
                    "publish_time",
                    "wants",
                    "tags",
                    "url",
                )
            },
        },
    }


def build_demo() -> dict[str, Any]:
    """Build deterministic synthetic search, analysis, evaluation, and delivery."""

    search = _synthetic_search()
    listings = analyze.sanitize_results(search, max_items=20)
    public_search = copy.deepcopy(search)
    for item in public_search["items"]:
        item["url"] = f"https://www.goofish.com/item?id={item['id']}"
    provider = analyze._provider_evidence(  # noqa: SLF001 - shared contract demo.
        analyze.DEFAULT_BASE_URL,
        analyze.DEFAULT_MODEL,
    )
    request = analyze.build_request_payload(listings, model=analyze.DEFAULT_MODEL)
    analysis = analyze.validate_analysis(_synthetic_model_output(), listings)
    run_evidence = analyze._run_evidence(  # noqa: SLF001 - shared contract demo.
        provider=provider,
        request_payload=request,
        listings=listings,
        analysis=analysis,
        latency_ms=0,
    )
    golden = [
        {
            "item_id": "demo-16gb-local",
            "expected_label": "relevant",
            "minimum_score": 80,
            "maximum_score": 100,
        },
        {
            "item_id": "demo-accessory",
            "expected_label": "irrelevant",
            "minimum_score": 0,
            "maximum_score": 10,
        },
    ]
    evaluation = evaluate.evaluate_golden(
        {
            item["id"]: {
                "match_level": item["match_level"],
                "score": item["score"],
            }
            for item in analysis["items"]
        },
        golden,
    )
    event = _demo_event(analysis["items"][0])
    delivery_preview = deliver.build_delivery_preview(
        "webhook",
        DEMO_ENDPOINT,
        [event],
    )
    return {
        "ok": True,
        "schema_version": DEMO_SCHEMA_VERSION,
        "demo": {
            "status": "synthetic",
            "network": "not-used",
            "credentials": "not-used",
            "local_writes": "none",
            "claims_real_xianyu_state": False,
        },
        "search": public_search,
        "analysis": {
            "summary": analysis["summary"],
            "items": analysis["items"],
            "run_evidence": run_evidence,
        },
        "evaluation": evaluation,
        "outbox": {
            "event_count": 1,
            "idempotency_key": event["idempotency_key"],
        },
        "delivery_preview": delivery_preview,
        "next_action": {
            "code": "run-setup",
            "hint": (
                "Run xianyu setup with an explicit private state path and keyword "
                "to test one real authorized search."
            ),
        },
    }


def build_parser() -> JsonArgumentParser:
    return JsonArgumentParser(
        description="Show a deterministic offline xianyu-monitor product demo"
    )


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(build_demo(), ensure_ascii=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
