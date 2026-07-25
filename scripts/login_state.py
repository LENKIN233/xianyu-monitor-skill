#!/usr/bin/env python3
"""Save browser state only after interactive confirmation and a site marker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import selectors
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
    )
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
    )
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

if __package__:
    from .create_state import _credential_output_path, _secure_write_json
    from .spider import (
        BASE_URL,
        DependencyError,
        _filter_goofish_storage_state,
        _has_storage_state_material,
        _safe_page_location,
    )
else:
    from create_state import _credential_output_path, _secure_write_json
    from spider import (
        BASE_URL,
        DependencyError,
        _filter_goofish_storage_state,
        _has_storage_state_material,
        _safe_page_location,
    )


CONFIRMATION_PREFIX = "SAVE"
# Xianyu's current PC layout uses this response's nonempty displayName as one
# candidate session signal. We report only field presence: the raw value is not
# emitted or separately copied, and the resulting browser state remains secret.
SIGNED_IN_NAV_HOST = "h5api.m.goofish.com"
SIGNED_IN_NAV_PATH = "/h5/mtop.idle.web.user.page.nav/1.0/"
NON_IDENTITY_DISPLAY_NAMES = {"login", "sign in", "登录", "请登录", "未登录"}
BLOCKED_PAGE_MARKERS = ("captcha", "challenge", "login", "passport", "punish", "verify")


@dataclass
class CaptureProgress:
    confirmation_received: bool = False
    nav_display_name_present: bool = False
    state_saved: bool = False
    state_commit_status: str = "not-saved"
    saved_output: Path | None = None
    cleanup_failures: list[str] = field(default_factory=list)


class BrowserCleanupError(RuntimeError):
    """Raised when a dedicated browser resource cannot be closed."""


def _new_confirmation_token() -> str:
    return f"{CONFIRMATION_PREFIX}-{secrets.token_hex(2).upper()}"


def _is_interactive_stream(stream: TextIO | None) -> bool:
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _wait_for_windows_console_line(
    timeout_seconds: float,
    browser_closed: Callable[[], bool] | None,
    *,
    console: Any | None = None,
) -> str:
    if console is None:
        import msvcrt

        console = msvcrt

    deadline = time.monotonic() + timeout_seconds
    characters: list[str] = []
    while True:
        if browser_closed is not None and browser_closed():
            raise ValueError("browser closed before interactive confirmation")
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for interactive confirmation")
        if not console.kbhit():
            time.sleep(0.05)
            continue
        character = console.getwche()
        if character in {"\r", "\n"}:
            return "".join(characters)
        if character == "\x03":
            raise KeyboardInterrupt
        if character == "\x1a":
            return ""
        if character in {"\x00", "\xe0"}:
            if console.kbhit():
                console.getwch()
            continue
        if character == "\b":
            if characters:
                characters.pop()
            continue
        characters.append(character)


def _wait_for_terminal_line(
    input_stream: TextIO,
    timeout_seconds: float,
    browser_closed: Callable[[], bool] | None,
) -> str:
    """Read one TTY line without leaving a blocked background reader."""

    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        if input_stream is not sys.stdin:
            raise ValueError("unsupported interactive terminal input")
        return _wait_for_windows_console_line(timeout_seconds, browser_closed)

    try:
        descriptor = input_stream.fileno()
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("interactive terminal file descriptor unavailable") from exc

    selector = selectors.DefaultSelector()
    original_blocking: bool | None = None
    original_terminal_attributes: list[Any] | None = None
    termios_module: Any | None = None
    try:
        if os.isatty(descriptor):
            import termios

            termios_module = termios
            original_terminal_attributes = termios.tcgetattr(descriptor)
            controlled_attributes = termios.tcgetattr(descriptor)
            controlled_attributes[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
            controlled_attributes[6][termios.VMIN] = 1
            controlled_attributes[6][termios.VTIME] = 0
            termios.tcsetattr(descriptor, termios.TCSANOW, controlled_attributes)
        original_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        collected = bytearray()
        while True:
            if browser_closed is not None and browser_closed():
                raise ValueError("browser closed before interactive confirmation")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for interactive confirmation")
            if not selector.select(timeout=min(0.1, remaining)):
                continue
            try:
                chunk = os.read(descriptor, 256)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise ValueError("unable to read interactive confirmation") from exc
            if not chunk:
                break
            if b"\x03" in chunk:
                raise KeyboardInterrupt
            collected.extend(chunk)
            if len(collected) > 4_096:
                raise ValueError("interactive confirmation is too long")
            newline_positions = [
                position
                for delimiter in (b"\n", b"\r")
                if (position := collected.find(delimiter)) >= 0
            ]
            if newline_positions:
                collected = collected[: min(newline_positions) + 1]
                break
        encoding = getattr(input_stream, "encoding", None) or "utf-8"
        try:
            return bytes(collected).decode(encoding)
        except (LookupError, UnicodeError) as exc:
            raise ValueError("unable to decode interactive confirmation") from exc
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            selector.close()
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(
                ("failed to close the interactive terminal selector", exc)
            )
        if original_blocking is not None:
            try:
                os.set_blocking(descriptor, original_blocking)
            except BaseException as exc:  # noqa: BLE001
                cleanup_errors.append(
                    ("failed to restore the interactive terminal state", exc)
                )
        if original_terminal_attributes is not None and termios_module is not None:
            try:
                termios_module.tcsetattr(
                    descriptor,
                    termios_module.TCSANOW,
                    original_terminal_attributes,
                )
            except BaseException as exc:  # noqa: BLE001
                cleanup_errors.append(
                    ("failed to restore the interactive terminal state", exc)
                )
        if cleanup_errors:
            messages = list(dict.fromkeys(message for message, _ in cleanup_errors))
            cleanup_interruption = next(
                (
                    error
                    for _, error in cleanup_errors
                    if not isinstance(error, Exception)
                ),
                None,
            )
            if cleanup_interruption is not None and active_error is not None:
                existing = getattr(active_error, "cleanup_failures", None)
                if isinstance(existing, list):
                    transferred = getattr(
                        cleanup_interruption,
                        "cleanup_failures",
                        None,
                    )
                    if not isinstance(transferred, list):
                        transferred = []
                        setattr(
                            cleanup_interruption,
                            "cleanup_failures",
                            transferred,
                        )
                    for failure in existing:
                        if failure not in transferred:
                            transferred.append(failure)
            evidence_error = cleanup_interruption or active_error
            if evidence_error is not None:
                failures = getattr(evidence_error, "cleanup_failures", None)
                if not isinstance(failures, list):
                    failures = []
                    setattr(evidence_error, "cleanup_failures", failures)
                for message in messages:
                    if message not in failures:
                        failures.append(message)
            if cleanup_interruption is not None:
                raise cleanup_interruption
            if active_error is None:
                restore_failure = ValueError("; ".join(messages))
                restore_failure.cleanup_failures = messages
                raise restore_failure from cleanup_errors[0][1]


def _read_explicit_confirmation(
    input_stream: TextIO,
    error_stream: TextIO,
    token: str,
    timeout_seconds: float,
    *,
    browser_closed: Callable[[], bool] | None = None,
    line_reader: Callable[
        [TextIO, float, Callable[[], bool] | None], str
    ] = _wait_for_terminal_line,
) -> None:
    """Require one exact token from an interactive terminal."""

    if timeout_seconds <= 0:
        raise TimeoutError("timed out before interactive confirmation")
    if not _is_interactive_stream(input_stream):
        raise ValueError(
            "interactive terminal required; the user must confirm personally"
        )

    print(
        "After you personally verify the intended account is visible in the browser, "
        f"type {token} and press Enter. Agents must not enter this token.",
        file=error_stream,
        flush=True,
    )

    line = line_reader(input_stream, timeout_seconds, browser_closed)
    if not line:
        raise ValueError("confirmation input ended before a value was read")
    if line.strip() != token:
        raise ValueError("confirmation did not match; browser state was not saved")


def _validate_confirmation_page(url: str) -> None:
    """Reject login and challenge pages without treating a URL as identity proof."""

    parsed = urlsplit(url)
    path = parsed.path.lower()
    challenge_markers = "\n".join((path, parsed.query.lower(), parsed.fragment.lower()))
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "www.goofish.com"
        or any(marker in challenge_markers for marker in BLOCKED_PAGE_MARKERS)
    ):
        raise ValueError(
            f"confirmation page is not a normal Xianyu page: {_safe_page_location(url)}"
        )


def _has_nav_display_name(payload: Any) -> bool:
    """Check the PC navigation display-name field without returning its value."""

    if not isinstance(payload, dict):
        return False
    ret = payload.get("ret")
    ret_values = ret if isinstance(ret, list) else [ret]
    if not ret_values or any(
        not isinstance(value, str) or not value.upper().startswith("SUCCESS::")
        for value in ret_values
    ):
        return False
    data = payload.get("data")
    module = data.get("module") if isinstance(data, dict) else None
    base = module.get("base") if isinstance(module, dict) else None
    display_name = base.get("displayName") if isinstance(base, dict) else None
    if not isinstance(display_name, str):
        return False
    normalized_name = display_name.strip()
    return bool(normalized_name) and (
        normalized_name.casefold() not in NON_IDENTITY_DISPLAY_NAMES
    )


def _is_nav_response_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == SIGNED_IN_NAV_HOST
        and f"{parsed.path.rstrip('/')}/".lower() == SIGNED_IN_NAV_PATH
    )


def _observe_nav_display_name(page: Any) -> threading.Event:
    observed = threading.Event()

    def inspect_response(response: Any) -> None:
        try:
            if not _is_nav_response_url(str(response.url)):
                return
            payload = response.json()
        # Playwright may raise a plain Exception anywhere in a synchronous
        # response callback when the driver disconnects.
        except Exception:  # noqa: BLE001
            return
        if _has_nav_display_name(payload):
            observed.set()

    page.on("response", inspect_response)
    return observed


def _require_nav_display_name(
    page: Any,
    observed: threading.Event,
    timeout_seconds: float,
) -> None:
    """Observe the current PC navigation display-name field, failing closed."""

    if timeout_seconds <= 0:
        raise TimeoutError("timed out before session-candidate validation")
    try:
        page.goto(
            BASE_URL,
            wait_until="domcontentloaded",
            timeout=max(1, min(30_000, int(timeout_seconds * 1000))),
        )
    except PlaywrightTimeoutError as exc:
        raise TimeoutError("timed out validating the session candidate") from exc
    _validate_confirmation_page(page.url)

    deadline = time.monotonic() + min(timeout_seconds, 15)
    while not observed.is_set() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    if not observed.is_set():
        raise ValueError(
            "Xianyu navigation display name was not observed; "
            "browser state was not saved"
        )


def _cleanup_preserving_primary_error(
    action: Callable[[], None],
    error_message: str,
    progress: CaptureProgress,
) -> None:
    """Run cleanup without masking an active exception."""

    primary_error_active = sys.exc_info()[0] is not None
    try:
        action()
    except BaseException as exc:  # noqa: BLE001
        if error_message not in progress.cleanup_failures:
            progress.cleanup_failures.append(error_message)
        if not isinstance(exc, Exception):
            raise
        if not primary_error_active:
            raise BrowserCleanupError(error_message) from exc


def _cleanup_interrupted_playwright_start(
    manager: Any,
    error: BaseException,
    progress: CaptureProgress,
) -> None:
    """Stop a partially initialized sync Playwright manager."""

    message = "failed to stop the partially started browser runtime"
    exit_action = getattr(manager, "__exit__", None)
    if not callable(exit_action):
        if message not in progress.cleanup_failures:
            progress.cleanup_failures.append(message)
        return
    try:
        exit_action(type(error), error, error.__traceback__)
    except BaseException as cleanup_error:  # noqa: BLE001
        if message not in progress.cleanup_failures:
            progress.cleanup_failures.append(message)
        if not isinstance(cleanup_error, Exception):
            raise


def capture_login_state(
    output_file: str,
    *,
    browser_channel: str | None,
    timeout_seconds: int,
    force: bool,
    input_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    token_factory: Callable[[], str] = _new_confirmation_token,
    progress: CaptureProgress | None = None,
) -> Path:
    if sync_playwright is None:
        raise DependencyError(
            "playwright is not installed; run: "
            "python -m pip install -r requirements.txt"
        )
    if timeout_seconds < 1:
        raise ValueError("timeout must be at least 1 second")
    confirmation_input = input_stream if input_stream is not None else sys.stdin
    confirmation_errors = error_stream if error_stream is not None else sys.stderr
    capture_progress = progress if progress is not None else CaptureProgress()
    if not _is_interactive_stream(confirmation_input):
        raise ValueError(
            "interactive terminal required; the user must confirm personally"
        )
    output = _credential_output_path(output_file)
    if output.is_symlink():
        raise ValueError(f"refusing to write login state through a symlink: {output}")
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")

    playwright_manager = sync_playwright()
    playwright: Any | None = None
    browser: Any | None = None
    try:
        try:
            playwright = playwright_manager.start()
        except BaseException as exc:  # noqa: BLE001
            # start() can initialize the manager before raising (including an
            # interruption delivered before its return value is assigned).
            _cleanup_interrupted_playwright_start(
                playwright_manager,
                exc,
                capture_progress,
            )
            raise

        launch_kwargs: dict[str, Any] = {"headless": False}
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        browser = playwright.chromium.launch(**launch_kwargs)

        if browser is not None:
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(
                    BASE_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except PlaywrightTimeoutError as exc:
                raise TimeoutError("timed out opening Xianyu login page") from exc

            deadline = time.monotonic() + timeout_seconds
            try:
                _read_explicit_confirmation(
                    confirmation_input,
                    confirmation_errors,
                    token_factory(),
                    deadline - time.monotonic(),
                    browser_closed=page.is_closed,
                )
            except BaseException as exc:  # noqa: BLE001
                for failure in getattr(exc, "cleanup_failures", []):
                    if failure not in capture_progress.cleanup_failures:
                        capture_progress.cleanup_failures.append(failure)
                raise
            capture_progress.confirmation_received = True
            _validate_confirmation_page(page.url)
            # Attach only after explicit confirmation so the identity-bearing
            # response is not read before consent and cannot be a stale signal
            # from the initial anonymous/login page load.
            validation_page = context.new_page()
            try:
                nav_display_name = _observe_nav_display_name(validation_page)
                _require_nav_display_name(
                    validation_page,
                    nav_display_name,
                    deadline - time.monotonic(),
                )
                capture_progress.nav_display_name_present = True
            finally:
                _cleanup_preserving_primary_error(
                    validation_page.close,
                    "failed to close the validation page",
                    capture_progress,
                )
            try:
                raw_state = context.storage_state(indexed_db=True)
            except TypeError as exc:
                raise DependencyError(
                    "playwright 1.51 or newer is required to capture IndexedDB state"
                ) from exc
            state = _filter_goofish_storage_state(raw_state)
            if not _has_storage_state_material(state):
                raise ValueError(
                    "browser has no retained Goofish storage material; "
                    "browser state was not saved"
                )

            def mark_committed_state(path: Path) -> None:
                # The writer retries this idempotent callback after reconciling
                # an interrupted atomic publish.
                capture_progress.saved_output = path
                capture_progress.state_saved = True
                capture_progress.state_commit_status = "candidate-saved"

            try:
                saved_output = _secure_write_json(
                    str(output),
                    state,
                    force,
                    on_commit=mark_committed_state,
                )
            except BaseException as exc:  # noqa: BLE001
                commit_status = getattr(exc, "credential_state_status", None)
                if commit_status in {
                    "candidate-saved",
                    "not-established",
                    "not-saved",
                }:
                    capture_progress.state_commit_status = commit_status
                if commit_status == "candidate-saved":
                    capture_progress.state_saved = True
                    committed_output = getattr(exc, "credential_output", None)
                    capture_progress.saved_output = (
                        committed_output
                        if isinstance(committed_output, Path)
                        else output
                    )
                for failure in getattr(exc, "cleanup_failures", []):
                    if failure not in capture_progress.cleanup_failures:
                        capture_progress.cleanup_failures.append(failure)
                raise
            mark_committed_state(saved_output)
            return saved_output
    finally:
        try:
            if browser is not None:
                _cleanup_preserving_primary_error(
                    browser.close,
                    "failed to close the dedicated browser",
                    capture_progress,
                )
        finally:
            if playwright is not None:
                _cleanup_preserving_primary_error(
                    playwright.stop,
                    "failed to stop the dedicated browser runtime",
                    capture_progress,
                )


def _capture_evidence(progress: CaptureProgress) -> dict[str, Any]:
    state: dict[str, Any]
    if progress.state_saved:
        state = {"status": "candidate-saved"}
        if progress.saved_output is not None:
            state["output"] = str(progress.saved_output)
    elif progress.state_commit_status == "not-established":
        state = {"status": "not-established"}
    else:
        state = {"status": "not-saved"}
    return {
        "state": state,
        "confirmation": {
            "status": (
                "interactive-token-received"
                if progress.confirmation_received
                else "not-received"
            ),
            "actor": (
                "not-machine-verified"
                if progress.confirmation_received
                else "not-established"
            ),
        },
        "session": {
            "nav_display_name": (
                "present" if progress.nav_display_name_present else "not-established"
            )
        },
        "identity": {
            "status": (
                "not-machine-verified"
                if progress.nav_display_name_present
                else "not-established"
            )
        },
        "authentication": {"status": "not-established"},
        "search_capability": {"status": "not-tested"},
        "cleanup": (
            {
                "status": "failed",
                "errors": list(progress.cleanup_failures),
            }
            if progress.cleanup_failures
            else {"status": "complete-or-not-required"}
        ),
    }


def _require_complete_capture(progress: CaptureProgress) -> None:
    if not (
        progress.confirmation_received
        and progress.nav_display_name_present
        and progress.state_saved
    ):
        raise ValueError("capture completed without the required evidence")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open a visible Xianyu browser and save candidate state "
            "after interactive confirmation"
        )
    )
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument(
        "--browser-channel",
        default=os.getenv("XIANYU_BROWSER_CHANNEL"),
        help="Playwright browser channel, for example chrome",
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="login timeout seconds"
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    progress = CaptureProgress()
    try:
        if not _is_interactive_stream(sys.stdin):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "interactive terminal required; "
                            "the user must confirm personally"
                        ),
                        **_capture_evidence(progress),
                    },
                    ensure_ascii=True,
                )
            )
            return 2  # noqa: TRY300 - TTY handling shares the cancellation boundary.
        print(
            json.dumps(
                {
                    "status": "browser-opening",
                    "site": "goofish.com",
                    "output": str(Path(args.output).expanduser()),
                    "requires_user_confirmation": True,
                },
                ensure_ascii=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        capture_login_state(
            args.output,
            browser_channel=args.browser_channel,
            timeout_seconds=args.timeout,
            force=args.force,
            progress=progress,
        )
        _require_complete_capture(progress)
        print(
            json.dumps(
                {
                    "ok": True,
                    **_capture_evidence(progress),
                },
                ensure_ascii=True,
            )
        )
        return 0  # noqa: TRY300 - success emission must stay cancellation-protected.
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "capture cancelled",
                    "error_type": type(exc).__name__,
                    **_capture_evidence(progress),
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (
        DependencyError,
        OSError,
        TimeoutError,
        ValueError,
        PlaywrightError,
        BrowserCleanupError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    **_capture_evidence(progress),
                },
                ensure_ascii=True,
            )
        )
        return 2
    except Exception:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "unexpected capture failure",
                    **_capture_evidence(progress),
                },
                ensure_ascii=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
