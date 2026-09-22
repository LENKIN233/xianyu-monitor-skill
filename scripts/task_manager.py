#!/usr/bin/env python3
"""Manage persistent Xianyu monitor tasks safely."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

if __package__:
    from .cli_contract import (
        MAX_SEARCH_PAGES,
        MAX_SEARCH_RETRIES,
        JsonArgumentParser,
        sigterm_cancellable,
    )
else:
    from cli_contract import (
        MAX_SEARCH_PAGES,
        MAX_SEARCH_RETRIES,
        JsonArgumentParser,
        sigterm_cancellable,
    )

SCHEMA_VERSION = 3
TASK_TRANSFER_SCHEMA = 1
TASK_UPDATE_PREVIEW_VERSION = 1
MAX_TASK_TRANSFER_BYTES = 2 * 1024 * 1024
MAX_SEEN_ITEMS = 50_000
MAX_LAST_RESULTS = 100
MAX_OUTBOX_EVENTS = 50_000
OUTBOX_ITEM_FIELDS = (
    "id",
    "title",
    "price",
    "location",
    "publish_time",
    "wants",
    "tags",
    "url",
)
TASK_DEFINITION_FIELDS = (
    "keyword",
    "min_price",
    "max_price",
    "location",
    "criteria",
    "pages",
    "retries",
    "state_file",
    "browser_channel",
    "status",
)
PORTABLE_TASK_FIELDS = tuple(
    field_name
    for field_name in TASK_DEFINITION_FIELDS
    if field_name not in {"state_file", "status"}
)
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


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def outbox_idempotency_key(task_id: str, item_id: str) -> str:
    """Return the stable delivery key shared by monitor and storage evidence."""

    return outbox_generation_key(task_id, item_id, 0)


def outbox_generation_key(task_id: str, item_id: str, generation: int) -> str:
    return _canonical_sha256(
        {
            "version": 1,
            "task_id": task_id,
            "item_id": item_id,
            "delivery_generation": generation,
        }
    )


def _expected_preview_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise argparse.ArgumentTypeError(
            "expected preview SHA-256 must contain 64 hexadecimal characters"
        )
    return normalized


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


class TaskFileNotFoundError(ValueError):
    """The selected task store is absent where an existing store is required."""


class TaskFileChangedError(OSError):
    """The task store no longer matches the file loaded for this mutation."""


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


@dataclass(frozen=True)
class _TaskFileSnapshot:
    """Identity, metadata, and content evidence captured through one open file."""

    metadata: os.stat_result
    sha256: str


_USE_LOADED_SNAPSHOT = object()


def _same_task_file_metadata(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return (
        os.path.samestat(expected, actual)
        and expected.st_size == actual.st_size
        and expected.st_mtime_ns == actual.st_mtime_ns
        and expected.st_ctime_ns == actual.st_ctime_ns
        and expected.st_uid == actual.st_uid
        and stat.S_IMODE(expected.st_mode) == stat.S_IMODE(actual.st_mode)
        and stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
    )


def _read_task_file_snapshot(path: Path) -> tuple[bytes, _TaskFileSnapshot]:
    """Read bytes and bind them to stable file metadata without path disclosure."""

    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise TaskFileChangedError(  # noqa: TRY301 - translated below.
                    "task file is not a regular file"
                )
            payload = stream.read()
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        raise
    except TaskFileChangedError:
        raise
    except OSError as exc:
        raise TaskFileChangedError("task file is not safely readable") from exc
    if not _same_task_file_metadata(before, after):
        raise TaskFileChangedError("task file changed while being read")
    return payload, _TaskFileSnapshot(
        metadata=after,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _require_task_file_snapshot(
    path: Path,
    expected: _TaskFileSnapshot | None,
) -> None:
    """Fail closed if a mutation's loaded task store disappeared or changed."""

    try:
        _payload, actual = _read_task_file_snapshot(path)
    except FileNotFoundError as exc:
        if expected is None:
            return
        raise TaskFileChangedError("task file disappeared during mutation") from exc
    except TaskFileChangedError:
        raise
    if expected is None:
        raise TaskFileChangedError("task file appeared during mutation")
    if (
        not _same_task_file_metadata(expected.metadata, actual.metadata)
        or expected.sha256 != actual.sha256
    ):
        raise TaskFileChangedError("task file changed during mutation")


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
    if os.name != "nt":
        os.fchmod(stage.stream.fileno(), stat.S_IRUSR | stat.S_IWUSR)
        secured = os.fstat(stage.stream.fileno())
        if not os.path.samestat(stage.file_stat, secured):
            raise OSError("private task staging file changed while being secured")
        if not stat.S_ISREG(secured.st_mode):
            raise OSError("private task staging file has an unexpected type")
        if secured.st_uid != os.geteuid():
            raise OSError("private task staging file is not user-owned")
        if stat.S_IMODE(secured.st_mode) != stat.S_IRUSR | stat.S_IWUSR:
            raise OSError("private task staging file permissions are not 0600")
        stage.file_stat = secured


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

    def __init__(
        self,
        data_file: str = "tasks.json",
        *,
        allow_missing: bool = True,
    ):
        self.data_file = Path(data_file).expanduser().resolve()
        self.lock_file = self.data_file.with_suffix(f"{self.data_file.suffix}.lock")
        self.allow_missing = allow_missing
        self.tasks: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.last_outbox_events: list[dict[str, Any]] = []
        self._loaded_snapshot: _TaskFileSnapshot | None = None
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
        normalized.setdefault("browser_channel", None)
        browser_channel = normalized["browser_channel"]
        if isinstance(browser_channel, str):
            normalized["browser_channel"] = browser_channel.strip() or None
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
        normalized.setdefault("delivery_generation", 0)
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
        browser_channel = task.get("browser_channel")
        if browser_channel is not None and not isinstance(browser_channel, str):
            raise self._schema_error(
                f"{location}.browser_channel must be a string or null"
            )

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

        for field_name in (
            "location",
            "state_file",
            "browser_channel",
            "last_run",
            "last_error",
        ):
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

        if not isinstance(normalized["status"], str) or normalized["status"] not in {
            "running",
            "stopped",
        }:
            raise self._schema_error(f"{location}.status must be running or stopped")

        # v1.0 allowed any positive value. Keep those task files readable so
        # users can list/delete them after an upgrade; monitor enforces the
        # current operational ceiling before opening a browser.
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

        delivery_generation = normalized["delivery_generation"]
        if (
            isinstance(delivery_generation, bool)
            or not isinstance(delivery_generation, int)
            or delivery_generation < 0
        ):
            raise self._schema_error(
                f"{location}.delivery_generation must be a non-negative integer"
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
        self._loaded_snapshot = None
        try:
            raw_payload, loaded_snapshot = _read_task_file_snapshot(self.data_file)
            payload = json.loads(
                raw_payload.decode("utf-8"),
                parse_constant=_reject_nonfinite_json_constant,
            )
        except FileNotFoundError as exc:
            if not self.allow_missing:
                raise TaskFileNotFoundError("task file does not exist") from exc
            self.tasks = []
            self.outbox = []
            return
        except (OSError, RecursionError, ValueError) as exc:
            raise ValueError(f"invalid task file {self.data_file}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise ValueError(  # noqa: TRY004 - invalid persisted data is a value error.
                f"invalid task file schema: {self.data_file}"
            )
        schema_version = payload.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, 2, SCHEMA_VERSION}
        ):
            raise self._schema_error(
                f"schema_version must be 1, 2, or {SCHEMA_VERSION}"
            )
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
        raw_outbox = payload.get("outbox", [])
        if not isinstance(raw_outbox, list) or len(raw_outbox) > MAX_OUTBOX_EVENTS:
            raise self._schema_error(
                f"outbox must be a list of at most {MAX_OUTBOX_EVENTS} events"
            )
        normalized_outbox: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        for index, event in enumerate(raw_outbox):
            if not isinstance(event, dict) or set(event) != {
                "idempotency_key",
                "task_id",
                "item_id",
                "delivery_generation",
                "created_at",
                "payload",
            }:
                raise self._schema_error(f"outbox[{index}] has invalid fields")
            event_id = event.get("idempotency_key")
            if (
                not isinstance(event_id, str)
                or len(event_id) != 64
                or any(character not in "0123456789abcdef" for character in event_id)
                or event_id in event_ids
            ):
                raise self._schema_error(f"outbox[{index}].idempotency_key is invalid")
            if any(
                not isinstance(event.get(field_name), str) or not event[field_name]
                for field_name in ("task_id", "item_id", "created_at")
            ) or not isinstance(event.get("payload"), dict):
                raise self._schema_error(f"outbox[{index}] has invalid values")
            generation = event.get("delivery_generation")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
                or not hmac.compare_digest(
                    event_id,
                    outbox_generation_key(
                        event["task_id"],
                        event["item_id"],
                        generation,
                    ),
                )
            ):
                raise self._schema_error(f"outbox[{index}] delivery key is invalid")
            event_payload = event["payload"]
            event_item = event_payload.get("item")
            expected_url = "https://www.goofish.com/item?id=" + quote(
                event["item_id"], safe=""
            )
            if (
                set(event_payload) != {"task_id", "keyword", "criteria", "item"}
                or event_payload.get("task_id") != event["task_id"]
                or not isinstance(event_payload.get("keyword"), str)
                or not isinstance(event_payload.get("criteria"), str)
                or not isinstance(event_item, dict)
                or not set(event_item) <= set(OUTBOX_ITEM_FIELDS)
                or event_item.get("id") != event["item_id"]
                or event_item.get("url") != expected_url
            ):
                raise self._schema_error(f"outbox[{index}].payload is invalid")
            try:
                json.dumps(event_payload, ensure_ascii=False, allow_nan=False)
            except (RecursionError, TypeError, ValueError) as exc:
                raise self._schema_error(f"outbox[{index}].payload is invalid") from exc
            event_ids.add(event_id)
            normalized_outbox.append(copy.deepcopy(event))
        self.outbox = normalized_outbox
        self._loaded_snapshot = loaded_snapshot

    def _save(
        self,
        *,
        on_commit: Callable[[], None] | None = None,
        expected_snapshot: _TaskFileSnapshot | None | object = _USE_LOADED_SNAPSHOT,
    ) -> None:
        if expected_snapshot is _USE_LOADED_SNAPSHOT:
            expected_snapshot = self._loaded_snapshot
        if expected_snapshot is not None and not isinstance(
            expected_snapshot,
            _TaskFileSnapshot,
        ):
            raise TypeError("invalid task file snapshot")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "tasks": self.tasks,
            "outbox": self.outbox,
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
            _require_task_file_snapshot(self.data_file, expected_snapshot)
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
                expected_snapshot = self._loaded_snapshot
                yield
                self._save(
                    on_commit=mark_committed,
                    expected_snapshot=expected_snapshot,
                )
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
        browser_channel: str | None,
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
                and task.get("browser_channel") == browser_channel
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
        browser_channel: str | None = None,
        progress: TaskMutationProgress | None = None,
    ) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword must not be empty")
        self._validate_prices(min_price, max_price)
        if isinstance(pages, bool) or not isinstance(pages, int):
            raise ValueError(  # noqa: TRY004 - public input uses ValueError.
                "pages must be an integer"
            )
        if not 1 <= pages <= MAX_SEARCH_PAGES:
            raise ValueError(f"pages must be between 1 and {MAX_SEARCH_PAGES}")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise ValueError(  # noqa: TRY004 - public input uses ValueError.
                "retries must be an integer"
            )
        if not 1 <= retries <= MAX_SEARCH_RETRIES:
            raise ValueError(f"retries must be between 1 and {MAX_SEARCH_RETRIES}")
        state_file = self._resolve_state_file(state_file)
        if browser_channel is not None and not isinstance(browser_channel, str):
            raise ValueError("browser channel must be a string or null")
        browser_channel = browser_channel.strip() or None if browser_channel else None
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
                    browser_channel,
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
                        "browser_channel": browser_channel,
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

    @staticmethod
    def _task_definition(task: Mapping[str, Any]) -> dict[str, Any]:
        return {
            field_name: copy.deepcopy(task.get(field_name))
            for field_name in TASK_DEFINITION_FIELDS
        }

    @staticmethod
    def _portable_definition(task: Mapping[str, Any]) -> dict[str, Any]:
        return {
            field_name: copy.deepcopy(task.get(field_name))
            for field_name in PORTABLE_TASK_FIELDS
        }

    @staticmethod
    def _public_definition(task: Mapping[str, Any]) -> dict[str, Any]:
        definition = TaskManager._portable_definition(task)
        definition["status"] = task.get("status")
        definition["state_configured"] = bool(task.get("state_file"))
        return definition

    def _validated_update(
        self,
        task: Mapping[str, Any],
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        unexpected = set(changes) - set(TASK_DEFINITION_FIELDS)
        if unexpected:
            raise ValueError(f"unsupported task update field: {sorted(unexpected)[0]}")
        candidate = copy.deepcopy(dict(task))
        candidate.update(copy.deepcopy(dict(changes)))
        if "keyword" in changes:
            keyword = changes["keyword"]
            if not isinstance(keyword, str) or not keyword.strip():
                raise ValueError("keyword must be a non-empty string")
            candidate["keyword"] = keyword.strip()
        if "state_file" in changes:
            candidate["state_file"] = self._resolve_state_file(changes["state_file"])
        if "browser_channel" in changes:
            channel = changes["browser_channel"]
            if channel is not None and not isinstance(channel, str):
                raise ValueError("browser channel must be a string or null")
            candidate["browser_channel"] = channel.strip() or None if channel else None
        for field_name, maximum in (
            ("pages", MAX_SEARCH_PAGES),
            ("retries", MAX_SEARCH_RETRIES),
        ):
            value = candidate[field_name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(  # noqa: TRY004 - public input uses ValueError.
                    f"{field_name} must be an integer"
                )
            if not 1 <= value <= maximum:
                raise ValueError(f"{field_name} must be between 1 and {maximum}")
        candidate["updated_at"] = task["updated_at"]
        return self._validate_loaded_task(candidate, 0)

    def _update_preview(
        self,
        task: Mapping[str, Any],
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate = self._validated_update(task, changes)
        before = self._task_definition(task)
        after = self._task_definition(candidate)
        diff: list[dict[str, Any]] = []
        for field_name in TASK_DEFINITION_FIELDS:
            if before[field_name] == after[field_name]:
                continue
            if field_name == "state_file":
                diff.append(
                    {
                        "field": field_name,
                        "sensitive": True,
                        "before_configured": bool(before[field_name]),
                        "after_configured": bool(after[field_name]),
                    }
                )
            else:
                diff.append(
                    {
                        "field": field_name,
                        "before": before[field_name],
                        "after": after[field_name],
                    }
                )
        preview_sha256 = _canonical_sha256(
            {
                "version": TASK_UPDATE_PREVIEW_VERSION,
                "operation": "update",
                "task_id": task["id"],
                "before": before,
                "after": after,
            }
        )
        return {
            "operation": "update",
            "task_id": task["id"],
            "changed": bool(diff),
            "diff": diff,
            "result": self._public_definition(candidate),
            "approval": {
                "version": TASK_UPDATE_PREVIEW_VERSION,
                "preview_sha256": preview_sha256,
                "binds": ["task_id", "before", "after"],
            },
        }

    def preview_update(
        self,
        task_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._load()
        task = next((entry for entry in self.tasks if entry.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return self._update_preview(task, changes)

    def update_task(
        self,
        task_id: str,
        changes: Mapping[str, Any],
        *,
        expected_preview_sha256: str,
        progress: TaskMutationProgress | None = None,
    ) -> dict[str, Any]:
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
            task = next(
                (entry for entry in self.tasks if entry.get("id") == task_id),
                None,
            )
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            preview = self._update_preview(task, changes)
            if not hmac.compare_digest(
                preview["approval"]["preview_sha256"],
                expected_preview_sha256,
            ):
                raise ValueError("task update preview SHA-256 does not match")
            if not preview["changed"]:
                raise ValueError("task update does not change the selected task")
            candidate = self._validated_update(task, changes)
            candidate["updated_at"] = _now()
            task.clear()
            task.update(candidate)
            result = {
                **preview,
                "applied": True,
                "result": self._public_definition(task),
            }
        if result is None:
            raise RuntimeError("task update completed without a result")
        return copy.deepcopy(result)

    def export_tasks(self, *, running_only: bool = False) -> dict[str, Any]:
        self._load()
        selected = (
            [task for task in self.tasks if task.get("status") == "running"]
            if running_only
            else self.tasks
        )
        if len(selected) > 1_000:
            raise ValueError("task export contains too many tasks")
        return {
            "schema_version": TASK_TRANSFER_SCHEMA,
            "kind": "xianyu-monitor-task-definitions",
            "tasks": [
                {
                    "source_id": task["id"],
                    **self._portable_definition(task),
                }
                for task in selected
            ],
        }

    def _validated_import_definitions(
        self,
        transfer: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if set(transfer) != {"schema_version", "kind", "tasks"}:
            raise ValueError("task import document contains unexpected fields")
        if (
            transfer.get("schema_version") != TASK_TRANSFER_SCHEMA
            or transfer.get("kind") != "xianyu-monitor-task-definitions"
            or not isinstance(transfer.get("tasks"), list)
        ):
            raise ValueError("task import document has an unsupported schema")
        raw_tasks = transfer["tasks"]
        if len(raw_tasks) > 1_000:
            raise ValueError("task import document contains too many tasks")
        expected_fields = {"source_id", *PORTABLE_TASK_FIELDS}
        definitions: list[dict[str, Any]] = []
        source_ids: set[str] = set()
        for index, raw_task in enumerate(raw_tasks):
            if not isinstance(raw_task, dict) or set(raw_task) != expected_fields:
                raise ValueError(
                    f"task import definition {index} contains invalid fields"
                )
            source_id = raw_task["source_id"]
            if (
                not isinstance(source_id, str)
                or not source_id.strip()
                or source_id in source_ids
            ):
                raise ValueError(f"task import definition {index} has an invalid ID")
            source_ids.add(source_id)
            candidate = {
                "id": "task_import_validation",
                **{
                    field_name: copy.deepcopy(raw_task[field_name])
                    for field_name in PORTABLE_TASK_FIELDS
                },
                "state_file": None,
                "status": "stopped",
            }
            validated = self._validate_loaded_task(candidate, index)
            definitions.append(
                {
                    "source_id": source_id,
                    **self._portable_definition(validated),
                }
            )
        return definitions

    def _import_preview(self, transfer: Mapping[str, Any]) -> dict[str, Any]:
        definitions = self._validated_import_definitions(transfer)
        existing_definitions = {
            _canonical_sha256(self._portable_definition(task)) for task in self.tasks
        }
        additions: list[dict[str, Any]] = []
        skipped: list[str] = []
        observed_imports: set[str] = set()
        for definition in definitions:
            portable = {
                field_name: definition[field_name]
                for field_name in PORTABLE_TASK_FIELDS
            }
            fingerprint = _canonical_sha256(portable)
            if fingerprint in existing_definitions or fingerprint in observed_imports:
                skipped.append(definition["source_id"])
                continue
            observed_imports.add(fingerprint)
            additions.append(definition)
        preview_sha256 = _canonical_sha256(
            {
                "version": TASK_UPDATE_PREVIEW_VERSION,
                "operation": "import",
                "existing": sorted(existing_definitions),
                "transfer": {
                    "schema_version": TASK_TRANSFER_SCHEMA,
                    "kind": "xianyu-monitor-task-definitions",
                    "tasks": definitions,
                },
            }
        )
        return {
            "operation": "import",
            "add_count": len(additions),
            "skip_count": len(skipped),
            "additions": [
                {
                    "source_id": definition["source_id"],
                    "task": {
                        **{
                            field_name: definition[field_name]
                            for field_name in PORTABLE_TASK_FIELDS
                        },
                        "status": "stopped",
                        "state_configured": False,
                    },
                }
                for definition in additions
            ],
            "skipped_source_ids": skipped,
            "approval": {
                "version": TASK_UPDATE_PREVIEW_VERSION,
                "preview_sha256": preview_sha256,
                "binds": ["existing_definitions", "transfer"],
            },
        }

    def preview_import(self, transfer: Mapping[str, Any]) -> dict[str, Any]:
        self._load()
        return self._import_preview(transfer)

    def import_tasks(
        self,
        transfer: Mapping[str, Any],
        *,
        expected_preview_sha256: str,
        progress: TaskMutationProgress | None = None,
    ) -> dict[str, Any]:
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
            preview = self._import_preview(transfer)
            if not hmac.compare_digest(
                preview["approval"]["preview_sha256"],
                expected_preview_sha256,
            ):
                raise ValueError("task import preview SHA-256 does not match")
            imported: list[dict[str, Any]] = []
            existing_ids = {task["id"] for task in self.tasks}
            timestamp = _now()
            for addition in preview["additions"]:
                portable = {
                    field_name: addition["task"][field_name]
                    for field_name in PORTABLE_TASK_FIELDS
                }
                fingerprint = _canonical_sha256(portable)
                task_id = f"task_import_{fingerprint[:12]}"
                if task_id in existing_ids:
                    raise ValueError("task import generated an existing task ID")
                task = self._normalize_task(
                    {
                        "id": task_id,
                        **portable,
                        "state_file": None,
                        "status": "stopped",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
                self.tasks.append(task)
                existing_ids.add(task_id)
                imported.append(
                    {
                        "source_id": addition["source_id"],
                        "task_id": task_id,
                        "task": self._public_definition(task),
                    }
                )
            result = {
                **preview,
                "applied": True,
                "imported": imported,
            }
        if result is None:
            raise RuntimeError("task import completed without a result")
        return copy.deepcopy(result)

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
                    task["delivery_generation"] = (
                        int(task.get("delivery_generation", 0)) + 1
                    )
                    task["updated_at"] = _now()
                    found = True
                    break
        return found

    @staticmethod
    def _outbox_payload(
        task: Mapping[str, Any],
        item: Mapping[str, Any],
    ) -> dict[str, Any]:
        item_id = str(item["id"])
        try:
            sanitized_item = {
                field_name: copy.deepcopy(item[field_name])
                for field_name in OUTBOX_ITEM_FIELDS
                if field_name in item and field_name != "url"
            }
            json.dumps(sanitized_item, ensure_ascii=False, allow_nan=False)
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("outbox item contains unsupported values") from exc
        sanitized_item["id"] = item_id
        sanitized_item["url"] = "https://www.goofish.com/item?id=" + quote(
            item_id, safe=""
        )
        return {
            "task_id": task["id"],
            "keyword": task["keyword"],
            "criteria": task.get("criteria", ""),
            "item": sanitized_item,
        }

    def list_outbox(
        self,
        *,
        limit: int = 100,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1_000
        ):
            raise ValueError("outbox limit must be between 1 and 1000")
        self._load()
        events = (
            [event for event in self.outbox if event["task_id"] == task_id]
            if task_id
            else self.outbox
        )
        return copy.deepcopy(events[:limit])

    def acknowledge_outbox(
        self,
        idempotency_key: str,
        *,
        progress: TaskMutationProgress | None = None,
    ) -> bool:
        if len(idempotency_key) != 64 or any(
            character not in "0123456789abcdef" for character in idempotency_key
        ):
            raise ValueError("outbox idempotency key must be 64 lowercase hex digits")
        self._load()
        if not any(
            hmac.compare_digest(event["idempotency_key"], idempotency_key)
            for event in self.outbox
        ):
            return False
        operation_progress = (
            progress if progress is not None else TaskMutationProgress()
        )
        operation_progress.reset()
        acknowledged = False
        with self._mutation(
            interrupted_result=lambda: acknowledged,
            commit_observer=operation_progress._mark_committed,
            uncertain_observer=operation_progress._mark_not_established,
        ):
            for index, event in enumerate(self.outbox):
                if hmac.compare_digest(event["idempotency_key"], idempotency_key):
                    self.outbox.pop(index)
                    acknowledged = True
                    break
        return acknowledged

    def record_baseline(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress | None = None,
    ) -> list[dict[str, Any]]:
        return self._record_run(
            task_id,
            items,
            enqueue_outbox=False,
            progress=progress,
        )

    def record_run(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        progress: RecordRunProgress | None = None,
    ) -> list[dict[str, Any]]:
        return self._record_run(
            task_id,
            items,
            enqueue_outbox=True,
            progress=progress,
        )

    def _record_run(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        enqueue_outbox: bool,
        progress: RecordRunProgress | None = None,
    ) -> list[dict[str, Any]]:
        run_progress = progress if progress is not None else RecordRunProgress()
        run_progress.reset()
        self.last_outbox_events = []
        new_items: list[dict[str, Any]] = []
        outbox_events: list[dict[str, Any]] = []
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
            pending_outbox_keys = {
                str(event["idempotency_key"]) for event in self.outbox
            }
            delivery_generation = int(task.get("delivery_generation", 0))
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                ordered_ids.append(item_id)
                new_items.append(copy.deepcopy(item))

                if enqueue_outbox:
                    event_payload = self._outbox_payload(task, item)
                    idempotency_key = outbox_generation_key(
                        task_id,
                        item_id,
                        delivery_generation,
                    )
                    if idempotency_key in pending_outbox_keys:
                        continue
                    event = {
                        "idempotency_key": idempotency_key,
                        "task_id": task_id,
                        "item_id": item_id,
                        "delivery_generation": delivery_generation,
                        "created_at": _now(),
                        "payload": event_payload,
                    }
                    self.outbox.append(event)
                    pending_outbox_keys.add(idempotency_key)
                    outbox_events.append(copy.deepcopy(event))

            task["seen_item_ids"] = ordered_ids[-MAX_SEEN_ITEMS:]
            task["last_results"] = copy.deepcopy(items[:MAX_LAST_RESULTS])
            task["last_run"] = _now()
            task["last_error"] = None
            task["results_count"] = int(task.get("results_count", 0)) + len(items)
            task["updated_at"] = _now()
            if len(self.outbox) > MAX_OUTBOX_EVENTS:
                raise ValueError("outbox capacity is exhausted; deliver pending events")
        self.last_outbox_events = outbox_events
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


_DATA_FILE_HELP = (
    "task JSON path (default: tasks.json; accepted before or after the subcommand)"
)


def _add_subcommand_data_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-file",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=_DATA_FILE_HELP,
    )


def _task_transfer_input(value: str) -> str:
    if value == "-":
        return value
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(
            "--input must be an absolute path or -"
        ) from exc
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--input must be an absolute path or -")
    return str(path)


def _read_task_transfer(value: str) -> dict[str, Any]:
    try:
        if value == "-":
            raw = sys.stdin.buffer.read(MAX_TASK_TRANSFER_BYTES + 1)
        else:
            path = Path(value)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("task import input must be a regular non-symlink file")
            if metadata.st_size > MAX_TASK_TRANSFER_BYTES:
                raise ValueError("task import input exceeds the safety limit")
            with path.open("rb") as stream:
                raw = stream.read(MAX_TASK_TRANSFER_BYTES + 1)
    except OSError as exc:
        raise ValueError("task import input is unreadable") from exc
    if len(raw) > MAX_TASK_TRANSFER_BYTES:
        raise ValueError("task import input exceeds the safety limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ValueError("task import input is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(  # noqa: TRY004 - public input uses one value-error type.
            "task import input must contain one JSON object"
        )
    if set(payload) == {"ok", "result", "cleanup"}:
        cleanup = payload.get("cleanup")
        if (
            payload.get("ok") is not True
            or not isinstance(payload.get("result"), dict)
            or cleanup != {"status": "complete-or-not-required"}
        ):
            raise ValueError("task import CLI export envelope is invalid")
        payload = payload["result"]
    return payload


def _update_changes(args: argparse.Namespace) -> dict[str, Any]:
    changes = {
        field_name: getattr(args, field_name)
        for field_name in TASK_DEFINITION_FIELDS
        if hasattr(args, field_name)
    }
    if not changes:
        raise ValueError("task update requires at least one changed field")
    return changes


def _required_apply_digest(args: argparse.Namespace, operation: str) -> str:
    digest = args.expected_preview_sha256
    if digest is None:
        raise ValueError(f"task {operation} --apply requires --expected-preview-sha256")
    return digest


def _add_preview_apply_mode(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="show the exact diff")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply only with the matching preview SHA-256",
    )
    parser.add_argument(
        "--expected-preview-sha256",
        type=_expected_preview_sha256,
        help="digest returned by the immediately preceding preview",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Manage Xianyu monitor tasks")
    parser.add_argument(
        "--data-file",
        default="tasks.json",
        metavar="PATH",
        help=_DATA_FILE_HELP,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        help="create a persistent monitor task",
        description="Create a persistent Xianyu monitor task.",
    )
    _add_subcommand_data_file(create)
    create.add_argument("keyword", help="Xianyu search keyword")
    create.add_argument(
        "--max-price",
        type=float,
        help="inclusive finite, non-negative maximum price",
    )
    create.add_argument(
        "--min-price",
        type=float,
        help="inclusive finite, non-negative minimum price",
    )
    create.add_argument(
        "--location",
        help="case-insensitive location substring filter",
    )
    create.add_argument(
        "--criteria",
        default="",
        help="downstream analysis hint; not a deterministic collector filter",
    )
    create.add_argument(
        "--pages",
        type=int,
        default=1,
        help=f"pages per run (default: 1; range: 1-{MAX_SEARCH_PAGES})",
    )
    create.add_argument(
        "--retries",
        type=int,
        default=3,
        help=(
            "attempts for transient failures "
            f"(default: 3; range: 1-{MAX_SEARCH_RETRIES})"
        ),
    )
    create.add_argument(
        "--state",
        help="browser-state path; relatives resolve from the task file directory",
    )
    create.add_argument(
        "--browser-channel",
        help="Playwright browser executable channel; does not reuse a profile",
    )
    create.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="create even when an equivalent running task already exists",
    )

    list_parser = subparsers.add_parser("list", help="list persisted monitor tasks")
    _add_subcommand_data_file(list_parser)
    list_parser.add_argument(
        "--running",
        action="store_true",
        help="show only running tasks",
    )

    update = subparsers.add_parser(
        "update",
        help="preview or apply a task definition update",
    )
    _add_subcommand_data_file(update)
    update.add_argument("task_id", help="task ID returned by create/list")
    update.add_argument("--keyword", default=argparse.SUPPRESS)
    for field_name, option_name in (
        ("min_price", "min-price"),
        ("max_price", "max-price"),
    ):
        price = update.add_mutually_exclusive_group()
        price.add_argument(
            f"--{option_name}",
            dest=field_name,
            type=float,
            default=argparse.SUPPRESS,
        )
        price.add_argument(
            f"--clear-{option_name}",
            dest=field_name,
            action="store_const",
            const=None,
            default=argparse.SUPPRESS,
        )
    location = update.add_mutually_exclusive_group()
    location.add_argument("--location", default=argparse.SUPPRESS)
    location.add_argument(
        "--clear-location",
        dest="location",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
    )
    criteria = update.add_mutually_exclusive_group()
    criteria.add_argument("--criteria", default=argparse.SUPPRESS)
    criteria.add_argument(
        "--clear-criteria",
        dest="criteria",
        action="store_const",
        const="",
        default=argparse.SUPPRESS,
    )
    update.add_argument("--pages", type=int, default=argparse.SUPPRESS)
    update.add_argument("--retries", type=int, default=argparse.SUPPRESS)
    state = update.add_mutually_exclusive_group()
    state.add_argument("--state", dest="state_file", default=argparse.SUPPRESS)
    state.add_argument(
        "--clear-state",
        dest="state_file",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
    )
    channel = update.add_mutually_exclusive_group()
    channel.add_argument(
        "--browser-channel",
        dest="browser_channel",
        default=argparse.SUPPRESS,
    )
    channel.add_argument(
        "--clear-browser-channel",
        dest="browser_channel",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
    )
    update.add_argument(
        "--status",
        choices=("running", "stopped"),
        default=argparse.SUPPRESS,
    )
    _add_preview_apply_mode(update)

    export = subparsers.add_parser(
        "export",
        help="export portable definitions without state paths or runtime history",
    )
    _add_subcommand_data_file(export)
    export.add_argument("--running", action="store_true")

    import_parser = subparsers.add_parser(
        "import",
        help="preview or apply portable task definitions as stopped tasks",
    )
    _add_subcommand_data_file(import_parser)
    import_parser.add_argument(
        "--input",
        required=True,
        type=_task_transfer_input,
        help="absolute exported JSON path, or - for stdin",
    )
    _add_preview_apply_mode(import_parser)

    for command in ("stop", "resume", "delete", "reset-seen"):
        command_parser = subparsers.add_parser(
            command,
            help=f"{command.replace('-', ' ')} one persisted task",
        )
        _add_subcommand_data_file(command_parser)
        command_parser.add_argument("task_id", help="task ID returned by create/list")

    outbox = subparsers.add_parser(
        "outbox",
        help="list or acknowledge durable notification events",
    )
    _add_subcommand_data_file(outbox)
    outbox_commands = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_list = outbox_commands.add_parser("list", help="list pending events")
    _add_subcommand_data_file(outbox_list)
    outbox_list.add_argument("--task-id")
    outbox_list.add_argument("--limit", type=int, default=100)
    outbox_ack = outbox_commands.add_parser(
        "ack",
        help="acknowledge one event after successful external delivery",
    )
    _add_subcommand_data_file(outbox_ack)
    outbox_ack.add_argument("idempotency_key")
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
        manager = TaskManager(
            args.data_file,
            allow_missing=args.command in {"create", "import"},
        )
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
                browser_channel=args.browser_channel,
                skip_duplicate=not args.allow_duplicate,
                progress=mutation_progress,
            )
        elif args.command == "list":
            result = manager.list_tasks(running_only=args.running)
        elif args.command == "update":
            changes = _update_changes(args)
            if args.apply:
                mutation_progress = TaskMutationProgress()
                result = manager.update_task(
                    args.task_id,
                    changes,
                    expected_preview_sha256=_required_apply_digest(args, "update"),
                    progress=mutation_progress,
                )
            else:
                result = manager.preview_update(args.task_id, changes)
        elif args.command == "export":
            result = manager.export_tasks(running_only=args.running)
        elif args.command == "import":
            transfer = _read_task_transfer(args.input)
            if args.apply:
                mutation_progress = TaskMutationProgress()
                result = manager.import_tasks(
                    transfer,
                    expected_preview_sha256=_required_apply_digest(args, "import"),
                    progress=mutation_progress,
                )
            else:
                result = manager.preview_import(transfer)
        elif args.command == "outbox":
            if args.outbox_command == "list":
                result = manager.list_outbox(limit=args.limit, task_id=args.task_id)
            else:
                mutation_progress = TaskMutationProgress()
                result = {
                    "acknowledged": manager.acknowledge_outbox(
                        args.idempotency_key,
                        progress=mutation_progress,
                    )
                }
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
        if isinstance(exc, TaskFileNotFoundError):
            report.update(
                error_code="create-task-file",
                next_action={
                    "code": "create-task-file",
                    "hint": (
                        "Run task create with the intended --data-file path to "
                        "initialize it, or correct --data-file."
                    ),
                },
            )
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
