from __future__ import annotations

import json
import os
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

    if os.name != "nt":
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
