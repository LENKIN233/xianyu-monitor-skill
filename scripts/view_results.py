"""Read result JSON and produce a local, compact listing report."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .listing_filters import normalize_exclusions, title_is_excluded
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from listing_filters import normalize_exclusions, title_is_excluded

VIEW_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 5_000


def _reject_constant(_value: str) -> Any:
    raise ValueError("结果包含无效数字。")


def read_input(path: str) -> Any:
    try:
        if path == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            raw = stream.read(MAX_INPUT_BYTES + 1)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
        else:
            with Path(path).expanduser().open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError("无法读取结果文件。") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("结果文件超过 2 MiB，请减少单次处理的商品数量。")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("需要 search、monitor、analyze 或 demo 输出的 JSON。") from exc


def _text(value: Any, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    # A listing must not inject terminal controls, line breaks, or bidi controls.
    plain = " ".join(value.split())
    return "".join(character for character in plain if character.isprintable())[:limit]


def _public_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("商品列表格式不正确。")  # noqa: TRY004
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
        raise ValueError("商品缺少有效编号。")
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        price = None
    elif price < 0 or price > 1e12 or not math.isfinite(price):
        price = None
    public = {
        "id": item_id,
        "title": _text(item.get("title")) or "未提供标题",
        "price": price,
        "location": _text(item.get("location"), 100),
        "url": "https://www.goofish.com/item?id=" + quote(item_id, safe=""),
    }
    score = item.get("score")
    if type(score) is int and 0 <= score <= 100:
        public["score"] = score
    for field in ("observed_evidence", "uncertainties"):
        values = item.get(field)
        if isinstance(values, list):
            public[field] = [
                _text(value, 160) for value in values[:2] if _text(value, 160)
            ]
    return public


def build_report(
    payload: Any,
    *,
    limit: int = 20,
    sort: str = "original",
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("显示数量应为 1 到 100。")
    if sort not in {"original", "price-asc", "price-desc"}:
        raise ValueError("不支持这种排序方式。")
    terms = normalize_exclusions([] if exclude is None else exclude)
    if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        raise ValueError("需要 search、monitor、analyze 或 demo 输出的 JSON。")
    synthetic = (
        isinstance(payload.get("demo"), dict)
        and payload["demo"].get("status") == "synthetic"
    )
    source = payload.get("search") if synthetic else payload
    if not isinstance(source, dict) or type(source.get("ok")) is not bool:
        raise ValueError("搜索结果格式不正确。")
    failed = payload["ok"] is False or source["ok"] is False
    kind = "search"
    items: list[Any] = []
    if "tasks" in source:
        kind = "monitor"
        tasks = source["tasks"]
        if not isinstance(tasks, list) or len(tasks) > 1_000:
            raise ValueError("监控结果格式不正确。")
        for task in tasks:
            if not isinstance(task, dict) or type(task.get("ok")) is not bool:
                raise ValueError("监控任务格式不正确。")
            failed = failed or not task["ok"]
            task_items = task.get("items", [])
            if not isinstance(task_items, list):
                raise ValueError("监控商品列表格式不正确。")  # noqa: TRY004
            items.extend(task_items)
    elif "items" in source:
        if not isinstance(source["items"], list):
            raise ValueError("商品列表格式不正确。")
        items = source["items"]
        if "run_evidence" in source:
            kind = "analysis"
            source_run = source.get("source_run", {})
            if not isinstance(source_run, dict):
                raise ValueError("AI 分析结果中的运行状态不正确。")
            failed = failed or source_run.get("status") == "failed"
    elif not failed:
        raise ValueError("这份 JSON 不包含商品结果。")
    if len(items) > MAX_ITEMS or any(not isinstance(item, dict) for item in items):
        raise ValueError("商品列表格式不正确或超过 5000 条。")
    # Validate all rows before filtering so malformed data cannot be hidden by a term.
    public_items = [_public_item(item) for item in items]
    selected = [
        public
        for raw, public in zip(items, public_items, strict=True)
        if not title_is_excluded(raw.get("title"), terms)
    ]
    if sort != "original":
        selected.sort(
            key=lambda item: (
                item["price"] is None,
                (item["price"] or 0) * (-1 if sort == "price-desc" else 1),
            )
        )
    return {
        "ok": not failed,
        "schema_version": VIEW_SCHEMA_VERSION,
        "source_kind": kind,
        "synthetic": synthetic,
        "keyword": _text(source.get("keyword"), 100),
        "received_count": len(items),
        "excluded_count": len(items) - len(selected),
        "matched_count": len(selected),
        "shown_count": min(limit, len(selected)),
        "sort": sort,
        "exclude_keywords": terms,
        "items": selected[:limit],
    }


def _markdown(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(text, quote=False))


def render_report(report: dict[str, Any], format: str = "text") -> str:
    if format == "json":
        return json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False)
    markdown = format == "markdown"
    display = _markdown if markdown else lambda value: value
    lines = []
    if report["synthetic"]:
        lines.append("演示数据，未连接闲鱼。")
    if not report["ok"]:
        lines.append(
            "本次运行未完成。下方可能有已返回的商品，请查看原始结果中的错误和保存状态。"
        )
    label = {"search": "搜索结果", "monitor": "监控结果", "analysis": "AI 分析结果"}[
        report["source_kind"]
    ]
    keyword = f" · {display(report['keyword'])}" if report["keyword"] else ""
    lines.append(
        f"{label}{keyword}：共 {report['received_count']} 件，"
        f"显示 {report['shown_count']} 件。"
    )
    if report["excluded_count"]:
        lines.append(f"按标题排除了 {report['excluded_count']} 件商品。")
    if report["shown_count"] < report["matched_count"]:
        lines.append(
            f"还有 {report['matched_count'] - report['shown_count']} 件未显示，"
            "可增大 --limit。"
        )
    if report["matched_count"] == 0 and report["ok"]:
        lines.append("没有符合条件的商品。")
    for index, item in enumerate(report["items"], 1):
        price = f"¥{item['price']:g}" if item["price"] is not None else "价格未提供"
        details = [price, display(item["location"])] if item["location"] else [price]
        if "score" in item:
            details.append(f"匹配分 {item['score']}/100")
        lines.extend(
            ("", f"{index}. {display(item['title'])}", "   " + " · ".join(details))
        )
        link = f"[打开商品]({item['url']})" if markdown else item["url"]
        lines.append("   " + link)
        for field, label in (
            ("observed_evidence", "理由"),
            ("uncertainties", "待确认"),
        ):
            if item.get(field):
                lines.append(f"   {label}：" + display("；".join(item[field])))
    return "\n".join(lines)


def print_report(text: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write((text + "\n").encode("utf-8"))
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Read a listing report without network access or task changes"
    )
    parser.add_argument("--input", default="-", help="result JSON path, or - for stdin")
    parser.add_argument(
        "--sort",
        choices=("original", "price-asc", "price-desc"),
        default="original",
        help="keep original order, or sort by price",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="listings to show (default: 20; max: 100)"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="WORD",
        help="skip titles containing this word; repeat as needed",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_report(
            read_input(args.input),
            limit=args.limit,
            sort=args.sort,
            exclude=args.exclude,
        )
    except ValueError as exc:
        message = "无法整理结果：" + str(exc)
        if args.format == "json":
            print(
                json.dumps(
                    {"ok": False, "error": message, "error_type": "ResultInputError"},
                    ensure_ascii=True,
                )
            )
        else:
            print_report(message)
        return 2
    print_report(render_report(report, args.format))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
