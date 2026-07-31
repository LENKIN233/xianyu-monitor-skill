from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

import monitor
import pytest
import task_manager
from spider import SearchCancelledError, StateFileError
from task_manager import RecordRunProgress, TaskManager, TaskMutationProgress


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
    assert first[0]["search_capability"]["status"] == "passed-for-this-run"
    assert first[0]["persistence"]["status"] == "recorded"
    assert first[0]["authentication"]["status"] == "not-evaluated"
    assert first[0]["identity"]["status"] == "not-evaluated"


def test_monitor_parser_leaves_environment_channel_for_cdp_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XIANYU_BROWSER_CHANNEL", "chrome")

    args = monitor.build_parser().parse_args(
        ["--cdp-user-data-dir", "/private/profile"]
    )

    assert args.browser_channel is None
    assert args.cdp_user_data_dir == "/private/profile"


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


def test_persistence_failure_preserves_successful_search_evidence(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)

    def fail_record_run(
        _manager: TaskManager,
        _task_id: str,
        _items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress,
    ) -> list[dict[str, Any]]:
        assert progress.committed is False
        raise OSError("persistence failed")

    monkeypatch.setattr(TaskManager, "record_run", fail_record_run)
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

    reports, had_error = asyncio.run(monitor.run_tasks(args))

    assert had_error is True
    assert reports[0]["ok"] is False
    assert reports[0]["search_capability"]["status"] == "passed-for-this-run"
    assert reports[0]["matched_count"] == 1
    assert reports[0]["error_type"] == "OSError"
    assert reports[0]["persistence"]["status"] == "not-recorded"


def test_precommit_persistence_error_keeps_cleanup_status_independent(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    real_cleanup = task_manager._cleanup_owned_lock  # noqa: SLF001

    def fail_before_publish(_source: Any, _destination: Any) -> None:
        raise OSError("injected pre-publish failure")

    def cleanup_then_attach_failure(
        lock_file: Path,
        descriptor: int,
        owned_stat: Any,
        primary: BaseException | None = None,
    ) -> BaseException | None:
        error = real_cleanup(lock_file, descriptor, owned_stat, primary)
        if error is not None:
            failures = getattr(error, "cleanup_failures", None)
            if not isinstance(failures, list):
                failures = []
                error.cleanup_failures = failures
            failures.append("injected lock cleanup failure")
        return error

    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    monkeypatch.setattr(task_manager.os, "replace", fail_before_publish)
    monkeypatch.setattr(
        task_manager,
        "_cleanup_owned_lock",
        cleanup_then_attach_failure,
    )

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 2
    payload = json.loads(capsys.readouterr().out)

    report = payload["tasks"][0]
    assert report["search_capability"]["status"] == "passed-for-this-run"
    assert report["persistence"]["status"] == "not-recorded"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["injected lock cleanup failure"],
    }
    assert report["error_recording"]["status"] == "not-attempted"
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == []
    assert stored["last_error"] is None


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_post_commit_interrupt_retains_new_items_in_cancellation_output(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    interruption: type[BaseException],
    error_type: str,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    real_replace = task_manager.os.replace

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise interruption

    monkeypatch.setattr(task_manager.os, "replace", replace_then_interrupt)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_type"] == error_type
    assert payload["new_count"] == 1
    assert payload["task_count"] == 1
    report = payload["tasks"][0]
    assert report["ok"] is True
    assert report["new_count"] == 1
    assert report["items"] == [{"id": "new-1", "title": "新商品", "price": 100}]
    assert report["persistence"]["status"] == "recorded"
    assert report["interruption"] == {
        "status": "cancelled-after-task-commit",
        "error_type": error_type,
    }
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]
    assert not list(tmp_path.glob(".tasks.json.*.tmp"))
    assert not manager.lock_file.exists()


def test_pre_commit_interrupt_reports_no_committed_items(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)

    def interrupt_before_replace(_source: Any, _destination: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(task_manager.os, "replace", interrupt_before_replace)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["new_count"] == 0
    report = payload["tasks"][0]
    assert report["ok"] is False
    assert report["error_type"] == "KeyboardInterrupt"
    assert report["search_capability"]["status"] == "passed-for-this-run"
    assert report["persistence"]["status"] == "not-recorded"
    assert "items" not in report
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == []


@pytest.mark.parametrize(
    ("interruption_factory", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_precommit_interrupt_keeps_cleanup_status_independent(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    interruption_factory: type[BaseException],
    error_type: str,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    real_cleanup = task_manager._cleanup_owned_lock  # noqa: SLF001

    def interrupt_before_publish(_source: Any, _destination: Any) -> None:
        raise interruption_factory

    def cleanup_then_attach_failure(
        lock_file: Path,
        descriptor: int,
        owned_stat: Any,
        primary: BaseException | None = None,
    ) -> BaseException | None:
        error = real_cleanup(lock_file, descriptor, owned_stat, primary)
        if error is not None:
            failures = getattr(error, "cleanup_failures", None)
            if not isinstance(failures, list):
                failures = []
                error.cleanup_failures = failures
            failures.append("injected lock cleanup failure")
        return error

    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    monkeypatch.setattr(task_manager.os, "replace", interrupt_before_publish)
    monkeypatch.setattr(
        task_manager,
        "_cleanup_owned_lock",
        cleanup_then_attach_failure,
    )

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_type"] == error_type
    report = payload["tasks"][0]
    assert report["search_capability"]["status"] == "passed-for-this-run"
    assert report["persistence"]["status"] == "not-recorded"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["injected lock cleanup failure"],
    }
    assert report["error_recording"]["status"] == "not-attempted"
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == []
    assert stored["last_error"] is None


def test_spider_capability_does_not_imply_persistence_was_attempted(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")

    class CompletedThenCancelledSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 1
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            self.last_capability_status = "passed-for-this-run"
            raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "XianyuSpider", CompletedThenCancelledSpider)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["search_capability"]["status"] == "passed-for-this-run"
    report = payload["tasks"][0]
    assert report["search_capability"]["status"] == "passed-for-this-run"
    assert report["persistence"]["status"] == "not-attempted"


def test_cancellation_after_search_return_before_record_run_is_not_recorded(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    gate_calls = 0
    record_run_calls = 0

    def cancel_at_post_search_gate() -> None:
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 2:
            raise asyncio.CancelledError

    def unexpected_record_run(
        _manager: TaskManager,
        _task_id: str,
        _items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress,
    ) -> list[dict[str, Any]]:
        nonlocal record_run_calls
        del progress
        record_run_calls += 1
        return []

    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    monkeypatch.setattr(
        monitor, "_raise_if_task_cancelling", cancel_at_post_search_gate
    )
    monkeypatch.setattr(TaskManager, "record_run", unexpected_record_run)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert gate_calls == 2
    assert record_run_calls == 0
    report = payload["tasks"][0]
    assert report["search_capability"]["status"] == "passed-for-this-run"
    assert report["persistence"]["status"] == "not-recorded"


def test_interrupt_after_record_run_return_uses_external_commit_progress(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FakeSpider)
    real_record_run = TaskManager.record_run

    def commit_then_interrupt(
        task_manager_instance: TaskManager,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress,
    ) -> list[dict[str, Any]]:
        real_record_run(
            task_manager_instance,
            task_id,
            items,
            progress=progress,
        )
        assert progress.committed is True
        raise KeyboardInterrupt

    monkeypatch.setattr(TaskManager, "record_run", commit_then_interrupt)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["new_count"] == 1
    report = payload["tasks"][0]
    assert report["ok"] is True
    assert report["persistence"]["status"] == "recorded"
    assert report["items"][0]["id"] == "new-1"
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]


@pytest.mark.skipif(
    not hasattr(signal, "raise_signal"),
    reason="signal.raise_signal is required",
)
def test_real_sigint_after_first_commit_never_starts_second_task(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    second_task = manager.create_task("second")

    class CountingSpider(FakeSpider):
        calls = 0

        async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
            CountingSpider.calls += 1
            return await super().search(**kwargs)

    real_record_run = TaskManager.record_run
    record_calls = 0

    def commit_then_signal(
        task_manager_instance: TaskManager,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress,
    ) -> list[dict[str, Any]]:
        nonlocal record_calls
        result = real_record_run(
            task_manager_instance,
            task_id,
            items,
            progress=progress,
        )
        record_calls += 1
        if record_calls == 1:
            signal.raise_signal(signal.SIGINT)
        return result

    monkeypatch.setattr(monitor, "XianyuSpider", CountingSpider)
    monkeypatch.setattr(TaskManager, "record_run", commit_then_signal)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert CountingSpider.calls == 1
    assert payload["task_count"] == 1
    assert payload["new_count"] == 1
    assert payload["tasks"][0]["task_id"] == first_task["id"]
    assert payload["tasks"][0]["persistence"]["status"] == "recorded"
    stored_second = TaskManager(str(tasks_file)).get_task(second_task["id"])
    assert stored_second is not None
    assert stored_second["seen_item_ids"] == []


def test_post_commit_finalization_error_retains_items_and_stops_batch(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    manager.create_task("second")

    class CountingSpider(FakeSpider):
        calls = 0

        async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
            CountingSpider.calls += 1
            return await super().search(**kwargs)

    monkeypatch.setattr(monitor, "XianyuSpider", CountingSpider)
    real_cleanup = task_manager._cleanup_owned_lock  # noqa: SLF001

    def cleanup_then_report_error(
        lock_file: Path,
        descriptor: int,
        owned_stat: Any,
        primary: BaseException | None = None,
    ) -> BaseException | None:
        cleanup_error = real_cleanup(
            lock_file,
            descriptor,
            owned_stat,
            primary,
        )
        if cleanup_error is None and primary is None:
            cleanup_error = OSError("simulated lock finalization failure")
            cleanup_error.cleanup_failures = [
                "failed to close the task lock descriptor"
            ]
        return cleanup_error

    monkeypatch.setattr(
        task_manager,
        "_cleanup_owned_lock",
        cleanup_then_report_error,
    )

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert CountingSpider.calls == 1
    assert payload["new_count"] == 1
    report = payload["tasks"][0]
    assert report["ok"] is False
    assert report["new_count"] == 1
    assert report["items"][0]["id"] == "new-1"
    assert report["persistence"]["status"] == "recorded"
    assert report["finalization"]["status"] == "failed"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the task lock descriptor"],
    }
    stored = TaskManager(str(tasks_file)).get_task(first_task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_unknown_task_commit_on_cancellation_retains_possible_items(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    interruption: type[BaseException],
    error_type: str,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    second_task = manager.create_task("second")

    class CountingSpider(FakeSpider):
        calls = 0

        async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
            CountingSpider.calls += 1
            return await super().search(**kwargs)

    real_replace = task_manager.os.replace
    real_lstat = Path.lstat
    publish_happened = False
    reconciliation_failed = False

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        nonlocal publish_happened
        real_replace(source, destination)
        publish_happened = True
        raise interruption

    def fail_one_reconciliation(path: Path) -> Any:
        nonlocal reconciliation_failed
        if publish_happened and not reconciliation_failed and path == tasks_file:
            reconciliation_failed = True
            raise PermissionError("simulated task commit reconciliation denial")
        return real_lstat(path)

    monkeypatch.setattr(monitor, "XianyuSpider", CountingSpider)
    monkeypatch.setattr(task_manager.os, "replace", replace_then_interrupt)
    monkeypatch.setattr(Path, "lstat", fail_one_reconciliation)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert CountingSpider.calls == 1
    assert payload["error_type"] == error_type
    assert payload["new_count"] == 1
    report = payload["tasks"][0]
    assert report["ok"] is False
    assert report["items"][0]["id"] == "new-1"
    assert report["persistence"] == {
        "status": "not-established",
        "possible_duplicate": True,
    }
    assert report["interruption"]["status"] == (
        "cancelled-with-task-commit-not-established"
    )
    stored_first = TaskManager(str(tasks_file)).get_task(first_task["id"])
    assert stored_first is not None
    assert stored_first["seen_item_ids"] == ["new-1"]
    stored_second = TaskManager(str(tasks_file)).get_task(second_task["id"])
    assert stored_second is not None
    assert stored_second["seen_item_ids"] == []


def test_unknown_task_commit_on_ordinary_error_retains_possible_items(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    manager.create_task("second")

    class CountingSpider(FakeSpider):
        calls = 0

        async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
            CountingSpider.calls += 1
            return await super().search(**kwargs)

    real_replace = task_manager.os.replace
    real_lstat = Path.lstat
    publish_happened = False
    reconciliation_failed = False

    def replace_then_fail(source: Any, destination: Any) -> None:
        nonlocal publish_happened
        real_replace(source, destination)
        publish_happened = True
        raise OSError("simulated replace return failure")

    def fail_one_reconciliation(path: Path) -> Any:
        nonlocal reconciliation_failed
        if publish_happened and not reconciliation_failed and path == tasks_file:
            reconciliation_failed = True
            raise PermissionError("simulated task commit reconciliation denial")
        return real_lstat(path)

    monkeypatch.setattr(monitor, "XianyuSpider", CountingSpider)
    monkeypatch.setattr(task_manager.os, "replace", replace_then_fail)
    monkeypatch.setattr(Path, "lstat", fail_one_reconciliation)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert CountingSpider.calls == 1
    assert payload["new_count"] == 1
    report = payload["tasks"][0]
    assert report["ok"] is False
    assert report["items"][0]["id"] == "new-1"
    assert report["persistence"] == {
        "status": "not-established",
        "possible_duplicate": True,
    }
    assert report["finalization"]["status"] == "commit-status-not-established"
    stored = TaskManager(str(tasks_file)).get_task(first_task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]


def test_cleanup_failure_stops_before_error_recording_and_later_tasks(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    manager.create_task("second")

    class CleanupFailingSpider:
        calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            CleanupFailingSpider.calls += 1
            error = monitor.SpiderError("browser cleanup failed")
            error.cleanup_failures = ["failed to close the dedicated search browser"]
            raise error

    record_error_calls = 0

    def record_error_unexpected(
        _manager: TaskManager,
        _task_id: str,
        _error: str,
        *,
        progress: TaskMutationProgress,
    ) -> bool:
        nonlocal record_error_calls
        del progress
        record_error_calls += 1
        return True

    monkeypatch.setattr(monitor, "XianyuSpider", CleanupFailingSpider)
    monkeypatch.setattr(TaskManager, "record_error", record_error_unexpected)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert CleanupFailingSpider.calls == 1
    assert record_error_calls == 0
    assert payload["task_count"] == 1
    report = payload["tasks"][0]
    assert report["task_id"] == first_task["id"]
    assert report["error_recording"]["status"] == "not-attempted"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the dedicated search browser"],
    }


def test_error_recording_cleanup_failure_stops_later_tasks(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    manager.create_task("second")

    class FailingSpider:
        calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            FailingSpider.calls += 1
            raise monitor.SpiderError("search failed")

    def fail_error_recording(
        _manager: TaskManager,
        _task_id: str,
        _error: str,
        *,
        progress: TaskMutationProgress,
    ) -> bool:
        del progress
        error = OSError("task lock finalization failed")
        error.cleanup_failures = ["failed to remove the owned task lock"]
        raise error

    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
    monkeypatch.setattr(TaskManager, "record_error", fail_error_recording)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert FailingSpider.calls == 1
    assert payload["task_count"] == 1
    report = payload["tasks"][0]
    assert report["task_id"] == first_task["id"]
    assert report["error"] == "search failed"
    assert report["error_recording"]["status"] == "not-recorded"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove the owned task lock"],
    }


def test_local_state_error_does_not_claim_search_rejection(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class InvalidStateSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise StateFileError("invalid local state")

    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", InvalidStateSpider)
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

    reports, had_error = asyncio.run(monitor.run_tasks(args))

    assert had_error is True
    assert reports[0]["search_capability"]["status"] == "not-established"


def test_missing_task_during_error_recording_is_reported_not_recorded(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FailingSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise monitor.SpiderError("search failed")

    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
    monkeypatch.setattr(
        TaskManager,
        "record_error",
        lambda *_args, **_kwargs: False,
    )
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

    reports, had_error = asyncio.run(monitor.run_tasks(args))

    assert had_error is True
    assert reports[0]["error"] == "search failed"
    assert reports[0]["error_recording"]["status"] == "not-recorded"


def test_spider_constructor_failure_becomes_structured_task_report(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FailingSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            raise ValueError("invalid browser configuration")

    tasks_file = tmp_path / "tasks.json"
    task = TaskManager(str(tasks_file)).create_task("测试")
    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
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

    reports, had_error = asyncio.run(monitor.run_tasks(args))

    assert had_error is True
    assert reports == [
        {
            "ok": False,
            "task_id": task["id"],
            "keyword": "测试",
            "error": "invalid browser configuration",
            "error_type": "ValueError",
            "search_capability": {"status": "not-established"},
            "persistence": {"status": "not-attempted"},
            "authentication": {"status": "not-evaluated"},
            "identity": {"status": "not-evaluated"},
            "cleanup": {"status": "complete-or-not-required"},
            "error_recording": {"status": "recorded"},
        }
    ]


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
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error_type"] == "ValueError"
    assert "task file does not exist" in payload["error"]
    assert payload["task_count"] == 0
    assert payload["new_count"] == 0
    assert payload["tasks"] == []
    assert payload["search_capability"]["status"] == "not-established"
    assert payload["authentication"]["status"] == "not-evaluated"
    assert payload["identity"]["status"] == "not-evaluated"
    assert payload["cleanup"]["status"] == "complete-or-not-required"


def test_monitor_cancellation_is_structured(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    async def cancel(
        _args: argparse.Namespace,
        **_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], bool]:
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "run_tasks", cancel)

    assert monitor.main(["--tasks-file", "/unused/tasks.json"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_type"] == "CancelledError"
    assert payload["search_capability"]["status"] == "not-established"
    assert payload["cleanup"]["status"] == "complete-or-not-required"


def test_cancellation_while_building_success_output_retains_committed_reports(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    report = {
        "ok": True,
        "task_id": "task_done",
        "keyword": "done",
        "new_count": 1,
        "items": [{"id": "new-1"}],
        "search_capability": {"status": "passed-for-this-run"},
        "persistence": {"status": "recorded"},
    }

    async def complete(
        _args: argparse.Namespace,
        *,
        progress: monitor.MonitorRunProgress,
    ) -> tuple[list[dict[str, Any]], bool]:
        progress.reports.append(report)
        progress.current_capability_status = "passed-for-this-run"
        return progress.reports, False

    real_dumps = monitor.json.dumps
    interrupted = False

    def interrupt_first_success_payload(payload: Any, **kwargs: Any) -> str:
        nonlocal interrupted
        if not interrupted and isinstance(payload, dict) and payload.get("ok") is True:
            interrupted = True
            raise KeyboardInterrupt
        return real_dumps(payload, **kwargs)

    monkeypatch.setattr(monitor, "run_tasks", complete)
    monkeypatch.setattr(monitor.json, "dumps", interrupt_first_success_payload)

    assert monitor.main(["--tasks-file", "/unused/tasks.json"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["new_count"] == 1
    assert payload["tasks"] == [report]
    assert payload["search_capability"]["status"] == "passed-for-this-run"


def test_cleanup_cancellation_stops_before_later_tasks(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.create_task("first")
    manager.create_task("second")

    class CancellingSpider:
        calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            CancellingSpider.calls += 1
            error = SearchCancelledError(
                "search was cancelled; browser cleanup was incomplete",
                capability_status="not-established",
            )
            error.cleanup_failures = ["failed to close the dedicated search browser"]
            raise error

    monkeypatch.setattr(monitor, "XianyuSpider", CancellingSpider)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert CancellingSpider.calls == 1
    assert payload["task_count"] == 1
    assert payload["tasks"][0]["error_type"] == "SearchCancelledError"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the dedicated search browser"],
    }


def test_cancellation_preserves_completed_task_reports(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.create_task("first")
    manager.create_task("second")

    class SecondTaskCancels:
        calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 1

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            SecondTaskCancels.calls += 1
            if SecondTaskCancels.calls == 1:
                return [{"id": "new-1", "title": "first", "price": 1}]
            raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "XianyuSpider", SecondTaskCancels)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert SecondTaskCancels.calls == 2
    assert payload["task_count"] == 2
    assert payload["new_count"] == 1
    assert payload["tasks"][0]["ok"] is True
    assert payload["tasks"][0]["search_capability"]["status"] == "passed-for-this-run"
    assert payload["tasks"][1]["ok"] is False
    assert payload["tasks"][1]["search_capability"]["status"] == "not-established"


def test_cancellation_during_error_recording_preserves_primary_error(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    TaskManager(str(tasks_file)).create_task("first")

    class RejectedSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise StateFileError("candidate state is invalid")

    def interrupt_error_recording(
        _manager: TaskManager,
        _task_id: str,
        _error: str,
        *,
        progress: TaskMutationProgress,
    ) -> bool:
        del progress
        raise KeyboardInterrupt

    monkeypatch.setattr(monitor, "XianyuSpider", RejectedSpider)
    monkeypatch.setattr(TaskManager, "record_error", interrupt_error_recording)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["task_count"] == 1
    report = payload["tasks"][0]
    assert report["error_type"] == "StateFileError"
    assert report["error"] == "candidate state is invalid"
    assert report["interruption"]["error_type"] == "KeyboardInterrupt"
    assert report["error_recording"]["status"] == "not-recorded"
    assert report["search_capability"]["status"] == "not-established"


@pytest.mark.parametrize(
    ("failure_factory", "expected_exit_code"),
    [
        (KeyboardInterrupt, 130),
        (OSError, 2),
    ],
    ids=["keyboard-interrupt", "ordinary-error"],
)
def test_error_recording_failure_after_atomic_publish_reports_recorded(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    failure_factory: type[BaseException],
    expected_exit_code: int,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("first")

    class FailingSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise monitor.SpiderError("primary search failure")

    real_replace = task_manager.os.replace

    def replace_then_fail(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise failure_factory("injected post-publish failure")

    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
    monkeypatch.setattr(task_manager.os, "replace", replace_then_fail)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == expected_exit_code
    payload = json.loads(capsys.readouterr().out)

    report = payload["tasks"][0]
    assert report["error"] == "primary search failure"
    assert report["error_type"] == "SpiderError"
    assert report["error_recording"]["status"] == "recorded"
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["last_error"] == "primary search failure"


@pytest.mark.parametrize(
    ("failure_factory", "expected_exit_code"),
    [
        (KeyboardInterrupt, 130),
        (OSError, 2),
    ],
    ids=["keyboard-interrupt", "ordinary-error"],
)
def test_error_recording_reconciliation_failure_reports_not_established(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    failure_factory: type[BaseException],
    expected_exit_code: int,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("first")

    class FailingSpider:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise monitor.SpiderError("primary search failure")

    real_replace = task_manager.os.replace
    real_lstat = Path.lstat
    publish_happened = False
    reconciliation_failed = False

    def replace_then_fail(source: Any, destination: Any) -> None:
        nonlocal publish_happened
        real_replace(source, destination)
        publish_happened = True
        raise failure_factory("injected post-publish failure")

    def fail_one_reconciliation(path: Path) -> Any:
        nonlocal reconciliation_failed
        if publish_happened and not reconciliation_failed and path == tasks_file:
            reconciliation_failed = True
            raise PermissionError("injected reconciliation denial")
        return real_lstat(path)

    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
    monkeypatch.setattr(task_manager.os, "replace", replace_then_fail)
    monkeypatch.setattr(Path, "lstat", fail_one_reconciliation)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == expected_exit_code
    payload = json.loads(capsys.readouterr().out)

    report = payload["tasks"][0]
    assert report["error"] == "primary search failure"
    assert report["error_type"] == "SpiderError"
    assert report["error_recording"]["status"] == "not-established"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["failed to reconcile the atomic task-file commit"],
    }
    stored = TaskManager(str(tasks_file)).get_task(task["id"])
    assert stored is not None
    assert stored["last_error"] == "primary search failure"


@pytest.mark.skipif(
    not hasattr(signal, "raise_signal"),
    reason="signal.raise_signal is required",
)
def test_real_sigint_after_error_record_commit_preserves_recorded_evidence(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    first_task = manager.create_task("first")
    second_task = manager.create_task("second")

    class FailingSpider:
        calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pages_scraped = 0
            self.last_capability_status = "not-established"

        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            FailingSpider.calls += 1
            raise monitor.SpiderError("primary search failure")

    real_record_error = TaskManager.record_error
    record_calls = 0

    def record_then_signal(
        manager_instance: TaskManager,
        task_id: str,
        error: str,
        *,
        progress: TaskMutationProgress,
    ) -> bool:
        nonlocal record_calls
        result = real_record_error(
            manager_instance,
            task_id,
            error,
            progress=progress,
        )
        record_calls += 1
        if record_calls == 1:
            signal.raise_signal(signal.SIGINT)
        return result

    monkeypatch.setattr(monitor, "XianyuSpider", FailingSpider)
    monkeypatch.setattr(TaskManager, "record_error", record_then_signal)

    assert monitor.main(["--tasks-file", str(tasks_file)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert FailingSpider.calls == 1
    assert payload["task_count"] == 1
    report = payload["tasks"][0]
    assert report["task_id"] == first_task["id"]
    assert report["error"] == "primary search failure"
    assert report["error_recording"]["status"] == "recorded"
    stored_first = TaskManager(str(tasks_file)).get_task(first_task["id"])
    assert stored_first is not None
    assert stored_first["last_error"] == "primary search failure"
    stored_second = TaskManager(str(tasks_file)).get_task(second_task["id"])
    assert stored_second is not None
    assert stored_second["last_error"] is None
