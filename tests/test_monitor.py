from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import monitor
from task_manager import TaskManager


class FakeSpider:
    def __init__(self, *_args: Any, **_kwargs: Any):
        self.pages_scraped = 2

    async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "new-1", "title": "新商品", "price": 100}]


def test_monitor_persists_seen_items(tmp_path: Path, monkeypatch: Any) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    args = argparse.Namespace(
        tasks_file=str(tasks_file),
        task_id=None,
        state=None,
        proxy=None,
        headed=False,
        browser_channel=None,
        include_seen=False,
        baseline=False,
    )

    first, first_error = asyncio.run(monitor.run_tasks(args))
    second, second_error = asyncio.run(monitor.run_tasks(args))

    assert first_error is False
    assert second_error is False
    assert first[0]["new_count"] == 1
    assert second[0]["new_count"] == 0


def test_monitor_baseline_suppresses_existing_items(
    tmp_path: Path, monkeypatch: Any
) -> None:
    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    args = argparse.Namespace(
        tasks_file=str(tasks_file),
        task_id=None,
        state=None,
        proxy=None,
        headed=False,
        browser_channel=None,
        include_seen=False,
        baseline=True,
    )

    reports, had_error = asyncio.run(monitor.run_tasks(args))

    assert had_error is False
    assert reports[0]["new_count"] == 0
    assert reports[0]["baseline_count"] == 1
    assert reports[0]["items"] == []
