#!/usr/bin/env python3
"""Manage persistent Xianyu monitor tasks safely."""

from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MAX_SEEN_ITEMS = 50_000
MAX_LAST_RESULTS = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _exclusive_lock(lock_file: Path, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a small cross-platform lock file with stale-lock recovery."""

    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_file.stat().st_mtime > 60
            except FileNotFoundError:
                continue
            if stale:
                lock_file.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for task lock: {lock_file}")
            time.sleep(0.05)

    try:
        yield
    finally:
        os.close(descriptor)
        lock_file.unlink(missing_ok=True)


class TaskManager:
    """Persist task definitions and per-task seen-item state."""

    def __init__(self, data_file: str = "tasks.json"):
        self.data_file = Path(data_file).expanduser().resolve()
        self.lock_file = self.data_file.with_suffix(f"{self.data_file.suffix}.lock")
        self.tasks: list[dict[str, Any]] = []
        self._load()

    def _normalize_task(self, task: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(task)
        normalized.setdefault("id", f"task_{uuid.uuid4().hex[:12]}")
        normalized.setdefault("keyword", "")
        normalized.setdefault("min_price", None)
        normalized.setdefault("max_price", None)
        normalized.setdefault("location", None)
        normalized.setdefault("criteria", "")
        normalized.setdefault("pages", 1)
        normalized.setdefault("retries", 3)
        normalized.setdefault("state_file", None)
        normalized.setdefault("status", "running")
        normalized.setdefault("created_at", _now())
        normalized.setdefault("updated_at", normalized["created_at"])
        normalized.setdefault("last_run", None)
        normalized.setdefault("last_error", None)
        normalized.setdefault("results_count", 0)
        normalized.setdefault("last_results", [])
        normalized.setdefault("seen_item_ids", [])
        # Delivery belongs to the scheduler/agent session, not this data model.
        normalized.pop("notification", None)
        return normalized

    def _load(self) -> None:
        if not self.data_file.exists():
            self.tasks = []
            return
        try:
            payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid task file {self.data_file}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise ValueError(  # noqa: TRY004 - invalid persisted data is a value error.
                f"invalid task file schema: {self.data_file}"
            )
        self.tasks = [
            self._normalize_task(task)
            for task in payload["tasks"]
            if isinstance(task, dict)
        ]

    def _save(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "tasks": self.tasks,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.data_file.name}.",
            suffix=".tmp",
            dir=self.data_file.parent,
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            except (AttributeError, OSError):
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.data_file)
            try:
                self.data_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_file):
            self._load()
            yield
            self._save()

    @staticmethod
    def _validate_prices(min_price: float | None, max_price: float | None) -> None:
        if min_price is not None and min_price < 0:
            raise ValueError("minimum price must not be negative")
        if max_price is not None and max_price < 0:
            raise ValueError("maximum price must not be negative")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("minimum price must not exceed maximum price")

    def _find_existing_task(
        self,
        keyword: str,
        max_price: float | None,
        min_price: float | None,
        location: str | None,
    ) -> dict[str, Any] | None:
        for task in self.tasks:
            if (
                task.get("keyword") == keyword
                and task.get("max_price") == max_price
                and task.get("min_price") == min_price
                and task.get("location") == location
                and task.get("status") == "running"
            ):
                return task
        return None

    def create_task(
        self,
        keyword: str,
        max_price: float | None = None,
        min_price: float | None = None,
        criteria: str = "",
        location: str | None = None,
        skip_duplicate: bool = True,
        *,
        pages: int = 1,
        retries: int = 3,
        state_file: str | None = None,
    ) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword must not be empty")
        self._validate_prices(min_price, max_price)
        if pages < 1:
            raise ValueError("pages must be at least 1")
        if retries < 1:
            raise ValueError("retries must be at least 1")

        with self._mutation():
            if skip_duplicate:
                existing = self._find_existing_task(
                    keyword, max_price, min_price, location
                )
                if existing:
                    result = copy.deepcopy(existing)
                    result["existing"] = True
                    return result

            timestamp = _now()
            task = self._normalize_task(
                {
                    "id": f"task_{uuid.uuid4().hex[:12]}",
                    "keyword": keyword,
                    "max_price": max_price,
                    "min_price": min_price,
                    "criteria": criteria,
                    "location": location,
                    "pages": pages,
                    "retries": retries,
                    "state_file": state_file,
                    "status": "running",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            self.tasks.append(task)
            return copy.deepcopy(task)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        self._load()
        for task in self.tasks:
            if task.get("id") == task_id:
                return copy.deepcopy(task)
        return None

    def list_tasks(self, *, running_only: bool = False) -> list[dict[str, Any]]:
        self._load()
        tasks = self.tasks
        if running_only:
            tasks = [task for task in tasks if task.get("status") == "running"]
        return copy.deepcopy(tasks)

    def set_status(self, task_id: str, status: str) -> bool:
        if status not in {"running", "stopped"}:
            raise ValueError("status must be running or stopped")
        found = False
        with self._mutation():
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["status"] = status
                    task["updated_at"] = _now()
                    found = True
                    break
        return found

    def delete_task(self, task_id: str) -> bool:
        found = False
        with self._mutation():
            for index, task in enumerate(self.tasks):
                if task.get("id") == task_id:
                    self.tasks.pop(index)
                    found = True
                    break
        return found

    def reset_seen(self, task_id: str) -> bool:
        found = False
        with self._mutation():
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["seen_item_ids"] = []
                    task["updated_at"] = _now()
                    found = True
                    break
        return found

    def record_run(
        self, task_id: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        new_items: list[dict[str, Any]] = []
        with self._mutation():
            task = next(
                (entry for entry in self.tasks if entry.get("id") == task_id), None
            )
            if task is None:
                raise KeyError(f"task not found: {task_id}")

            seen_ids = {
                str(item_id)
                for item_id in task.get("seen_item_ids", [])
                if item_id is not None
            }
            ordered_ids = list(task.get("seen_item_ids", []))
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                ordered_ids.append(item_id)
                new_items.append(copy.deepcopy(item))

            task["seen_item_ids"] = ordered_ids[-MAX_SEEN_ITEMS:]
            task["last_results"] = copy.deepcopy(items[:MAX_LAST_RESULTS])
            task["last_run"] = _now()
            task["last_error"] = None
            task["results_count"] = int(task.get("results_count", 0)) + len(items)
            task["updated_at"] = _now()
        return new_items

    def record_error(self, task_id: str, error: str) -> bool:
        found = False
        with self._mutation():
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["last_run"] = _now()
                    task["last_error"] = str(error)[:1_000]
                    task["updated_at"] = _now()
                    found = True
                    break
        return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Xianyu monitor tasks")
    parser.add_argument("--data-file", default="tasks.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("keyword")
    create.add_argument("--max-price", type=float)
    create.add_argument("--min-price", type=float)
    create.add_argument("--location")
    create.add_argument("--criteria", default="")
    create.add_argument("--pages", type=int, default=1)
    create.add_argument("--retries", type=int, default=3)
    create.add_argument("--state")
    create.add_argument("--allow-duplicate", action="store_true")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--running", action="store_true")

    for command in ("stop", "resume", "delete", "reset-seen"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manager = TaskManager(args.data_file)
        if args.command == "create":
            result: Any = manager.create_task(
                args.keyword,
                max_price=args.max_price,
                min_price=args.min_price,
                location=args.location,
                criteria=args.criteria,
                pages=args.pages,
                retries=args.retries,
                state_file=args.state,
                skip_duplicate=not args.allow_duplicate,
            )
        elif args.command == "list":
            result = manager.list_tasks(running_only=args.running)
        elif args.command == "stop":
            result = {"updated": manager.set_status(args.task_id, "stopped")}
        elif args.command == "resume":
            result = {"updated": manager.set_status(args.task_id, "running")}
        elif args.command == "delete":
            result = {"deleted": manager.delete_task(args.task_id)}
        else:
            result = {"updated": manager.reset_seen(args.task_id)}
    except (KeyError, OSError, TimeoutError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
