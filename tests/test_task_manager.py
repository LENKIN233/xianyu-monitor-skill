from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from task_manager import TaskManager


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

    assert stat.S_IMODE(task_file.stat().st_mode) == 0o600
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2


def test_price_validation_and_status_updates(tmp_path: Path) -> None:
    manager = TaskManager(str(tmp_path / "tasks.json"))
    with pytest.raises(ValueError, match="minimum price"):
        manager.create_task("坏条件", min_price=2_000, max_price=1_000)

    task = manager.create_task("显示器")
    assert manager.set_status(task["id"], "stopped") is True
    assert manager.list_tasks(running_only=True) == []
    assert manager.set_status(task["id"], "running") is True


def test_invalid_task_file_is_not_silently_erased(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid task file"):
        TaskManager(str(task_file))
