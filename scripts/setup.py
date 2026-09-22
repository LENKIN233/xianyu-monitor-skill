#!/usr/bin/env python3
"""Compose the safe first-run workflow without weakening confirmation gates."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

if __package__:
    from . import doctor, state_check
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    import doctor
    import state_check
    from cli_contract import JsonArgumentParser, sigterm_cancellable

SETUP_SCHEMA = 1
MAX_CHILD_JSON_BYTES = 4 * 1024 * 1024


class SetupArgumentParser(JsonArgumentParser):
    """Keep parse failures machine-readable without reflecting private values."""

    def error(self, _message: str) -> None:
        super().error("invalid setup arguments; run --help")


class SetupContractError(ValueError):
    """A composed command did not honor its public JSON contract."""


class SetupChildCancelled(KeyboardInterrupt):
    """The parent cancelled an owned child and retained any final evidence."""

    def __init__(self, result: CommandResult | None = None):
        super().__init__("setup child cancelled")
        self.result = result


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    payload: dict[str, Any]


@dataclass
class SetupProgress:
    """Path-private evidence retained across cancellation boundaries."""

    report: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        self.report = {
            "ok": False,
            "schema_version": SETUP_SCHEMA,
            "phases": {
                "doctor": {"status": "not-run"},
                "login": {"status": "not-run"},
                "state": {"status": "not-run"},
                "capability": {"status": "not-run"},
            },
            "cleanup": {"status": "complete-or-not-required"},
        }


CommandRunner = Callable[[list[str]], CommandResult]


def _absolute_state_path(value: str) -> Path:
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError("--state must be an absolute path") from exc
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--state must be an absolute path")
    return path


def _search_keyword(value: str) -> str:
    keyword = value.strip()
    if not keyword or len(keyword) > 200:
        raise argparse.ArgumentTypeError("--keyword must contain 1 to 200 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in keyword):
        raise argparse.ArgumentTypeError("--keyword contains unsupported controls")
    return keyword


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _parse_child_payload(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_CHILD_JSON_BYTES:
        raise SetupContractError("composed command output exceeded the safety limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise SetupContractError("composed command returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise SetupContractError("composed command returned an invalid JSON contract")
    return payload


def _run_child(arguments: list[str]) -> CommandResult:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(  # noqa: S603
        arguments,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=None,
        env=environment,
    )
    try:
        stdout, _stderr = process.communicate()
    except BaseException as primary_error:  # noqa: BLE001 - stop owned child safely
        try:
            process.terminate()
        except OSError:
            pass
        try:
            stdout, _stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _stderr = process.communicate()
        retained: CommandResult | None = None
        try:
            retained = CommandResult(
                returncode=process.returncode,
                payload=_parse_child_payload(stdout),
            )
        except SetupContractError:
            retained = None
        if isinstance(primary_error, (KeyboardInterrupt, asyncio.CancelledError)):
            raise SetupChildCancelled(retained) from primary_error
        raise
    return CommandResult(
        returncode=process.returncode,
        payload=_parse_child_payload(stdout),
    )


def _safe_evidence(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload}


def _resolve_browser_channel(
    requested: str | None,
    doctor_payload: dict[str, Any],
) -> str | None:
    if requested:
        return requested
    next_action = doctor_payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("code") == (
        "ready-use-browser-channel"
    ):
        return "chrome"
    return None


def _next_action(code: str, hint: str) -> dict[str, str]:
    return {"code": code, "hint": hint}


def _login_command(args: argparse.Namespace, channel: str | None) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("login_state.py")),
        "--output",
        str(args.state),
        "--timeout",
        str(args.timeout),
        "--confirm-in-browser",
    ]
    if channel:
        command.extend(["--browser-channel", channel])
    return command


def _search_command(args: argparse.Namespace, channel: str | None) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("spider.py")),
        "--keyword",
        args.keyword,
        "--pages",
        "1",
        "--retries",
        "1",
        "--state",
        str(args.state),
        "--quiet",
    ]
    if channel:
        command.extend(["--browser-channel", channel])
    if args.headed_capability_test:
        command.append("--headed")
    return command


def _record_login_result(
    report: dict[str, Any],
    result: CommandResult,
) -> None:
    payload = result.payload
    report["phases"]["login"] = {
        "status": (
            "candidate-saved"
            if result.returncode == 0
            and payload.get("state", {}).get("status") == "candidate-saved"
            else "cancelled"
            if result.returncode == 130
            else "failed"
        ),
        **_safe_evidence(
            payload,
            "state",
            "confirmation",
            "session",
            "authentication",
            "identity",
            "search_capability",
            "cleanup",
        ),
    }
    if isinstance(payload.get("cleanup"), dict):
        report["cleanup"] = payload["cleanup"]


def _validate_search_success(result: CommandResult) -> bool:
    payload = result.payload
    items = payload.get("items")
    count = payload.get("count")
    capability = payload.get("search_capability")
    return (
        result.returncode == 0
        and payload.get("ok") is True
        and isinstance(items, list)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(items)
        and payload.get("pages_scraped") == 1
        and isinstance(capability, dict)
        and capability.get("status") == "passed-for-this-run"
    )


def run_setup(
    args: argparse.Namespace,
    *,
    runner: CommandRunner = _run_child,
    progress: SetupProgress | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the bounded workflow and return one final path-private document."""

    setup_progress = progress if progress is not None else SetupProgress()
    setup_progress.reset()
    report = setup_progress.report

    doctor_payload = doctor.run_doctor(
        state_output_dir=(
            args.state.parent
            if args.capture_state and not _path_present(args.state)
            else None
        )
    )
    report["phases"]["doctor"] = {
        "status": "passed" if doctor_payload.get("ok") is True else "failed",
        "checks": doctor_payload.get("checks", []),
        "next_action": doctor_payload.get("next_action"),
    }
    if doctor_payload.get("ok") is not True:
        report["next_action"] = doctor_payload.get("next_action") or _next_action(
            "rerun-doctor",
            "Resolve the reported prerequisite and rerun setup.",
        )
        return 2, report

    channel = _resolve_browser_channel(args.browser_channel, doctor_payload)
    report["browser"] = {
        "selection": channel or "playwright-default",
        "profile_reuse": False,
    }

    if not _path_present(args.state):
        if not args.capture_state:
            report["phases"]["login"] = {"status": "handoff-required"}
            report["next_action"] = _next_action(
                "rerun-setup-with-capture-state",
                (
                    "Rerun setup with --capture-state; the user must complete the "
                    "visible login and local browser confirmation personally."
                ),
            )
            return 2, report

        report["phases"]["login"] = {"status": "not-established"}
        try:
            login_result = runner(_login_command(args, channel))
        except SetupChildCancelled as exc:
            if exc.result is not None:
                _record_login_result(report, exc.result)
            report["phases"]["login"]["status"] = "not-established"
            report["cleanup"] = {"status": "not-established"}
            raise
        _record_login_result(report, login_result)
        if login_result.returncode == 130:
            report["exit_reason"] = "cancelled"
            report["next_action"] = _next_action(
                "inspect-candidate-before-retry",
                (
                    "Treat any not-established candidate as private; inspect the "
                    "reported evidence before rerunning setup."
                ),
            )
            return 130, report
        if report["phases"]["login"]["status"] != "candidate-saved":
            report["next_action"] = _next_action(
                "complete-visible-login",
                "Complete login and browser confirmation, then rerun setup.",
            )
            return 2, report
    else:
        report["phases"]["login"] = {"status": "skipped-existing-candidate"}

    state_payload = state_check.inspect_state(args.state)
    report["phases"]["state"] = {
        "status": state_payload.get("state", {}).get("status", "not-established"),
        **_safe_evidence(
            state_payload,
            "privacy",
            "authentication",
            "identity",
            "search_capability",
            "cleanup",
        ),
    }
    if state_payload.get("ok") is not True:
        report["next_action"] = state_payload.get("next_action") or _next_action(
            "repair-state-candidate",
            "Repair or recapture the private candidate, then rerun setup.",
        )
        return 2, report

    report["phases"]["capability"] = {"status": "not-established"}
    try:
        search_result = runner(_search_command(args, channel))
    except SetupChildCancelled as exc:
        if exc.result is not None:
            retained_payload = exc.result.payload
            report["phases"]["capability"].update(
                _safe_evidence(
                    retained_payload,
                    "count",
                    "pages_scraped",
                    "search_capability",
                    "authentication",
                    "identity",
                    "cleanup",
                )
            )
        report["phases"]["capability"]["status"] = "not-established"
        report["cleanup"] = {"status": "not-established"}
        raise
    search_payload = search_result.payload
    capability_status = "failed"
    if search_result.returncode == 130:
        capability_status = "cancelled"
    elif _validate_search_success(search_result):
        capability_status = "passed-for-this-run"
    report["phases"]["capability"] = {
        "status": capability_status,
        **_safe_evidence(
            search_payload,
            "count",
            "pages_scraped",
            "search_capability",
            "authentication",
            "identity",
            "cleanup",
        ),
    }
    if isinstance(search_payload.get("cleanup"), dict):
        report["cleanup"] = search_payload["cleanup"]
    if search_result.returncode == 130:
        report["exit_reason"] = "cancelled"
        report["next_action"] = _next_action(
            "rerun-capability-test",
            "Rerun setup when ready; the existing candidate was not removed.",
        )
        return 130, report
    if capability_status != "passed-for-this-run":
        report["next_action"] = _next_action(
            "inspect-search-failure",
            (
                "Inspect the search failure; stop on login, CAPTCHA, risk-control, "
                "or rejection evidence."
            ),
        )
        return 2, report

    report["ok"] = True
    report["next_action"] = _next_action(
        "ready-create-task",
        "Search capability passed for this run; create a task only if requested.",
    )
    return 0, report


def build_parser() -> argparse.ArgumentParser:
    parser = SetupArgumentParser(
        description=(
            "Guide doctor, optional visible login, local state validation, and one "
            "bounded capability search"
        )
    )
    parser.add_argument("--state", required=True, type=_absolute_state_path)
    parser.add_argument("--keyword", required=True, type=_search_keyword)
    parser.add_argument(
        "--capture-state",
        action="store_true",
        help=(
            "when the state path is absent, open the visible login and require "
            "local in-browser user confirmation"
        ),
    )
    parser.add_argument(
        "--browser-channel",
        help="browser executable channel, for example chrome; never reuses a profile",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1_800,
        choices=range(1, 7_201),
        metavar="SECONDS",
        help="visible login timeout from 1 to 7200 seconds (default: 1800)",
    )
    parser.add_argument(
        "--headed-capability-test",
        action="store_true",
        help="show the single capability-test browser; does not add retries",
    )
    return parser


@sigterm_cancellable
def main(argv: Sequence[str] | None = None) -> int:
    progress = SetupProgress()
    try:
        args = build_parser().parse_args(argv)
        return_code, payload = run_setup(args, progress=progress)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        payload = progress.report or {
            "ok": False,
            "schema_version": SETUP_SCHEMA,
            "phases": {},
            "cleanup": {"status": "not-established"},
        }
        payload.update(
            ok=False,
            exit_reason="cancelled",
            error="setup cancelled",
            error_type=type(exc).__name__,
        )
        phases = payload.get("phases")
        if isinstance(phases, dict):
            for phase in phases.values():
                if isinstance(phase, dict) and phase.get("status") == "not-established":
                    payload["cleanup"] = {"status": "not-established"}
                    break
        return_code = 130
    except (OSError, SetupContractError, ValueError) as exc:
        payload = progress.report or {
            "ok": False,
            "schema_version": SETUP_SCHEMA,
            "phases": {},
            "cleanup": {"status": "complete-or-not-required"},
        }
        payload.update(
            ok=False,
            error="setup orchestration failed",
            error_type=type(exc).__name__,
            next_action=_next_action(
                "rerun-setup",
                "Inspect local prerequisites and rerun setup.",
            ),
        )
        return_code = 2
    print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
