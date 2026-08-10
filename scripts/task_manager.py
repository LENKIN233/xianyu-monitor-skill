#!/usr/bin/env python3
"""Manage persistent Xianyu monitor tasks safely."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

SCHEMA_VERSION = 2
MAX_SEEN_ITEMS = 50_000
MAX_LAST_RESULTS = 100
TASK_COMMIT_RECORDED = "recorded"
TASK_COMMIT_NOT_RECORDED = "not-recorded"
TASK_COMMIT_NOT_ESTABLISHED = "not-established"
TASK_COMMIT_STATUSES = {
    TASK_COMMIT_RECORDED,
    TASK_COMMIT_NOT_RECORDED,
    TASK_COMMIT_NOT_ESTABLISHED,
}


def _reject_nonfinite_json_constant(value: str) -> Any:
    """Reject Python's non-standard NaN/Infinity JSON extensions."""

    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _copy_mutation_evidence(
    target: BaseException,
    cause: BaseException,
    *,
    task_commit_status: str,
    result: Any = None,
    possible_result: Any = None,
) -> None:
    if task_commit_status not in TASK_COMMIT_STATUSES:
        raise ValueError(f"invalid task commit status: {task_commit_status}")
    committed = task_commit_status == TASK_COMMIT_RECORDED
    setattr(target, "cause_error", cause)
    setattr(target, "task_commit_status", task_commit_status)
    setattr(target, "persistence_status", task_commit_status)
    setattr(target, "committed", committed)
    setattr(target, "result", copy.deepcopy(result) if committed else None)
    setattr(
        target,
        "possible_result",
        copy.deepcopy(possible_result)
        if task_commit_status == TASK_COMMIT_NOT_ESTABLISHED
        else None,
    )
    failures = getattr(cause, "cleanup_failures", None)
    if isinstance(failures, list):
        setattr(target, "cleanup_failures", list(failures))


def _set_task_commit_status(error: BaseException, status: str) -> None:
    """Attach persistence evidence without changing an exception's identity."""

    if status not in TASK_COMMIT_STATUSES:
        raise ValueError(f"invalid task commit status: {status}")
    setattr(error, "task_commit_status", status)
    setattr(error, "persistence_status", status)


class TaskMutationInterrupted(BaseException):
    """Report whether an interrupted task mutation reached the atomic commit."""

    def __init__(
        self,
        cause: BaseException,
        *,
        committed: bool | None = None,
        result: Any = None,
        task_commit_status: str | None = None,
        possible_result: Any = None,
    ):
        if task_commit_status is None:
            task_commit_status = (
                TASK_COMMIT_RECORDED if committed else TASK_COMMIT_NOT_RECORDED
            )
        super().__init__(
            f"task mutation interrupted with persistence {task_commit_status}"
        )
        _copy_mutation_evidence(
            self,
            cause,
            task_commit_status=task_commit_status,
            result=result,
            possible_result=possible_result,
        )


class TaskMutationPersistenceError(RuntimeError):
    """Carry retained result evidence when mutation finalization fails."""

    def __init__(
        self,
        cause: BaseException,
        *,
        task_commit_status: str,
        result: Any = None,
        possible_result: Any = None,
    ):
        super().__init__(
            "task mutation persistence "
            f"{task_commit_status}; finalization failed: {type(cause).__name__}"
        )
        _copy_mutation_evidence(
            self,
            cause,
            task_commit_status=task_commit_status,
            result=result,
            possible_result=possible_result,
        )


class TaskMutationCommittedError(TaskMutationPersistenceError):
    """Carry a committed mutation result when finalization fails."""

    def __init__(self, cause: BaseException, *, result: Any):
        super().__init__(
            cause,
            task_commit_status=TASK_COMMIT_RECORDED,
            result=result,
        )


@dataclass
class TaskMutationProgress:
    """Caller-owned persistence evidence for one task mutation."""

    committed: bool = False
    result: Any = None
    task_commit_status: str = TASK_COMMIT_NOT_RECORDED
    persistence_status: str = TASK_COMMIT_NOT_RECORDED
    possible_result: Any = None

    def reset(self) -> None:
        self.committed = False
        self.result = None
        self.task_commit_status = TASK_COMMIT_NOT_RECORDED
        self.persistence_status = TASK_COMMIT_NOT_RECORDED
        self.possible_result = None

    def _set_status(self, status: str) -> None:
        if status not in TASK_COMMIT_STATUSES:
            raise ValueError(f"invalid task commit status: {status}")
        self.task_commit_status = status
        self.persistence_status = status
        self.committed = status == TASK_COMMIT_RECORDED

    def _mark_committed(self, result: Any) -> None:
        self.result = copy.deepcopy(result)
        self.possible_result = None
        self._set_status(TASK_COMMIT_RECORDED)

    def _mark_not_established(self, result: Any) -> None:
        self.result = None
        self.possible_result = copy.deepcopy(result)
        self._set_status(TASK_COMMIT_NOT_ESTABLISHED)


@dataclass
class RecordRunProgress(TaskMutationProgress):
    """Caller-owned evidence that survives interruption before result assignment."""

    result: list[dict[str, Any]] = field(default_factory=list)
    possible_result: list[dict[str, Any]] | None = None

    def reset(self) -> None:
        super().reset()
        self.result = []

    def _mark_committed(self, result: Any) -> None:
        snapshot = copy.deepcopy(result)
        if not isinstance(snapshot, list):
            raise TypeError("record-run commit result must be a list")
        self.result = snapshot
        self.possible_result = None
        self._set_status(TASK_COMMIT_RECORDED)

    def _mark_not_established(self, result: Any) -> None:
        snapshot = copy.deepcopy(result)
        if not isinstance(snapshot, list):
            raise TypeError("record-run possible result must be a list")
        self.result = []
        self.possible_result = snapshot
        self._set_status(TASK_COMMIT_NOT_ESTABLISHED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_interruption(error: BaseException) -> bool:
    return isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))


def _is_control_flow(error: BaseException) -> bool:
    return not isinstance(error, Exception)


def _raise_if_async_task_cancelling() -> None:
    """Raise at sync boundaries when the current task has a pending cancel."""

    try:
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is None:
        return
    cancelling = getattr(task, "cancelling", None)
    cancellation_requested = (
        bool(cancelling())
        if callable(cancelling)
        else bool(getattr(task, "_must_cancel", False))
    )
    if cancellation_requested:
        raise asyncio.CancelledError


def _append_cleanup_failure(error: BaseException, message: str) -> None:
    failures = getattr(error, "cleanup_failures", None)
    if not isinstance(failures, list):
        failures = []
        setattr(error, "cleanup_failures", failures)
    if message not in failures:
        failures.append(message)


def _same_file(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return False
    return os.path.samestat(expected, actual)


def _unlink_if_same(path: Path, expected: os.stat_result) -> None:
    """Unlink only the path still naming the file this process created."""

    if _same_file(path, expected):
        path.unlink()


def _prefer_cleanup_interruption(
    primary: BaseException,
    cleanup: BaseException,
    message: str,
) -> BaseException:
    """Record cleanup failure while preserving an existing primary interruption."""

    _append_cleanup_failure(primary, message)
    if _is_control_flow(primary) or not _is_control_flow(cleanup):
        return primary
    for failure in getattr(primary, "cleanup_failures", []):
        _append_cleanup_failure(cleanup, failure)
    for attribute in ("task_commit_status", "persistence_status"):
        if hasattr(primary, attribute):
            setattr(cleanup, attribute, getattr(primary, attribute))
    setattr(cleanup, "cause_error", primary)
    return cleanup


@dataclass
class _PrivateFileStage:
    """Caller-owned identity for one same-filesystem private staging file."""

    directory: Path
    path: Path
    directory_stat: os.stat_result | None = None
    file_stat: os.stat_result | None = None
    stream: Any = None
    publish_attempted: bool = False


def _new_private_file_stage(parent: Path, prefix: str) -> _PrivateFileStage:
    directory = parent / f"{prefix}{uuid.uuid4().hex}.tmp"
    return _PrivateFileStage(
        directory=directory,
        path=directory / "payload",
    )


def _same_private_directory(stage: _PrivateFileStage) -> bool:
    if stage.directory_stat is None:
        return False
    try:
        actual = stage.directory.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(actual.st_mode) and os.path.samestat(
        stage.directory_stat,
        actual,
    )


def _prepare_private_file_stage(
    stage: _PrivateFileStage,
    *,
    encoding: str,
) -> None:
    stage.directory.mkdir(mode=stat.S_IRWXU)
    directory_stat = stage.directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("private task staging directory has an unexpected type")
    stage.directory_stat = directory_stat
    stage.stream = stage.path.open("x", encoding=encoding, newline="\n")
    stage.file_stat = os.fstat(stage.stream.fileno())
    try:
        os.fchmod(stage.stream.fileno(), stat.S_IRUSR | stat.S_IWUSR)
    except (AttributeError, OSError):
        pass


def _close_private_stage_stream(stage: _PrivateFileStage) -> None:
    if stage.stream is None:
        return
    stream = stage.stream
    stage.stream = None
    try:
        stream.close()
    except BaseException as close_error:  # noqa: BLE001
        _append_cleanup_failure(
            close_error,
            "failed to close the private task staging stream",
        )
        raise


def _merge_stage_cleanup_error(
    primary: BaseException | None,
    cleanup: BaseException,
    message: str,
) -> BaseException:
    if primary is None:
        _append_cleanup_failure(cleanup, message)
        return cleanup
    return _prefer_cleanup_interruption(primary, cleanup, message)


def _prefer_exception_without_cleanup_failure(
    primary: BaseException | None,
    secondary: BaseException,
) -> BaseException:
    if primary is None:
        return secondary
    if _is_control_flow(primary) or not _is_control_flow(secondary):
        return primary
    for attribute in ("task_commit_status", "persistence_status"):
        if hasattr(primary, attribute):
            setattr(secondary, attribute, getattr(primary, attribute))
    setattr(secondary, "cause_error", primary)
    return secondary


def _cleanup_private_file_stage(
    stage: _PrivateFileStage,
    primary: BaseException | None = None,
) -> BaseException | None:
    """Clean only the private directory identity retained by this invocation."""

    error = primary
    if stage.stream is not None:
        try:
            _close_private_stage_stream(stage)
        except BaseException as cleanup_error:  # noqa: BLE001
            error = _merge_stage_cleanup_error(
                error,
                cleanup_error,
                "failed to close the private task staging stream",
            )

    if stage.directory_stat is None:
        if isinstance(primary, FileExistsError):
            return error
        try:
            stage.directory.rmdir()
        except FileNotFoundError:
            pass
        except BaseException as cleanup_error:  # noqa: BLE001
            error = _merge_stage_cleanup_error(
                error,
                cleanup_error,
                "failed to remove an unidentified task staging directory",
            )
        return error

    try:
        directory_matches = _same_private_directory(stage)
    except BaseException as verification_error:  # noqa: BLE001
        error = _merge_stage_cleanup_error(
            error,
            verification_error,
            "failed to verify the private task staging directory",
        )
        return error
    if not directory_matches:
        try:
            stage.directory.lstat()
        except FileNotFoundError:
            return error
        except BaseException as verification_error:  # noqa: BLE001
            error = _merge_stage_cleanup_error(
                error,
                verification_error,
                "failed to verify the private task staging directory",
            )
        else:
            mismatch = OSError("private task staging directory changed")
            error = _merge_stage_cleanup_error(
                error,
                mismatch,
                "failed to verify the private task staging directory",
            )
        return error

    try:
        if stage.file_stat is not None:
            _unlink_if_same(stage.path, stage.file_stat)
        else:
            try:
                candidate = stage.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(candidate.st_mode):
                    raise OSError("private task staging file has an unexpected type")
                stage.path.unlink()
    except BaseException as cleanup_error:  # noqa: BLE001
        error = _merge_stage_cleanup_error(
            error,
            cleanup_error,
            "failed to remove the private task staging file",
        )

    try:
        if _same_private_directory(stage):
            stage.directory.rmdir()
        elif stage.directory.exists() or stage.directory.is_symlink():
            raise OSError(  # noqa: TRY301 - converted to cleanup evidence below.
                "private task staging directory changed"
            )
    except BaseException as cleanup_error:  # noqa: BLE001
        error = _merge_stage_cleanup_error(
            error,
            cleanup_error,
            "failed to remove the private task staging directory",
        )
    return error


def _cleanup_owned_lock(
    lock_file: Path,
    owned_stat: os.stat_result,
    stage: _PrivateFileStage,
    primary: BaseException | None = None,
) -> BaseException | None:
    """Unlink only the lock inode published from the retained private anchor."""

    error = primary
    try:
        _unlink_if_same(lock_file, owned_stat)
    except BaseException as cleanup_error:  # noqa: BLE001 - cancellation-safe cleanup
        try:
            owned_lock_remains = _same_file(lock_file, owned_stat)
        except BaseException as verification_error:  # noqa: BLE001
            error = _merge_stage_cleanup_error(
                error,
                verification_error,
                "could not confirm removal of the owned task lock",
            )
        else:
            if owned_lock_remains:
                error = _merge_stage_cleanup_error(
                    error,
                    cleanup_error,
                    "could not confirm removal of the owned task lock",
                )
            else:
                error = _prefer_exception_without_cleanup_failure(
                    error,
                    cleanup_error,
                )
    return _cleanup_private_file_stage(stage, error)


@contextmanager
def _exclusive_lock(lock_file: Path, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a lock file without guessing that an existing owner is stale."""

    deadline = time.monotonic() + timeout
    active_stage: _PrivateFileStage | None = None
    owned_stat: os.stat_result | None = None
    primary_error: BaseException | None = None
    primary_traceback: Any = None
    try:
        while owned_stat is None:
            _raise_if_async_task_cancelling()
            active_stage = _new_private_file_stage(
                lock_file.parent,
                f".{lock_file.name}.",
            )
            _prepare_private_file_stage(active_stage, encoding="ascii")
            stream = active_stage.stream
            if stream is None or active_stage.file_stat is None:
                raise OSError(  # noqa: TRY301 - outer block owns cleanup.
                    "task lock staging identity was not established"
                )
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())
            _close_private_stage_stream(active_stage)
            try:
                active_stage.publish_attempted = True
                os.link(active_stage.path, lock_file)
            except FileExistsError:
                active_stage.publish_attempted = False
                cleanup_error = _cleanup_private_file_stage(active_stage)
                if cleanup_error is not None:
                    raise cleanup_error
                active_stage = None
                _raise_if_async_task_cancelling()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for task lock: {lock_file}")
                time.sleep(0.05)
                _raise_if_async_task_cancelling()
                continue
            if not _same_file(lock_file, active_stage.file_stat):
                raise OSError(  # noqa: TRY301 - outer block owns cleanup.
                    "published task lock identity could not be verified"
                )
            owned_stat = active_stage.file_stat
            _raise_if_async_task_cancelling()

        yield
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive cancellation
        primary_error = exc
        primary_traceback = exc.__traceback__

    cleanup_error: BaseException | None = primary_error
    if active_stage is not None:
        if active_stage.publish_attempted and active_stage.file_stat is not None:
            cleanup_error = _cleanup_owned_lock(
                lock_file,
                active_stage.file_stat,
                active_stage,
                primary_error,
            )
        else:
            cleanup_error = _cleanup_private_file_stage(
                active_stage,
                primary_error,
            )
    if primary_error is not None and cleanup_error is primary_error:
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        if primary_error is None:
            raise cleanup_error
        raise cleanup_error from primary_error


class TaskManager:
    """Persist task definitions and per-task seen-item state."""

    def __init__(self, data_file: str = "tasks.json"):
        self.data_file = Path(data_file).expanduser().resolve()
        self.lock_file = self.data_file.with_suffix(f"{self.data_file.suffix}.lock")
        self.tasks: list[dict[str, Any]] = []
        self._load()

    def _resolve_state_file(self, state_file: str | None) -> str | None:
        if not state_file:
            return None
        state_path = Path(str(state_file)).expanduser()
        if not state_path.is_absolute():
            state_path = self.data_file.parent / state_path
        return os.path.abspath(state_path)

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
        state_file = normalized["state_file"]
        if state_file:
            state_path = Path(str(state_file)).expanduser()
            # New tasks are made absolute before normalization. Preserve a
            # legacy relative value so an upgrade never silently redirects it
            # from the scheduler's old working directory to another file.
            normalized["state_file"] = (
                os.path.abspath(state_path)
                if state_path.is_absolute()
                else str(state_file)
            )
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

    def _schema_error(self, detail: str) -> ValueError:
        return ValueError(f"invalid task file schema: {self.data_file}: {detail}")

    def _validate_loaded_task(
        self,
        task: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        location = f"tasks[{index}]"
        for required in ("id", "keyword"):
            if required not in task:
                raise self._schema_error(f"{location}.{required} is required")
        state_file = task.get("state_file")
        if state_file is not None and not isinstance(state_file, str):
            raise self._schema_error(f"{location}.state_file must be a string or null")

        normalized = self._normalize_task(task)

        for field_name in ("id", "keyword"):
            value = normalized[field_name]
            if not isinstance(value, str) or not value.strip():
                raise self._schema_error(
                    f"{location}.{field_name} must be a non-empty string"
                )

        for field_name in ("criteria",):
            if not isinstance(normalized[field_name], str):
                raise self._schema_error(f"{location}.{field_name} must be a string")

        for field_name in ("location", "state_file", "last_run", "last_error"):
            value = normalized[field_name]
            if value is not None and not isinstance(value, str):
                raise self._schema_error(
                    f"{location}.{field_name} must be a string or null"
                )

        for field_name in ("created_at", "updated_at"):
            value = normalized[field_name]
            if not isinstance(value, str) or not value.strip():
                raise self._schema_error(
                    f"{location}.{field_name} must be a non-empty string"
                )

        if normalized["status"] not in {"running", "stopped"}:
            raise self._schema_error(f"{location}.status must be running or stopped")

        for field_name in ("pages", "retries"):
            value = normalized[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise self._schema_error(
                    f"{location}.{field_name} must be an integer of at least 1"
                )

        results_count = normalized["results_count"]
        if (
            isinstance(results_count, bool)
            or not isinstance(results_count, int)
            or results_count < 0
        ):
            raise self._schema_error(
                f"{location}.results_count must be a non-negative integer"
            )

        last_results = normalized["last_results"]
        if not isinstance(last_results, list) or len(last_results) > MAX_LAST_RESULTS:
            raise self._schema_error(
                f"{location}.last_results must be a list of at most "
                f"{MAX_LAST_RESULTS} objects"
            )
        if any(not isinstance(item, dict) for item in last_results):
            raise self._schema_error(f"{location}.last_results entries must be objects")

        seen_item_ids = normalized["seen_item_ids"]
        if not isinstance(seen_item_ids, list) or len(seen_item_ids) > MAX_SEEN_ITEMS:
            raise self._schema_error(
                f"{location}.seen_item_ids must be a list of at most "
                f"{MAX_SEEN_ITEMS} strings"
            )
        if any(
            not isinstance(item_id, str) or not item_id for item_id in seen_item_ids
        ):
            raise self._schema_error(
                f"{location}.seen_item_ids entries must be non-empty strings"
            )

        try:
            self._validate_prices(
                normalized["min_price"],
                normalized["max_price"],
            )
        except ValueError as exc:
            raise self._schema_error(f"{location}: {exc}") from exc

        return normalized

    def _load(self) -> None:
        if not self.data_file.exists():
            self.tasks = []
            return
        try:
            payload = json.loads(
                self.data_file.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid task file {self.data_file}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise ValueError(  # noqa: TRY004 - invalid persisted data is a value error.
                f"invalid task file schema: {self.data_file}"
            )
        schema_version = payload.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, SCHEMA_VERSION}
        ):
            raise self._schema_error(f"schema_version must be 1 or {SCHEMA_VERSION}")
        if "updated_at" in payload:
            updated_at = payload["updated_at"]
            if not isinstance(updated_at, str) or not updated_at.strip():
                raise self._schema_error("updated_at must be a non-empty string")

        normalized_tasks: list[dict[str, Any]] = []
        task_ids: set[str] = set()
        for index, task in enumerate(payload["tasks"]):
            if not isinstance(task, dict):
                raise self._schema_error(f"tasks[{index}] must be an object")
            normalized = self._validate_loaded_task(task, index)
            task_id = normalized["id"]
            if task_id in task_ids:
                raise self._schema_error(f"duplicate task id: {task_id}")
            task_ids.add(task_id)
            normalized_tasks.append(normalized)
        self.tasks = normalized_tasks

    def _save(self, *, on_commit: Callable[[], None] | None = None) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "tasks": self.tasks,
        }
        stage = _new_private_file_stage(
            self.data_file.parent,
            f".{self.data_file.name}.",
        )
        committed = False
        replace_attempted = False
        commit_notified = False

        def notify_commit() -> None:
            nonlocal commit_notified
            if commit_notified:
                return
            if on_commit is not None:
                on_commit()
            commit_notified = True

        def reconcile_commit() -> bool:
            if committed:
                return True
            return stage.file_stat is not None and _same_file(
                self.data_file, stage.file_stat
            )

        def confirm_commit() -> None:
            if not reconcile_commit():
                raise OSError(
                    "published task file was replaced before commit confirmation"
                )

        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            _prepare_private_file_stage(stage, encoding="utf-8")
            stream = stage.stream
            if stream is None or stage.file_stat is None:
                raise OSError(  # noqa: TRY301 - outer block owns cleanup.
                    "task staging identity was not established"
                )
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            _close_private_stage_stream(stage)
            replace_attempted = True
            os.replace(stage.path, self.data_file)
            confirm_commit()
            committed = True
            notify_commit()
            cleanup_error = _cleanup_private_file_stage(stage)
            if cleanup_error is not None:
                raise cleanup_error  # noqa: TRY301 - retain commit evidence below.
        except BaseException as primary_error:
            error_to_raise = primary_error
            commit_status = (
                TASK_COMMIT_RECORDED if committed else TASK_COMMIT_NOT_RECORDED
            )
            if replace_attempted and not committed:
                try:
                    if reconcile_commit():
                        committed = True
                        commit_status = TASK_COMMIT_RECORDED
                        notify_commit()
                except BaseException as reconciliation_error:  # noqa: BLE001
                    commit_status = TASK_COMMIT_NOT_ESTABLISHED
                    error_to_raise = _prefer_cleanup_interruption(
                        error_to_raise,
                        reconciliation_error,
                        "failed to reconcile the atomic task-file commit",
                    )

            cleaned_error = _cleanup_private_file_stage(stage, error_to_raise)
            if cleaned_error is not None:
                error_to_raise = cleaned_error

            _set_task_commit_status(error_to_raise, commit_status)
            if error_to_raise is primary_error:
                raise
            raise error_to_raise from primary_error

    @contextmanager
    def _mutation(
        self,
        *,
        interrupted_result: Callable[[], Any] | None = None,
        commit_observer: Callable[[Any], None] | None = None,
        uncertain_observer: Callable[[Any], None] | None = None,
    ) -> Iterator[None]:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        committed = False
        committed_result: Any = None
        evidence_requested = (
            interrupted_result is not None
            or commit_observer is not None
            or uncertain_observer is not None
        )

        def mark_committed() -> None:
            nonlocal committed, committed_result
            result = interrupted_result() if interrupted_result is not None else None
            snapshot = copy.deepcopy(result)
            if commit_observer is not None:
                commit_observer(snapshot)
            committed_result = snapshot
            committed = True

        try:
            with _exclusive_lock(self.lock_file):
                self._load()
                yield
                self._save(on_commit=mark_committed)
        except BaseException as exc:  # noqa: BLE001 - retain post-commit evidence
            if not evidence_requested:
                raise
            task_commit_status = getattr(
                exc,
                "task_commit_status",
                TASK_COMMIT_RECORDED if committed else TASK_COMMIT_NOT_RECORDED,
            )
            if task_commit_status == TASK_COMMIT_RECORDED and not committed:
                committed_result = (
                    copy.deepcopy(interrupted_result())
                    if interrupted_result is not None
                    else None
                )
                committed = True
            possible_result: Any = None
            if task_commit_status == TASK_COMMIT_NOT_ESTABLISHED:
                possible_result = (
                    copy.deepcopy(interrupted_result())
                    if interrupted_result is not None
                    else None
                )
                if uncertain_observer is not None:
                    uncertain_observer(possible_result)
            if _is_interruption(exc):
                raise TaskMutationInterrupted(
                    exc,
                    task_commit_status=task_commit_status,
                    result=committed_result,
                    possible_result=possible_result,
                ) from exc
            if _is_control_flow(exc):
                setattr(
                    exc,
                    "committed",
                    task_commit_status == TASK_COMMIT_RECORDED,
                )
                setattr(
                    exc,
                    "result",
                    copy.deepcopy(committed_result)
                    if task_commit_status == TASK_COMMIT_RECORDED
                    else None,
                )
                setattr(
                    exc,
                    "possible_result",
                    copy.deepcopy(possible_result)
                    if task_commit_status == TASK_COMMIT_NOT_ESTABLISHED
                    else None,
                )
                _set_task_commit_status(exc, task_commit_status)
                raise
            if task_commit_status == TASK_COMMIT_NOT_RECORDED:
                raise
            if task_commit_status == TASK_COMMIT_RECORDED:
                raise TaskMutationCommittedError(
                    exc,
                    result=committed_result,
                ) from exc
            raise TaskMutationPersistenceError(
                exc,
                task_commit_status=task_commit_status,
                possible_result=possible_result,
            ) from exc

    @staticmethod
    def _validate_prices(min_price: float | None, max_price: float | None) -> None:
        for label, value in (("minimum", min_price), ("maximum", max_price)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(  # noqa: TRY004 - public input contract uses ValueError.
                    f"{label} price must be a number"
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{label} price must be finite")
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
        criteria: str,
        pages: int,
        retries: int,
        state_file: str | None,
    ) -> dict[str, Any] | None:
        for task in self.tasks:
            if (
                task.get("keyword") == keyword
                and task.get("max_price") == max_price
                and task.get("min_price") == min_price
                and task.get("location") == location
                and task.get("criteria", "") == criteria
                and int(task.get("pages", 1)) == pages
                and int(task.get("retries", 3)) == retries
                and task.get("state_file") == state_file
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
        progress: TaskMutationProgress | None = None,
    ) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword must not be empty")
        self._validate_prices(min_price, max_price)
        if pages < 1:
            raise ValueError("pages must be at least 1")
        if retries < 1:
            raise ValueError("retries must be at least 1")
        state_file = self._resolve_state_file(state_file)
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        result: dict[str, Any] | None = None

        with self._mutation(
            interrupted_result=lambda: result,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            if skip_duplicate:
                existing = self._find_existing_task(
                    keyword,
                    max_price,
                    min_price,
                    location,
                    criteria,
                    pages,
                    retries,
                    state_file,
                )
                if existing:
                    result = copy.deepcopy(existing)
                    result["existing"] = True
            if result is None:
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
                result = copy.deepcopy(task)
        if result is None:
            raise RuntimeError("task creation completed without a result")
        return copy.deepcopy(result)

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

    def set_status(
        self,
        task_id: str,
        status: str,
        *,
        progress: TaskMutationProgress | None = None,
    ) -> bool:
        if status not in {"running", "stopped"}:
            raise ValueError("status must be running or stopped")
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        found = False
        with self._mutation(
            interrupted_result=lambda: found,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["status"] = status
                    task["updated_at"] = _now()
                    found = True
                    break
        return found

    def delete_task(
        self,
        task_id: str,
        *,
        progress: TaskMutationProgress | None = None,
    ) -> bool:
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        found = False
        with self._mutation(
            interrupted_result=lambda: found,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            for index, task in enumerate(self.tasks):
                if task.get("id") == task_id:
                    self.tasks.pop(index)
                    found = True
                    break
        return found

    def reset_seen(
        self,
        task_id: str,
        *,
        progress: TaskMutationProgress | None = None,
    ) -> bool:
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        found = False
        with self._mutation(
            interrupted_result=lambda: found,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["seen_item_ids"] = []
                    task["updated_at"] = _now()
                    found = True
                    break
        return found

    def record_run(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress | None = None,
    ) -> list[dict[str, Any]]:
        run_progress = progress if progress is not None else RecordRunProgress()
        run_progress.reset()
        new_items: list[dict[str, Any]] = []
        with self._mutation(
            interrupted_result=lambda: new_items,
            commit_observer=run_progress._mark_committed,
            uncertain_observer=run_progress._mark_not_established,
        ):
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

    def record_error(
        self,
        task_id: str,
        error: str,
        *,
        progress: TaskMutationProgress | None = None,
    ) -> bool:
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        found = False
        with self._mutation(
            interrupted_result=lambda: found,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["last_run"] = _now()
                    task["last_error"] = str(error)[:1_000]
                    task["updated_at"] = _now()
                    found = True
                    break
        return found


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Manage Xianyu monitor tasks")
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


def _command_result(args: argparse.Namespace, raw_result: Any) -> Any:
    if args.command in {"stop", "resume", "reset-seen"}:
        return {"updated": raw_result}
    if args.command == "delete":
        return {"deleted": raw_result}
    return raw_result


def _command_cleanup_evidence(
    error: BaseException | None = None,
) -> dict[str, Any]:
    failures = getattr(error, "cleanup_failures", None)
    if isinstance(failures, list) and failures:
        return {
            "cleanup": {
                "status": "failed",
                "errors": list(failures),
            }
        }
    return {"cleanup": {"status": "complete-or-not-required"}}


def _command_mutation_evidence(
    args: argparse.Namespace,
    progress: TaskMutationProgress | None,
    error: BaseException,
) -> dict[str, Any]:
    status = getattr(error, "task_commit_status", None)
    if status not in TASK_COMMIT_STATUSES and progress is not None:
        status = progress.task_commit_status
    if status not in TASK_COMMIT_STATUSES:
        status = "not-attempted"

    report: dict[str, Any] = {
        "task_commit_status": status,
        "persistence": {"status": status},
        **_command_cleanup_evidence(error),
    }
    if status == TASK_COMMIT_RECORDED:
        raw_result = getattr(error, "result", None)
        if raw_result is None and progress is not None:
            raw_result = progress.result
        report["result"] = _command_result(args, copy.deepcopy(raw_result))
    elif status == TASK_COMMIT_NOT_ESTABLISHED:
        possible_result = getattr(error, "possible_result", None)
        if possible_result is None and progress is not None:
            possible_result = progress.possible_result
        report["possible_result"] = _command_result(
            args,
            copy.deepcopy(possible_result),
        )
    return report


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mutation_progress: TaskMutationProgress | None = None
    try:
        manager = TaskManager(args.data_file)
        if args.command == "create":
            mutation_progress = TaskMutationProgress()
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
                progress=mutation_progress,
            )
        elif args.command == "list":
            result = manager.list_tasks(running_only=args.running)
        elif args.command == "stop":
            mutation_progress = TaskMutationProgress()
            result = {
                "updated": manager.set_status(
                    args.task_id,
                    "stopped",
                    progress=mutation_progress,
                )
            }
        elif args.command == "resume":
            mutation_progress = TaskMutationProgress()
            result = {
                "updated": manager.set_status(
                    args.task_id,
                    "running",
                    progress=mutation_progress,
                )
            }
        elif args.command == "delete":
            mutation_progress = TaskMutationProgress()
            result = {
                "deleted": manager.delete_task(
                    args.task_id,
                    progress=mutation_progress,
                )
            }
        else:
            mutation_progress = TaskMutationProgress()
            result = {
                "updated": manager.reset_seen(
                    args.task_id,
                    progress=mutation_progress,
                )
            }
        print(
            json.dumps(
                {
                    "ok": True,
                    "result": result,
                    **_command_cleanup_evidence(),
                },
                ensure_ascii=True,
                indent=2,
                allow_nan=False,
            )
        )
    except TaskMutationInterrupted as exc:
        cause = getattr(exc, "cause_error", exc)
        report = {
            "ok": False,
            "error": "task command cancelled",
            "error_type": type(cause).__name__,
            **_command_mutation_evidence(args, mutation_progress, exc),
        }
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 130
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        report = {
            "ok": False,
            "error": "task command cancelled",
            "error_type": type(exc).__name__,
            **_command_mutation_evidence(args, mutation_progress, exc),
        }
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 130
    except TaskMutationPersistenceError as exc:
        cause = getattr(exc, "cause_error", exc)
        report = {
            "ok": False,
            "error": str(cause),
            "error_type": type(cause).__name__,
            **_command_mutation_evidence(args, mutation_progress, exc),
        }
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 2
    except (KeyError, OSError, TimeoutError, ValueError) as exc:
        report = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            **_command_cleanup_evidence(exc),
        }
        if args.command != "list":
            report.update(
                _command_mutation_evidence(
                    args,
                    mutation_progress,
                    exc,
                )
            )
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
