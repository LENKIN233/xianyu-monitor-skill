from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import task_manager
from task_manager import (
    RecordRunProgress,
    TaskManager,
    TaskMutationCommittedError,
    TaskMutationInterrupted,
    TaskMutationPersistenceError,
    TaskMutationProgress,
    _exclusive_lock,
    _raise_if_async_task_cancelling,
)


def test_task_ids_are_unique_and_duplicates_are_stable(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    first = manager.create_task("iPhone", skip_duplicate=False)
    second = manager.create_task("MacBook", skip_duplicate=False)
    duplicate = manager.create_task("iPhone")

    assert first["id"] != second["id"]
    assert duplicate["id"] == first["id"]
    assert duplicate["existing"] is True
    assert len(manager.list_tasks()) == 2


def test_record_run_returns_only_new_items(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    task = manager.create_task("相机")
    first = manager.record_run(
        task["id"],
        [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
    )
    second = manager.record_run(
        task["id"],
        [{"id": "2", "title": "B"}, {"id": "3", "title": "C"}],
    )

    assert [item["id"] for item in first] == ["1", "2"]
    assert [item["id"] for item in second] == ["3"]
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["1", "2", "3"]
    assert stored["results_count"] == 4


def test_task_file_is_private_and_atomic_schema_is_present(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    manager = TaskManager(str(task_file))
    manager.create_task("键盘")

    if os.name != "nt":
        assert stat.S_IMODE(task_file.stat().st_mode) == 0o600
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2


def test_atomic_publish_does_not_chmod_a_replaced_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_file = tmp_path / "tasks.json"
    manager = TaskManager(str(task_file))

    def reject_path_chmod(
        _path: Path,
        _mode: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del follow_symlinks
        raise AssertionError("published task paths must not be chmod-ed by name")

    monkeypatch.setattr(Path, "chmod", reject_path_chmod)

    manager.create_task("键盘")

    assert task_file.exists()
    if os.name != "nt":
        assert stat.S_IMODE(task_file.stat().st_mode) == 0o600


def test_atomic_publish_does_not_confirm_a_replaced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_file = tmp_path / "tasks.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"replacement": true}\n', encoding="utf-8")
    manager = TaskManager(str(task_file))
    progress = task_manager.TaskMutationProgress()
    real_replace = os.replace
    publish_count = 0

    def publish_then_replace(source: Any, destination: Any) -> None:
        nonlocal publish_count
        real_replace(source, destination)
        publish_count += 1
        if publish_count == 1:
            real_replace(replacement, destination)

    monkeypatch.setattr(os, "replace", publish_then_replace)

    with pytest.raises(
        OSError,
        match="replaced before commit confirmation",
    ) as caught:
        manager.create_task("键盘", progress=progress)

    assert caught.value.task_commit_status == "not-recorded"
    assert caught.value.persistence_status == "not-recorded"
    assert progress.committed is False
    assert progress.task_commit_status == "not-recorded"
    assert task_file.read_text(encoding="utf-8") == '{"replacement": true}\n'


def test_price_validation_and_status_updates(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    with pytest.raises(ValueError, match="minimum price"):
        manager.create_task("坏条件", min_price=2_000, max_price=1_000)

    task = manager.create_task("显示器")
    assert manager.set_status(task["id"], "stopped") is True
    assert manager.list_tasks(running_only=True) == []
    assert manager.set_status(task["id"], "running") is True


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["min_price", "max_price"])
def test_nonfinite_prices_are_rejected(
    tmp_path: Path,
    field: str,
    price: float,
) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))

    with pytest.raises(ValueError, match="price must be finite"):
        manager.create_task("坏条件", **{field: price})

    assert not manager.data_file.exists()


def test_loaded_arbitrary_precision_integer_price_remains_valid(
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "tasks.json"
    enormous_price = 10**1000
    task_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": [
                    {
                        "id": "task_enormous_price",
                        "keyword": "test",
                        "max_price": enormous_price,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    task = TaskManager(str(task_file)).get_task("task_enormous_price")

    assert task is not None
    assert task["max_price"] == enormous_price


def test_cli_reports_nonfinite_price_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks_file = tmp_path / "tasks.json"

    exit_code = task_manager.main(
        ["--data-file", str(tasks_file), "create", "相机", "--min-price", "nan"]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["error_type"] == "ValueError"
    assert report["error"] == "minimum price must be finite"
    assert not tasks_file.exists()


def test_invalid_task_file_is_not_silently_erased(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid task file"):
        TaskManager(str(task_file))


@pytest.mark.parametrize("schema_version", [None, 0, 3, "2", True])
def test_invalid_schema_versions_are_rejected(
    tmp_path: Path,
    schema_version: object,
) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text(
        json.dumps({"schema_version": schema_version, "tasks": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version must be 1 or 2"):
        TaskManager(str(task_file))


@pytest.mark.parametrize("schema_version", [pytest.param(None, id="missing"), 1])
def test_legacy_task_files_remain_compatible(
    tmp_path: Path,
    schema_version: int | None,
) -> None:
    task_file = tmp_path / "tasks.json"
    payload: dict[str, Any] = {
        "tasks": [
            {
                "id": "task_legacy",
                "keyword": "相机",
                "max_price": 5000,
                "min_price": 1000,
                "criteria": "成色良好",
                "location": "上海",
                "notification": {"channel": "feishu", "enabled": True},
                "status": "running",
                "created_at": "2025-01-01T00:00:00",
                "last_run": None,
                "results_count": 0,
                "last_results": [],
            }
        ]
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    task_file.write_text(json.dumps(payload), encoding="utf-8")

    task = TaskManager(str(task_file)).get_task("task_legacy")

    assert task is not None
    assert task["pages"] == 1
    assert task["retries"] == 3
    assert task["seen_item_ids"] == []
    assert "notification" not in task


def test_non_object_task_entry_is_rejected_without_rewriting_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    original = '{"schema_version": 2, "tasks": ["corrupt"]}\n'
    task_file.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match=r"tasks\[0\] must be an object"):
        TaskManager(str(task_file))

    assert task_file.read_text(encoding="utf-8") == original

    exit_code = task_manager.main(["--data-file", str(task_file), "list"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["error_type"] == "ValueError"
    assert "tasks[0] must be an object" in report["error"]
    assert task_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", 7, "id must be a non-empty string"),
        ("keyword", "", "keyword must be a non-empty string"),
        ("min_price", "100", "minimum price must be a number"),
        ("max_price", -1, "maximum price must not be negative"),
        ("pages", True, "pages must be an integer of at least 1"),
        ("retries", 0, "retries must be an integer of at least 1"),
        ("status", "paused", "status must be running or stopped"),
        ("state_file", 7, "state_file must be a string or null"),
        ("results_count", -1, "results_count must be a non-negative integer"),
        ("last_results", ["not-an-object"], "last_results entries must be objects"),
        ("seen_item_ids", [None], "seen_item_ids entries must be non-empty strings"),
    ],
)
def test_loaded_task_fields_are_strictly_validated(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    task_file = tmp_path / "tasks.json"
    task = {"id": "task_valid", "keyword": "相机", field: value}
    task_file.write_text(
        json.dumps({"schema_version": 2, "tasks": [task]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        TaskManager(str(task_file))


def test_duplicate_task_ids_are_rejected(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": [
                    {"id": "task_duplicate", "keyword": "相机"},
                    {"id": "task_duplicate", "keyword": "电脑"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate task id: task_duplicate"):
        TaskManager(str(task_file))


def test_task_save_rejects_nonfinite_nested_values(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    manager = TaskManager(str(task_file))
    manager.tasks = [
        {
            "id": "task_direct",
            "keyword": "相机",
            "last_results": [{"price": float("nan")}],
        }
    ]

    with pytest.raises(ValueError, match="Out of range float values"):
        manager._save()  # noqa: SLF001 - exercise the serialization guard directly.

    assert not task_file.exists()
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []


def test_cli_json_serialization_rejects_nonfinite_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def return_nonfinite_result(
        _manager: TaskManager,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, float]:
        return {"price": float("nan")}

    monkeypatch.setattr(TaskManager, "create_task", return_nonfinite_result)

    exit_code = task_manager.main(
        ["--data-file", str(tmp_path / "tasks.json"), "create", "相机"]
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["error_type"] == "ValueError"
    assert "Out of range float values" in report["error"]
    assert "NaN" not in output


def test_criteria_round_trips_as_an_analysis_hint(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    task = manager.create_task(
        "MacBook",
        criteria="Prefer title/tags mentioning 16GB",
    )

    stored = manager.get_task(task["id"])

    assert stored is not None
    assert stored["criteria"] == "Prefer title/tags mentioning 16GB"


def test_relative_state_path_is_resolved_from_task_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "private"
    elsewhere = tmp_path / "elsewhere"
    tasks_dir.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    manager = TaskManager(str(tasks_dir / "tasks.json"))

    task = manager.create_task("相机", state_file="state.json")

    assert task["state_file"] == os.path.abspath(tasks_dir / "state.json")


def test_legacy_relative_state_path_is_not_silently_redirected(
    tmp_path: Path,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": [
                    {
                        "id": "task_legacy",
                        "keyword": "相机",
                        "state_file": "state.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    task = TaskManager(str(tasks_file)).get_task("task_legacy")

    assert task is not None
    assert task["state_file"] == "state.json"


@pytest.mark.skipif(os.name == "nt", reason="symlink rotation is POSIX-only")
def test_state_path_preserves_stable_symlink_across_rotation(
    tmp_path: Path,
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    state_v1 = private_dir / "state-v1.json"
    state_v2 = private_dir / "state-v2.json"
    state_link = private_dir / "state.json"
    state_v1.write_text('{"version": 1}\n', encoding="utf-8")
    state_v2.write_text('{"version": 2}\n', encoding="utf-8")
    state_link.symlink_to(state_v1.name)

    tasks_file = private_dir / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机", state_file="state.json")

    assert task["state_file"] == os.path.abspath(state_link)
    assert Path(task["state_file"]).is_symlink()

    state_link.unlink()
    state_link.symlink_to(state_v2.name)
    state_v1.unlink()
    stored = TaskManager(str(tasks_file)).get_task(task["id"])

    assert stored is not None
    stored_state = Path(stored["state_file"])
    assert stored_state.is_symlink()
    assert stored_state.read_text(encoding="utf-8") == '{"version": 2}\n'


def test_deduplication_includes_all_task_definition_fields(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    original = manager.create_task(
        "MacBook",
        criteria="16GB",
        pages=1,
        state_file="first-state.json",
    )
    revised = manager.create_task(
        "MacBook",
        criteria="24GB",
        pages=2,
        state_file="second-state.json",
    )

    assert revised["id"] != original["id"]
    assert len(manager.list_tasks()) == 2


def test_record_run_interrupted_after_replace_carries_committed_new_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    item = {"id": "new-1", "title": "原始标题"}
    real_replace = os.replace

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", replace_then_interrupt)

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_run(task["id"], [item])

    interruption = caught.value
    assert isinstance(interruption.cause_error, KeyboardInterrupt)
    assert interruption.committed is True
    assert interruption.task_commit_status == "recorded"
    assert interruption.persistence_status == "recorded"
    assert interruption.result == [item]
    assert interruption.possible_result is None
    assert interruption.result[0] is not item
    item["title"] = "调用方随后修改"
    assert interruption.result == [{"id": "new-1", "title": "原始标题"}]

    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_record_run_interrupted_before_replace_is_not_reported_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")

    def interrupt_before_replace(_source: Any, _destination: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt_before_replace)

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_run(task["id"], [{"id": "not-committed"}])

    interruption = caught.value
    assert isinstance(interruption.cause_error, KeyboardInterrupt)
    assert interruption.committed is False
    assert interruption.task_commit_status == "not-recorded"
    assert interruption.persistence_status == "not-recorded"
    assert interruption.result is None
    assert interruption.possible_result is None
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == []
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_temporary_file_is_removed_when_initial_fstat_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    real_fstat = os.fstat
    calls = 0

    def interrupt_temporary_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        # The first call identifies the lock. The second is the initial
        # temporary-file check; cleanup must retry it before unlinking.
        if calls == 2:
            raise KeyboardInterrupt
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", interrupt_temporary_fstat)

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_run(task["id"], [{"id": "not-committed"}])

    assert caught.value.committed is False
    assert caught.value.result is None
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_record_run_cancelled_after_replace_has_the_same_commit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    cancellation = asyncio.CancelledError()
    real_replace = os.replace

    def replace_then_cancel(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise cancellation

    monkeypatch.setattr(os, "replace", replace_then_cancel)

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_run(task["id"], [{"id": "new-1"}])

    assert caught.value.cause_error is cancellation
    assert caught.value.committed is True
    assert caught.value.result == [{"id": "new-1"}]
    assert not manager.lock_file.exists()


@pytest.mark.parametrize(
    "interruption_factory",
    [KeyboardInterrupt, asyncio.CancelledError],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_record_error_interrupted_after_replace_carries_recorded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_factory: type[BaseException],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    progress = TaskMutationProgress()
    interruption = interruption_factory()
    real_replace = os.replace

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise interruption

    monkeypatch.setattr(os, "replace", replace_then_interrupt)

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_error(
            task["id"],
            "search failed",
            progress=progress,
        )

    assert caught.value.cause_error is interruption
    assert caught.value.task_commit_status == "recorded"
    assert caught.value.persistence_status == "recorded"
    assert caught.value.result is True
    assert caught.value.possible_result is None
    assert progress.task_commit_status == "recorded"
    assert progress.persistence_status == "recorded"
    assert progress.result is True
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["last_error"] == "search failed"


def test_record_error_ordinary_failure_before_replace_is_not_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    progress = TaskMutationProgress()
    failure = OSError("injected pre-publish failure")

    def fail_before_replace(_source: Any, _destination: Any) -> None:
        raise failure

    monkeypatch.setattr(os, "replace", fail_before_replace)

    with pytest.raises(OSError) as caught:
        manager.record_error(
            task["id"],
            "search failed",
            progress=progress,
        )

    assert caught.value is failure
    assert progress.task_commit_status == "not-recorded"
    assert progress.persistence_status == "not-recorded"
    assert progress.result is None
    assert progress.possible_result is None
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["last_error"] is None


def test_record_error_unknown_commit_retains_possible_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    progress = TaskMutationProgress()
    failure = OSError("injected post-publish failure")
    real_replace = os.replace
    real_lstat = Path.lstat

    def replace_then_fail(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise failure

    def deny_commit_reconciliation(path: Path) -> os.stat_result:
        if path == tasks_file:
            raise PermissionError("injected reconciliation denial")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_fail)
        patch.setattr(Path, "lstat", deny_commit_reconciliation)
        with pytest.raises(TaskMutationPersistenceError) as caught:
            manager.record_error(
                task["id"],
                "search failed",
                progress=progress,
            )

    assert caught.value.cause_error is failure
    assert caught.value.task_commit_status == "not-established"
    assert caught.value.persistence_status == "not-established"
    assert caught.value.result is None
    assert caught.value.possible_result is True
    assert progress.task_commit_status == "not-established"
    assert progress.persistence_status == "not-established"
    assert progress.result is None
    assert progress.possible_result is True
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["last_error"] == "search failed"


def test_system_exit_is_not_reclassified_as_monitor_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    termination = SystemExit(7)
    real_replace = os.replace

    def replace_then_exit(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise termination

    monkeypatch.setattr(os, "replace", replace_then_exit)

    with pytest.raises(SystemExit) as caught:
        manager.record_run(task["id"], [{"id": "new-1"}])

    assert caught.value is termination
    assert caught.value.code == 7
    assert caught.value.committed is True
    assert caught.value.result == [{"id": "new-1"}]


def test_record_run_progress_survives_interrupt_before_caller_assignment(
    tmp_path: Path,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    progress = RecordRunProgress()

    with pytest.raises(KeyboardInterrupt):
        manager.record_run(
            task["id"],
            [{"id": "new-1", "title": "新商品"}],
            progress=progress,
        )
        # This models SIGINT after record_run returns but before the caller can
        # assign its return value. The caller-owned progress already has it.
        raise KeyboardInterrupt

    assert progress.committed is True
    assert progress.result == [{"id": "new-1", "title": "新商品"}]


def test_exception_recovers_result_if_commit_observer_itself_is_interrupted(
    tmp_path: Path,
) -> None:
    class InterruptingProgress(RecordRunProgress):
        def _mark_committed(self, result: Any) -> None:
            super()._mark_committed(result)
            raise KeyboardInterrupt

    manager = TaskManager(str(tmp_path / "tasks.json"))
    task = manager.create_task("相机")
    progress = InterruptingProgress()

    with pytest.raises(TaskMutationInterrupted) as caught:
        manager.record_run(
            task["id"],
            [{"id": "new-1", "title": "新商品"}],
            progress=progress,
        )

    assert caught.value.task_commit_status == "recorded"
    assert caught.value.result == [{"id": "new-1", "title": "新商品"}]
    assert progress.task_commit_status == "recorded"
    assert progress.result == caught.value.result


def test_post_commit_lock_error_carries_committed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    progress = RecordRunProgress()
    original_unlink = Path.unlink

    def unlink_then_fail(path: Path, *args: Any, **kwargs: Any) -> None:
        original_unlink(path, *args, **kwargs)
        if path == manager.lock_file:
            raise OSError("injected lock unlink failure")

    monkeypatch.setattr(Path, "unlink", unlink_then_fail)

    with pytest.raises(TaskMutationCommittedError) as caught:
        manager.record_run(
            task["id"],
            [{"id": "new-1", "title": "新商品"}],
            progress=progress,
        )

    failure = caught.value
    assert isinstance(failure.cause_error, OSError)
    assert failure.committed is True
    assert failure.task_commit_status == "recorded"
    assert failure.persistence_status == "recorded"
    assert failure.result == [{"id": "new-1", "title": "新商品"}]
    assert failure.possible_result is None
    assert getattr(failure, "cleanup_failures", []) == []
    assert progress.committed is True
    assert progress.result == failure.result
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["new-1"]
    assert not manager.lock_file.exists()


@pytest.mark.parametrize(
    "interruption_factory",
    [KeyboardInterrupt, asyncio.CancelledError],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_unknown_commit_reconciliation_retains_possible_result_for_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_factory: type[BaseException],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    item = {"id": "possible-1", "title": "原始标题"}
    progress = RecordRunProgress()
    interruption = interruption_factory()
    real_replace = os.replace
    real_lstat = Path.lstat

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise interruption

    def deny_commit_reconciliation(path: Path) -> os.stat_result:
        if path == tasks_file:
            raise PermissionError("injected reconciliation denial")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_interrupt)
        patch.setattr(Path, "lstat", deny_commit_reconciliation)
        with pytest.raises(TaskMutationInterrupted) as caught:
            manager.record_run(task["id"], [item], progress=progress)

    failure = caught.value
    assert failure.cause_error is interruption
    assert failure.committed is False
    assert failure.task_commit_status == "not-established"
    assert failure.persistence_status == "not-established"
    assert failure.result is None
    assert failure.possible_result == [item]
    assert failure.possible_result[0] is not item
    assert progress.committed is False
    assert progress.task_commit_status == "not-established"
    assert progress.persistence_status == "not-established"
    assert progress.result == []
    assert progress.possible_result == [item]
    assert progress.possible_result is not failure.possible_result

    item["title"] = "调用方随后修改"
    assert failure.possible_result == [{"id": "possible-1", "title": "原始标题"}]
    assert progress.possible_result == [{"id": "possible-1", "title": "原始标题"}]
    # The injected replace did commit.  The API nevertheless reports unknown
    # because the process could not prove that fact at the interruption edge.
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["possible-1"]
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_unknown_commit_reconciliation_retains_possible_result_for_ordinary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    item = {"id": "possible-1", "title": "原始标题"}
    progress = RecordRunProgress()
    replace_error = OSError("injected post-replace error")
    real_replace = os.replace
    real_lstat = Path.lstat

    def replace_then_fail(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise replace_error

    def fail_commit_reconciliation(path: Path) -> os.stat_result:
        if path == tasks_file:
            raise OSError(5, "injected reconciliation I/O failure")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_fail)
        patch.setattr(Path, "lstat", fail_commit_reconciliation)
        with pytest.raises(TaskMutationPersistenceError) as caught:
            manager.record_run(task["id"], [item], progress=progress)

    failure = caught.value
    assert not isinstance(failure, TaskMutationCommittedError)
    assert failure.cause_error is replace_error
    assert failure.committed is False
    assert failure.task_commit_status == "not-established"
    assert failure.persistence_status == "not-established"
    assert failure.result is None
    assert failure.possible_result == [item]
    assert progress.committed is False
    assert progress.task_commit_status == "not-established"
    assert progress.persistence_status == "not-established"
    assert progress.result == []
    assert progress.possible_result == [item]
    assert failure.cleanup_failures == [
        "failed to reconcile the atomic task-file commit"
    ]

    item["title"] = "调用方随后修改"
    assert failure.possible_result == [{"id": "possible-1", "title": "原始标题"}]
    assert progress.possible_result == [{"id": "possible-1", "title": "原始标题"}]
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == ["possible-1"]
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_task_staging_stream_is_not_closed_twice_after_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    task = manager.create_task("相机")
    original_close = task_manager._close_private_stage_stream
    task_stage_close_calls = 0

    def close_then_fail(stage: task_manager._PrivateFileStage) -> None:
        nonlocal task_stage_close_calls
        if stage.directory.name.startswith(f".{tasks_file.name}."):
            task_stage_close_calls += 1
            original_close(stage)
            raise OSError("injected error after task staging close")
        original_close(stage)

    monkeypatch.setattr(
        task_manager,
        "_close_private_stage_stream",
        close_then_fail,
    )

    with pytest.raises(OSError, match="injected error after task staging close"):
        manager.record_run(task["id"], [{"id": "not-recorded"}])

    assert task_stage_close_calls == 1
    stored = manager.get_task(task["id"])
    assert stored is not None
    assert stored["seen_item_ids"] == []
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []
    assert not manager.lock_file.exists()


def test_task_staging_directory_creation_interruption_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.tasks = [{"id": "not-recorded", "keyword": "相机"}]
    original_mkdir = Path.mkdir

    def mkdir_then_interrupt(path: Path, *args: Any, **kwargs: Any) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.name.startswith(f".{tasks_file.name}."):
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "mkdir", mkdir_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        manager._save()  # noqa: SLF001 - exercise the allocation boundary directly.

    assert not tasks_file.exists()
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []


def test_task_staging_open_interruption_leaves_no_file_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.tasks = [{"id": "not-recorded", "keyword": "相机"}]
    original_open = Path.open

    def open_then_interrupt(path: Path, *args: Any, **kwargs: Any) -> Any:
        stream = original_open(path, *args, **kwargs)
        if path.name == "payload" and path.parent.name.startswith(
            f".{tasks_file.name}."
        ):
            if os.name == "nt":
                stream.close()
            raise KeyboardInterrupt
        return stream

    monkeypatch.setattr(Path, "open", open_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        manager._save()  # noqa: SLF001 - exercise the allocation boundary directly.

    assert not tasks_file.exists()
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []


@pytest.mark.parametrize(
    "interruption_factory",
    [KeyboardInterrupt, asyncio.CancelledError],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_task_staging_verification_failure_preserves_primary_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_factory: type[BaseException],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    manager = TaskManager(str(tasks_file))
    manager.tasks = [{"id": "not-recorded", "keyword": "相机"}]
    stage: task_manager._PrivateFileStage | None = None
    cleanup_phase = False
    interruption = interruption_factory()
    original_new_stage = task_manager._new_private_file_stage
    original_lstat = Path.lstat

    def capture_stage(parent: Path, prefix: str) -> task_manager._PrivateFileStage:
        nonlocal stage
        stage = original_new_stage(parent, prefix)
        return stage

    def interrupt_dump(*_args: object, **_kwargs: object) -> None:
        nonlocal cleanup_phase
        cleanup_phase = True
        raise interruption

    def reject_stage_verification(path: Path) -> os.stat_result:
        if cleanup_phase and stage is not None and path == stage.directory:
            raise PermissionError("injected staging-directory lstat denial")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(task_manager, "_new_private_file_stage", capture_stage)
        patch.setattr(task_manager.json, "dump", interrupt_dump)
        patch.setattr(Path, "lstat", reject_stage_verification)

        with pytest.raises(interruption_factory) as caught:
            manager._save()  # noqa: SLF001 - exercise cleanup evidence directly.

    assert caught.value is interruption
    assert caught.value.task_commit_status == "not-recorded"
    assert caught.value.persistence_status == "not-recorded"
    assert getattr(caught.value, "cleanup_failures", []) == [
        "failed to verify the private task staging directory"
    ]
    assert stage is not None
    assert stage.path.is_file()
    assert stage.directory.is_dir()
    assert not tasks_file.exists()

    stage.path.unlink()
    stage.directory.rmdir()


@pytest.mark.parametrize(
    "interruption_factory",
    [KeyboardInterrupt, asyncio.CancelledError],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_cli_reports_cancellation_after_committed_mutation_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption_factory: type[BaseException],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    real_create_task = TaskManager.create_task
    interruption = interruption_factory()

    def create_then_interrupt(
        manager: TaskManager,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        real_create_task(manager, *args, **kwargs)
        raise interruption

    monkeypatch.setattr(TaskManager, "create_task", create_then_interrupt)

    exit_code = task_manager.main(["--data-file", str(tasks_file), "create", "相机"])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 130
    assert report["ok"] is False
    assert report["error"] == "task command cancelled"
    assert report["error_type"] == type(interruption).__name__
    assert report["task_commit_status"] == "recorded"
    assert report["persistence"] == {"status": "recorded"}
    assert report["result"]["keyword"] == "相机"
    stored = TaskManager(str(tasks_file)).list_tasks()
    assert len(stored) == 1
    assert stored[0]["keyword"] == "相机"


def test_cli_reports_possible_result_when_ordinary_commit_status_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    real_replace = os.replace
    real_lstat = Path.lstat

    def replace_then_fail(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        raise OSError("injected post-replace error")

    def fail_commit_reconciliation(path: Path) -> os.stat_result:
        if path == tasks_file:
            raise OSError(5, "injected reconciliation I/O failure")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_fail)
        patch.setattr(Path, "lstat", fail_commit_reconciliation)
        exit_code = task_manager.main(
            ["--data-file", str(tasks_file), "create", "相机"]
        )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["error_type"] == "OSError"
    assert report["task_commit_status"] == "not-established"
    assert report["persistence"] == {"status": "not-established"}
    assert report["possible_result"]["keyword"] == "相机"
    stored = TaskManager(str(tasks_file)).list_tasks()
    assert len(stored) == 1
    assert stored[0]["keyword"] == "相机"


def test_lock_staging_creation_interruption_removes_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_prepare = task_manager._prepare_private_file_stage

    def prepare_then_interrupt(
        stage: task_manager._PrivateFileStage,
        *,
        encoding: str,
    ) -> None:
        original_prepare(stage, encoding=encoding)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        task_manager,
        "_prepare_private_file_stage",
        prepare_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        with _exclusive_lock(lock_file):
            pytest.fail("the lock body must not run")

    assert not lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt(), asyncio.CancelledError()],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_lock_link_commit_interruption_removes_owned_lock_and_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_link = os.link

    def link_then_interrupt(source: Any, target: Any) -> None:
        original_link(source, target)
        raise interruption

    monkeypatch.setattr(os, "link", link_then_interrupt)

    with pytest.raises(type(interruption)) as caught:
        with _exclusive_lock(lock_file):
            pytest.fail("the lock body must not run")

    assert caught.value is interruption
    assert not lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []

    monkeypatch.setattr(os, "link", original_link)
    with _exclusive_lock(lock_file):
        assert lock_file.exists()
    assert not lock_file.exists()


def test_lock_publish_verification_preserves_concurrent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_link = os.link

    def link_then_replace(source: Any, target: Any) -> None:
        original_link(source, target)
        Path(target).unlink()
        Path(target).write_text("replacement owner", encoding="ascii")

    monkeypatch.setattr(os, "link", link_then_replace)

    with pytest.raises(OSError, match="identity could not be verified"):
        with _exclusive_lock(lock_file):
            pytest.fail("a replaced lock must not enter the critical section")

    assert lock_file.read_text(encoding="ascii") == "replacement owner"
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []
    lock_file.unlink()


def test_lock_hardlink_unsupported_fails_closed_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"

    def reject_link(_source: Any, _target: Any) -> None:
        raise OSError("hard links unavailable")

    monkeypatch.setattr(os, "link", reject_link)

    with pytest.raises(OSError, match="hard links unavailable"):
        with _exclusive_lock(lock_file):
            pytest.fail("unsupported atomic locking must fail closed")

    assert not lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []


def test_lock_cleanup_preserves_primary_interrupt_and_still_unlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_unlink = Path.unlink

    def unlink_then_fail(path: Path, *args: Any, **kwargs: Any) -> None:
        original_unlink(path, *args, **kwargs)
        if path == lock_file:
            raise OSError("injected unlink failure")

    monkeypatch.setattr(Path, "unlink", unlink_then_fail)
    primary = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as caught:
        with _exclusive_lock(lock_file):
            raise primary

    assert caught.value is primary
    assert getattr(caught.value, "cleanup_failures", []) == []
    assert not lock_file.exists()


def test_lock_unlink_interruption_does_not_skip_anchor_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_unlink = Path.unlink

    def unlink_then_interrupt(path: Path, *args: Any, **kwargs: Any) -> None:
        original_unlink(path, *args, **kwargs)
        if path == lock_file:
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "unlink", unlink_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        with _exclusive_lock(lock_file):
            pass

    assert getattr(caught.value, "cleanup_failures", []) == []
    assert not lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []


def test_lock_cleanup_failure_before_unlink_is_reported_without_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    original_unlink = Path.unlink

    def reject_owned_lock_unlink(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if path == lock_file:
            raise PermissionError("injected unlink denial")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_owned_lock_unlink)

    with pytest.raises(PermissionError) as caught:
        with _exclusive_lock(lock_file):
            pass

    assert getattr(caught.value, "cleanup_failures", []) == [
        "could not confirm removal of the owned task lock"
    ]
    assert lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []

    monkeypatch.setattr(Path, "unlink", original_unlink)
    lock_file.unlink()


def test_cli_reports_committed_mutation_and_lock_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks_file = tmp_path / "tasks.json"
    lock_file = tasks_file.with_suffix(f"{tasks_file.suffix}.lock")
    original_unlink = Path.unlink

    def reject_owned_lock_unlink(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if path == lock_file:
            raise PermissionError("injected unlink denial")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", reject_owned_lock_unlink)
        exit_code = task_manager.main(
            ["--data-file", str(tasks_file), "create", "相机"]
        )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["error_type"] == "PermissionError"
    assert report["task_commit_status"] == "recorded"
    assert report["persistence"] == {"status": "recorded"}
    assert report["result"]["keyword"] == "相机"
    assert report["cleanup"] == {
        "status": "failed",
        "errors": ["could not confirm removal of the owned task lock"],
    }
    assert lock_file.exists()
    assert list(tmp_path.glob(".tasks.json.lock.*.tmp")) == []
    stored = TaskManager(str(tasks_file)).list_tasks()
    assert len(stored) == 1
    assert stored[0]["keyword"] == "相机"

    lock_file.unlink()


def test_lock_cleanup_does_not_unlink_a_replacement_lock(tmp_path: Path) -> None:
    lock_file = tmp_path / "tasks.json.lock"

    with _exclusive_lock(lock_file):
        lock_file.unlink()
        lock_file.write_text("replacement owner", encoding="utf-8")

    assert lock_file.read_text(encoding="utf-8") == "replacement owner"
    lock_file.unlink()


def test_old_lock_with_live_owner_is_not_removed(tmp_path: Path) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    lock_file.write_text(str(os.getpid()), encoding="ascii")
    old_timestamp = task_manager.time.time() - 120
    os.utime(lock_file, (old_timestamp, old_timestamp))

    with pytest.raises(TimeoutError, match="timed out waiting for task lock"):
        with _exclusive_lock(lock_file, timeout=0):
            pytest.fail("a live owner's old lock must remain exclusive")

    assert lock_file.read_text(encoding="ascii") == str(os.getpid())


def test_old_lock_with_unverifiable_owner_is_not_removed(tmp_path: Path) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    lock_file.write_text("not-a-pid", encoding="ascii")
    old_timestamp = task_manager.time.time() - 120
    os.utime(lock_file, (old_timestamp, old_timestamp))

    with pytest.raises(TimeoutError, match="timed out waiting for task lock"):
        with _exclusive_lock(lock_file, timeout=0):
            pytest.fail("an unverifiable lock owner must fail closed")

    assert lock_file.read_text(encoding="ascii") == "not-a-pid"


def test_old_lock_with_dead_owner_is_not_removed_automatically(tmp_path: Path) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    lock_file.write_text("424242", encoding="ascii")
    old_timestamp = task_manager.time.time() - 120
    os.utime(lock_file, (old_timestamp, old_timestamp))

    with pytest.raises(TimeoutError, match="timed out waiting for task lock"):
        with _exclusive_lock(lock_file, timeout=0):
            pytest.fail("even an apparently dead owner requires operator inspection")

    assert lock_file.read_text(encoding="ascii") == "424242"


@pytest.mark.parametrize("existing_lock", [False, True])
def test_already_cancelling_task_never_acquires_or_waits_for_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_lock: bool,
) -> None:
    lock_file = tmp_path / "tasks.json.lock"
    if existing_lock:
        lock_file.write_text("other owner", encoding="ascii")

    class CancellingTask:
        @staticmethod
        def cancelling() -> int:
            return 1

    monkeypatch.setattr(
        task_manager.asyncio,
        "current_task",
        lambda: CancellingTask(),
    )

    with pytest.raises(asyncio.CancelledError):
        with _exclusive_lock(lock_file, timeout=0):
            pytest.fail("an already-cancelled task must not enter the critical section")

    if existing_lock:
        assert lock_file.read_text(encoding="ascii") == "other owner"
    else:
        assert not lock_file.exists()


def test_python_310_pending_cancellation_is_detected_without_cancelling_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Python310Task:
        _must_cancel = True

    monkeypatch.setattr(
        task_manager.asyncio,
        "current_task",
        lambda: Python310Task(),
    )

    with pytest.raises(asyncio.CancelledError):
        _raise_if_async_task_cancelling()


def test_real_task_self_cancellation_is_detected_at_sync_boundary() -> None:
    events: list[str] = []

    async def cancel_at_boundary() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            _raise_if_async_task_cancelling()
        except asyncio.CancelledError:
            events.append("caught-at-boundary")
            raise
        events.append("missed")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_at_boundary())
    assert events == ["caught-at-boundary"]
