#!/usr/bin/env python3
"""Create a Playwright login-state file with restrictive permissions."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import stat
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

_PRIVATE_CREDENTIAL_MODE = stat.S_IRUSR | stat.S_IWUSR
_PRIVATE_DIRECTORY_MODE = stat.S_IRWXU


@dataclass
class CredentialWriteProgress:
    state_status: str = "not-saved"
    output: Path | None = None
    cleanup_failures: list[str] = field(default_factory=list)

    def mark_committed(self, output: Path) -> None:
        self.state_status = "candidate-saved"
        self.output = output

    def observe_error(self, error: BaseException) -> None:
        status = getattr(error, "credential_state_status", None)
        if status in {"candidate-saved", "not-established", "not-saved"}:
            self.state_status = status
        output = getattr(error, "credential_output", None)
        if self.state_status == "candidate-saved" and isinstance(output, Path):
            self.output = output
        failures = getattr(error, "cleanup_failures", None)
        if isinstance(failures, list):
            for failure in failures:
                if failure not in self.cleanup_failures:
                    self.cleanup_failures.append(failure)


def _credential_output_path(output_file: str) -> Path:
    requested = Path(output_file).expanduser()
    if not requested.name:
        raise ValueError("output must name a file")
    return requested.parent.resolve() / requested.name


def parse_cookie_string(cookie_string: str) -> list[dict[str, Any]]:
    """Parse a Cookie header into Playwright cookie dictionaries."""

    cookies: list[dict[str, Any]] = []
    for raw_item in cookie_string.split(";"):
        item = raw_item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".goofish.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    if not cookies:
        raise ValueError("cookie input did not contain any name=value pairs")
    return cookies


def _is_interruption(error: BaseException) -> bool:
    return isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))


def _append_cleanup_failure(error: BaseException, message: str) -> None:
    failures = getattr(error, "cleanup_failures", None)
    if not isinstance(failures, list):
        failures = []
        setattr(error, "cleanup_failures", failures)
    if message not in failures:
        failures.append(message)


def _merge_cleanup_error(
    primary: BaseException,
    cleanup: BaseException,
    message: str,
) -> BaseException:
    """Keep an action cancellation ahead of an ordinary cleanup failure."""

    _append_cleanup_failure(primary, message)
    if _is_interruption(primary) or not _is_interruption(cleanup):
        return primary
    for failure in getattr(primary, "cleanup_failures", []):
        _append_cleanup_failure(cleanup, failure)
    setattr(cleanup, "cause_error", primary)
    return cleanup


@dataclass
class _CredentialStage:
    """Caller-owned paths and identities for one private credential staging file."""

    directory: Path
    path: Path
    directory_stat: os.stat_result | None = None
    file_stat: os.stat_result | None = None
    stream: Any = None


def _new_credential_stage(output: Path) -> _CredentialStage:
    directory = output.parent / f".{output.name}.{secrets.token_hex(16)}.tmp"
    return _CredentialStage(
        directory=directory,
        path=directory / "payload",
    )


def _same_credential_stage_directory(stage: _CredentialStage) -> bool:
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


def _prepare_credential_stage(stage: _CredentialStage) -> None:
    stage.directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    directory_stat = stage.directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("private credential staging directory has an unexpected type")
    stage.directory_stat = directory_stat
    stage.stream = stage.path.open("x", encoding="utf-8", newline="\n")
    stage.file_stat = os.fstat(stage.stream.fileno())
    if os.name != "nt":
        os.fchmod(stage.stream.fileno(), _PRIVATE_CREDENTIAL_MODE)
    secured_stat = os.fstat(stage.stream.fileno())
    if not os.path.samestat(stage.file_stat, secured_stat):
        raise OSError("private credential staging file changed while being secured")
    _require_private_credential_metadata(
        secured_stat,
        description="private credential staging file",
    )
    stage.file_stat = secured_stat


def _close_credential_stage_stream(stage: _CredentialStage) -> None:
    if stage.stream is None:
        return
    stream = stage.stream
    stage.stream = None
    try:
        stream.close()
    except BaseException as close_error:  # noqa: BLE001
        _append_cleanup_failure(
            close_error,
            "failed to close the private credential staging stream",
        )
        raise


def _merge_credential_stage_cleanup(
    primary: BaseException | None,
    cleanup: BaseException,
    message: str,
) -> BaseException:
    if primary is None:
        _append_cleanup_failure(cleanup, message)
        return cleanup
    return _merge_cleanup_error(primary, cleanup, message)


def _cleanup_credential_stage(
    stage: _CredentialStage,
    primary: BaseException | None = None,
) -> BaseException | None:
    error = primary
    if stage.stream is not None:
        try:
            _close_credential_stage_stream(stage)
        except BaseException as cleanup_error:  # noqa: BLE001
            error = _merge_credential_stage_cleanup(
                error,
                cleanup_error,
                "failed to close the private credential staging stream",
            )

    if stage.directory_stat is None:
        if isinstance(primary, FileExistsError):
            return error
        try:
            stage.directory.rmdir()
        except FileNotFoundError:
            pass
        except BaseException as cleanup_error:  # noqa: BLE001
            error = _merge_credential_stage_cleanup(
                error,
                cleanup_error,
                "failed to remove an unidentified credential staging directory",
            )
        return error

    try:
        directory_matches = _same_credential_stage_directory(stage)
    except BaseException as verification_error:  # noqa: BLE001
        error = _merge_credential_stage_cleanup(
            error,
            verification_error,
            "failed to verify the private credential staging directory",
        )
        return error
    if not directory_matches:
        try:
            stage.directory.lstat()
        except FileNotFoundError:
            return error
        except BaseException as verification_error:  # noqa: BLE001
            error = _merge_credential_stage_cleanup(
                error,
                verification_error,
                "failed to verify the private credential staging directory",
            )
        else:
            mismatch = OSError("private credential staging directory changed")
            error = _merge_credential_stage_cleanup(
                error,
                mismatch,
                "failed to verify the private credential staging directory",
            )
        return error

    try:
        if stage.file_stat is not None:
            _unlink_staged_file(stage.path, stage.file_stat)
        else:
            try:
                candidate = stage.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(candidate.st_mode):
                    raise OSError(
                        "private credential staging file has an unexpected type"
                    )
                stage.path.unlink()
    except BaseException as cleanup_error:  # noqa: BLE001
        error = _merge_credential_stage_cleanup(
            error,
            cleanup_error,
            "failed to remove the private credential staging file",
        )

    try:
        if _same_credential_stage_directory(stage):
            stage.directory.rmdir()
        elif stage.directory.exists() or stage.directory.is_symlink():
            raise OSError(  # noqa: TRY301 - converted to cleanup evidence below.
                "private credential staging directory changed"
            )
    except BaseException as cleanup_error:  # noqa: BLE001
        error = _merge_credential_stage_cleanup(
            error,
            cleanup_error,
            "failed to remove the private credential staging directory",
        )
    return error


def _same_staged_file(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(actual.st_mode) and os.path.samestat(expected, actual)


def _require_private_credential_metadata(
    metadata: os.stat_result,
    *,
    description: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"{description} has an unexpected type")
    if os.name == "nt":
        return
    if metadata.st_uid != os.geteuid():
        raise OSError(f"{description} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != _PRIVATE_CREDENTIAL_MODE:
        raise OSError(f"{description} permissions are not 0600")


def _unlink_staged_file(path: Path, expected: os.stat_result) -> None:
    """Remove a staging path only while it still names our staged inode."""

    if not _same_staged_file(path, expected):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _require_staged_output(path: Path, expected: os.stat_result | None) -> None:
    if expected is None:
        raise OSError("credential output changed before commit could be verified")
    try:
        actual = path.lstat()
    except FileNotFoundError as exc:
        raise OSError(
            "credential output changed before commit could be verified"
        ) from exc
    if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(expected, actual):
        raise OSError("credential output changed before commit could be verified")
    _require_private_credential_metadata(
        actual,
        description="credential output",
    )


def _secure_write_json(
    output_file: str,
    payload: dict[str, Any],
    force: bool,
    *,
    on_commit: Callable[[Path], None] | None = None,
) -> Path:
    output = _credential_output_path(output_file)
    parent_created = False
    try:
        output.parent.mkdir(parents=True)
        parent_created = True
    except FileExistsError:
        pass
    if parent_created and os.name != "nt":
        output.parent.chmod(_PRIVATE_DIRECTORY_MODE)
        parent_stat = output.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("credential output directory has an unexpected type")
        if parent_stat.st_uid != os.geteuid():
            raise OSError(
                "credential output directory is not owned by the current user"
            )
        if stat.S_IMODE(parent_stat.st_mode) != _PRIVATE_DIRECTORY_MODE:
            raise OSError("credential output directory permissions are not 0700")
    if output.is_symlink():
        raise ValueError(f"refusing to write login state through a symlink: {output}")
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")

    stage = _new_credential_stage(output)
    committed = False
    commit_notified = False
    publish_attempted = False
    reconciliation_failed = False

    def notify_commit() -> None:
        nonlocal commit_notified
        if commit_notified:
            return
        if on_commit is not None:
            on_commit(output)
        commit_notified = True

    def reconcile_interrupted_commit() -> bool:
        if committed:
            return True
        if not publish_attempted:
            return False
        if stage.file_stat is None:
            raise OSError("credential publish was attempted without file identity")
        try:
            output_stat = output.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(output_stat.st_mode) or not os.path.samestat(
            stage.file_stat, output_stat
        ):
            return False
        try:
            _require_private_credential_metadata(
                output_stat,
                description="credential output",
            )
        except OSError:
            _unlink_staged_file(output, stage.file_stat)
            return False
        return True

    try:
        _prepare_credential_stage(stage)
        stream = stage.stream
        if stream is None or stage.file_stat is None:
            raise OSError(  # noqa: TRY301 - outer block owns cleanup.
                "credential staging identity was not established"
            )
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        _close_credential_stage_stream(stage)
        _require_staged_output(stage.path, stage.file_stat)

        if force:
            publish_attempted = True
            os.replace(stage.path, output)
        else:
            try:
                publish_attempted = True
                os.link(stage.path, output)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"{output} already exists; pass --force to replace it"
                ) from exc
        _require_staged_output(output, stage.file_stat)
        committed = True
        notify_commit()
        cleanup_error = _cleanup_credential_stage(stage)
        if cleanup_error is not None:
            raise cleanup_error  # noqa: TRY301 - retain commit evidence below.
    except BaseException as primary_error:
        error_to_raise = primary_error
        try:
            if reconcile_interrupted_commit():
                committed = True
                notify_commit()
        except BaseException as reconciliation_error:  # noqa: BLE001
            reconciliation_failed = True
            message = "failed to determine credential publish status"
            error_to_raise = _merge_cleanup_error(
                error_to_raise,
                reconciliation_error,
                message,
            )
        cleaned_error = _cleanup_credential_stage(stage, error_to_raise)
        if cleaned_error is not None:
            error_to_raise = cleaned_error
        credential_state_status = (
            "candidate-saved"
            if committed
            else "not-established"
            if reconciliation_failed
            else "not-saved"
        )
        setattr(error_to_raise, "credential_state_status", credential_state_status)
        if committed:
            setattr(error_to_raise, "credential_output", output)
        if error_to_raise is primary_error:
            raise
        raise error_to_raise
    return output


def create_storage_state(
    cookie_string: str,
    output_file: str,
    *,
    force: bool = False,
    progress: CredentialWriteProgress | None = None,
) -> Path:
    storage_state = {
        "cookies": parse_cookie_string(cookie_string),
        "origins": [],
    }
    write_progress = progress if progress is not None else CredentialWriteProgress()
    try:
        output = _secure_write_json(
            output_file,
            storage_state,
            force,
            on_commit=write_progress.mark_committed,
        )
    except BaseException as exc:  # noqa: BLE001 - retain commit/cleanup evidence
        write_progress.observe_error(exc)
        raise
    write_progress.mark_committed(output)
    return output


def _write_evidence(progress: CredentialWriteProgress) -> dict[str, Any]:
    state: dict[str, Any] = {"status": progress.state_status}
    if progress.state_status == "candidate-saved" and progress.output is not None:
        state["output"] = str(progress.output)
    return {
        "state": state,
        "authentication": {"status": "not-established"},
        "identity": {"status": "not-evaluated"},
        "search_capability": {"status": "not-tested"},
        "cleanup": (
            {"status": "failed", "errors": list(progress.cleanup_failures)}
            if progress.cleanup_failures
            else {"status": "complete-or-not-required"}
        ),
    }


def _read_cookie_input(args: argparse.Namespace) -> str:
    if args.cookie_stdin:
        if sys.stdin.isatty():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    return getpass.getpass(
                        "Cookie header (input hidden): ", stream=sys.stderr
                    ).strip()
            except getpass.GetPassWarning as exc:
                raise ValueError(
                    "unable to disable terminal echo; pipe the Cookie header "
                    "through stdin or use a protected --cookie-file"
                ) from exc
            except EOFError as exc:
                raise ValueError("cookie input ended before a value was read") from exc
        return sys.stdin.read().strip()
    if args.cookie_file:
        return Path(args.cookie_file).expanduser().read_text(encoding="utf-8").strip()
    if args.cookie:
        print(
            "[warning] --cookie may expose credentials in shell history; "
            "prefer --cookie-stdin",
            file=sys.stderr,
        )
        return args.cookie
    raise ValueError("provide --cookie-stdin, --cookie-file, or --cookie")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Create secure Playwright login state")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cookie-stdin", action="store_true")
    source.add_argument("--cookie-file")
    source.add_argument("--cookie", "-c", help="legacy; visible to the process list")
    parser.add_argument("--output", "-o", default="state.json")
    parser.add_argument("--force", action="store_true")
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    progress = CredentialWriteProgress()
    try:
        cookie_string = _read_cookie_input(args)
        output = create_storage_state(
            cookie_string,
            args.output,
            force=args.force,
            progress=progress,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": str(output),
                    "cookies": len(parse_cookie_string(cookie_string)),
                    **_write_evidence(progress),
                },
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 0  # noqa: TRY300 - keep success emission inside cancellation boundary
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        progress.observe_error(exc)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "state creation cancelled",
                    "error_type": type(exc).__name__,
                    **_write_evidence(progress),
                },
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 130
    except (OSError, ValueError) as exc:
        progress.observe_error(exc)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    **_write_evidence(progress),
                },
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
