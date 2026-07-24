#!/usr/bin/env python3
"""Run persistent Xianyu tasks and emit only newly observed items by default."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from spider import SpiderError, XianyuSpider
from task_manager import TaskManager


async def run_tasks(args: argparse.Namespace) -> tuple[list[dict[str, Any]], bool]:
    manager = TaskManager(args.tasks_file)
    if args.task_id:
        task = manager.get_task(args.task_id)
        if task is None:
            raise ValueError(f"task not found: {args.task_id}")
        tasks = [task]
    else:
        tasks = manager.list_tasks(running_only=True)

    reports: list[dict[str, Any]] = []
    had_error = False
    for task in tasks:
        task_id = task["id"]
        state_file = args.state or task.get("state_file")
        spider = XianyuSpider(
            state_file=state_file,
            proxy=args.proxy,
            headless=not args.headed,
            browser_channel=args.browser_channel,
        )
        try:
            items = await spider.search(
                keyword=task["keyword"],
                min_price=task.get("min_price"),
                max_price=task.get("max_price"),
                location=task.get("location"),
                pages=int(task.get("pages", 1)),
                max_retries=int(task.get("retries", 3)),
            )
            new_items = manager.record_run(task_id, items)
            baseline_count = len(new_items) if args.baseline else 0
            delivered_items = [] if args.baseline else new_items
            report: dict[str, Any] = {
                "ok": True,
                "task_id": task_id,
                "keyword": task["keyword"],
                "criteria": task.get("criteria", ""),
                "pages_scraped": spider.pages_scraped,
                "matched_count": len(items),
                "new_count": len(delivered_items),
                "baseline_count": baseline_count,
                "items": items if args.include_seen else delivered_items,
            }
        except (SpiderError, ValueError) as exc:
            had_error = True
            manager.record_error(task_id, str(exc))
            report = {
                "ok": False,
                "task_id": task_id,
                "keyword": task["keyword"],
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        reports.append(report)
    return reports, had_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Xianyu monitor tasks")
    parser.add_argument("--tasks-file", default="tasks.json")
    parser.add_argument("--task-id")
    parser.add_argument("--state", help="override task login-state path")
    parser.add_argument("--proxy")
    parser.add_argument(
        "--browser-channel", default=os.getenv("XIANYU_BROWSER_CHANNEL")
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--include-seen",
        action="store_true",
        help="include all matched items instead of only newly observed items",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="record current matches as seen without reporting them as new",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reports, had_error = asyncio.run(run_tasks(args))
    except (OSError, TimeoutError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(
        json.dumps(
            {
                "ok": not had_error,
                "task_count": len(reports),
                "new_count": sum(int(report.get("new_count", 0)) for report in reports),
                "tasks": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
