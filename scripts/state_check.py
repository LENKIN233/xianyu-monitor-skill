#!/usr/bin/env python3
"""Validate one private browser-state candidate without launching a browser."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .spider import (
        MAX_STATE_BYTES,
        StateFileError,
        StorageStateValidationError,
        _filter_goofish_storage_state,
        _has_storage_state_material,
    )
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from spider import (
        MAX_STATE_BYTES,
        StateFileError,
        StorageStateValidationError,
        _filter_goofish_storage_state,
        _has_storage_state_material,
    )


class StateAccessError(StateFileError):
    """The candidate path could not be safely opened for inspection."""


class StateChangedError(StateFileError):
    """The opened candidate changed while it was being inspected."""


@dataclass(frozen=True)
class _PathIdentity:
    path: Path
    lexical: os.stat_result
    resolved: os.stat_result


def _state_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--state must be an absolute path")
    return path


def _base_report() -> dict[str, Any]:
    return {
        "authentication": {"status": "not-established"},
        "identity": {"status": "not-evaluated"},
        "search_capability": {"status": "not-tested"},
        "cleanup": {"status": "complete-or-not-required"},
    }


def _privacy_check(metadata: os.stat_result) -> dict[str, Any]:
    if not stat.S_ISREG(metadata.st_mode):
        raise StateAccessError("browser-state path is not a regular file")
    if metadata.st_size > MAX_STATE_BYTES:
        raise StateFileError("browser-state file exceeds the 64 MiB safety limit")
    if os.name == "nt":
        return {"status": "platform-managed"}

    issues: list[str] = []
    if metadata.st_uid != os.geteuid():
        issues.append("not-owned-by-current-user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        issues.append("group-or-other-access-enabled")
    return {
        "status": "failed" if issues else "passed",
        "issues": issues,
    }


def _require_unchanged(before: os.stat_result, after: os.stat_result) -> None:
    if (
        not os.path.samestat(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or before.st_uid != after.st_uid
        or stat.S_IMODE(before.st_mode) != stat.S_IMODE(after.st_mode)
    ):
        raise StateChangedError("browser-state file changed during validation")


def _capture_path_identity(path: Path) -> _PathIdentity:
    try:
        return _PathIdentity(
            path=path,
            lexical=path.lstat(),
            resolved=path.stat(),
        )
    except FileNotFoundError as exc:
        raise StateAccessError("browser-state file does not exist") from exc
    except OSError as exc:
        raise StateAccessError("browser-state file is not accessible") from exc


def _require_path_still_points_to_open_file(
    identity: _PathIdentity,
    opened: os.stat_result,
) -> dict[str, Any]:
    try:
        lexical = identity.path.lstat()
        resolved = identity.path.stat()
    except OSError as exc:
        raise StateChangedError("browser-state path changed during validation") from exc
    if (
        not os.path.samestat(identity.lexical, lexical)
        or identity.lexical.st_ctime_ns != lexical.st_ctime_ns
        or identity.lexical.st_mtime_ns != lexical.st_mtime_ns
        or identity.lexical.st_size != lexical.st_size
        or stat.S_IFMT(identity.lexical.st_mode) != stat.S_IFMT(lexical.st_mode)
        or not os.path.samestat(identity.resolved, resolved)
        or not os.path.samestat(opened, resolved)
    ):
        raise StateChangedError("browser-state path changed during validation")
    final_privacy = _privacy_check(resolved)
    if final_privacy["status"] == "failed":
        return final_privacy
    _require_unchanged(identity.resolved, resolved)
    _require_unchanged(opened, resolved)
    return final_privacy


def _validate_state_payload(payload: bytes) -> None:
    if len(payload) > MAX_STATE_BYTES:
        raise StateFileError("browser-state file exceeds the 64 MiB safety limit")
    try:
        snapshot = json.loads(payload.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise StateFileError("browser state is unreadable or invalid JSON") from exc

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cookies"), list):
        raise StateFileError(
            "browser state must be a JSON object containing a cookies array"
        )

    enhanced = any(key in snapshot for key in ("env", "headers", "page", "storage"))
    origins = snapshot.get("origins", [])
    if enhanced:
        storage = snapshot.get("storage")
        if isinstance(storage, dict) and isinstance(storage.get("origins"), list):
            origins = storage["origins"]
    try:
        storage_state = _filter_goofish_storage_state(
            {"cookies": snapshot["cookies"], "origins": origins}
        )
    except StorageStateValidationError as exc:
        raise StateFileError("invalid browser state schema") from exc
    if not _has_storage_state_material(storage_state):
        raise StateFileError(
            "browser state contains no usable Goofish Cookie or origin storage data"
        )


def _open_state(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise StateAccessError("browser-state file does not exist") from exc
    except OSError as exc:
        raise StateAccessError("browser-state file is not accessible") from exc
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _privacy_failure(report: dict[str, Any], privacy: dict[str, Any]) -> dict[str, Any]:
    report["privacy"] = privacy
    report.update(
        error="browser-state file is not private",
        error_type="StatePrivacyError",
        next_action={
            "code": "fix-state-permissions",
            "hint": (
                "Restrict the exact state file to the current user, then rerun state."
            ),
        },
    )
    return report


def _inspect_open_state(
    stream: BinaryIO,
) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = stream.fileno()
    before = os.fstat(descriptor)
    privacy = _privacy_check(before)
    if privacy["status"] == "failed":
        return privacy, {"status": "not-inspected"}

    payload = stream.read(MAX_STATE_BYTES + 1)
    _validate_state_payload(payload)

    after = os.fstat(descriptor)
    final_privacy = _privacy_check(after)
    if final_privacy["status"] == "failed":
        return final_privacy, {"status": "not-inspected"}
    _require_unchanged(before, after)
    return final_privacy, {"status": "candidate-valid"}


def inspect_state(path: Path) -> dict[str, Any]:
    """Return path-private validation evidence for one authorized state file."""

    report: dict[str, Any] = {
        "ok": False,
        "state": {"status": "not-inspected"},
        "privacy": {"status": "not-established"},
        **_base_report(),
    }
    try:
        identity = _capture_path_identity(path)
        with _open_state(path) as stream:
            privacy, state = _inspect_open_state(stream)
            if privacy["status"] != "failed":
                privacy = _require_path_still_points_to_open_file(
                    identity,
                    os.fstat(stream.fileno()),
                )
        report["privacy"] = privacy
        if privacy["status"] == "failed":
            return _privacy_failure(report, privacy)
    except StateAccessError:
        report.update(
            state={"status": "not-established"},
            error="browser-state file does not exist or is inaccessible",
            error_type="StateAccessError",
            next_action={
                "code": "capture-or-import-state",
                "hint": (
                    "Capture or import a private candidate at the authorized path, "
                    "then rerun state."
                ),
            },
        )
        return report
    except StateChangedError:
        report.update(
            state={"status": "not-established"},
            error="browser-state file changed during validation",
            error_type="StateChangedError",
            next_action={
                "code": "retry-state-check",
                "hint": "Stop concurrent state rotation and rerun state.",
            },
        )
        return report
    except StateFileError:
        report.update(
            state={"status": "invalid"},
            error="browser-state candidate has invalid JSON or schema",
            error_type="StateFileError",
            next_action={
                "code": "capture-or-import-state",
                "hint": (
                    "Capture or import a valid private candidate state, then rerun "
                    "state."
                ),
            },
        )
        return report
    except OSError:
        report.update(
            state={"status": "not-established"},
            error="browser-state file changed or became inaccessible during validation",
            error_type="StateFileError",
            next_action={
                "code": "retry-state-check",
                "hint": "Stop concurrent state rotation and rerun state.",
            },
        )
        return report

    report.update(
        ok=True,
        state=state,
        next_action={
            "code": "run-controlled-search",
            "hint": (
                "Run a one-page, one-attempt search to prove capability for this run."
            ),
        },
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description=(
            "Validate one private browser-state candidate without launching a browser"
        )
    )
    parser.add_argument("--state", required=True, type=_state_path)
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = inspect_state(args.state)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        report = {
            "ok": False,
            "state": {"status": "not-established"},
            "privacy": {"status": "not-established"},
            "error": "state validation cancelled",
            "error_type": type(exc).__name__,
            **_base_report(),
        }
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 130
    print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
