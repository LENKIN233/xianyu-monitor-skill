from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import monitor
import pytest
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


def test_quiet_if_empty_suppresses_stdout_and_routine_stderr(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    class NoisyFakeSpider:
        def __init__(self, *_args: Any, verbose: bool = True, **_kwargs: Any):
            self.pages_scraped = 1
            self.verbose = verbose

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            if self.verbose:
                print("[search] routine progress", file=sys.stderr)
            return [{"id": "existing-1", "title": "存量商品", "price": 100}]

    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    manager.record_run(
        task["id"],
        [{"id": "existing-1", "title": "存量商品", "price": 100}],
    )
    monkeypatch.setattr(monitor, "XianyuSpider", NoisyFakeSpider)

    result = monitor.main(
        [
            "--tasks-file",
            str(tasks_file),
            "--quiet-if-empty",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out == ""
    assert captured.err == ""


def test_quiet_if_empty_never_hides_failures(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    class FailingSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise monitor.SpiderError("blocked")

    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)

    result = monitor.main(["--tasks-file", str(tasks_file), "--quiet-if-empty"])
    captured = capsys.readouterr()

    assert result == 2
    assert '"ok": false' in captured.out
    assert "blocked" in captured.out


def test_quiet_if_empty_and_include_seen_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        monitor.build_parser().parse_args(["--quiet-if-empty", "--include-seen"])


def test_quiet_if_empty_and_baseline_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        monitor.build_parser().parse_args(["--quiet-if-empty", "--baseline"])


def test_explicit_stopped_task_is_rejected_before_browser_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class UnexpectedSpider:
        starts = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            UnexpectedSpider.starts += 1

    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    manager.set_status(task["id"], "stopped")
    monkeypatch.setattr(monitor, "XianyuSpider", UnexpectedSpider)
    args = argparse.Namespace(
        tasks_file=str(tasks_file),
        task_id=task["id"],
        state=None,
        proxy=None,
        headed=False,
        browser_channel=None,
        include_seen=False,
        baseline=False,
    )

    with pytest.raises(ValueError, match="task is not running"):
        asyncio.run(monitor.run_tasks(args))

    assert UnexpectedSpider.starts == 0


def test_legacy_relative_state_is_rejected_before_browser_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class UnexpectedSpider:
        starts = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            UnexpectedSpider.starts += 1

    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": [
                    {
                        "id": "task_first",
                        "keyword": "先执行的任务",
                        "status": "running",
                        "state_file": None,
                    },
                    {
                        "id": "task_legacy",
                        "keyword": "测试",
                        "status": "running",
                        "state_file": "state.json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "XianyuSpider", UnexpectedSpider)
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

    with pytest.raises(ValueError, match="legacy relative login-state path"):
        asyncio.run(monitor.run_tasks(args))

    assert UnexpectedSpider.starts == 0
    first = TaskManager(str(tasks_file)).get_task("task_first")
    assert first is not None
    assert first["seen_item_ids"] == []


def test_missing_task_file_is_never_a_silent_success(
    tmp_path: Path, capsys: Any
) -> None:
    missing = tmp_path / "missing/tasks.json"

    result = monitor.main(["--tasks-file", str(missing), "--quiet-if-empty"])
    captured = capsys.readouterr()

    assert result == 2
    assert '"ok": false' in captured.out
    assert "task file does not exist" in captured.out
