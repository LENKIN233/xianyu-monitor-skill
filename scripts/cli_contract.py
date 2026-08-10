#!/usr/bin/env python3
"""Shared machine-readable CLI and process-lifecycle helpers."""

from __future__ import annotations

import argparse
import functools
import json
import signal
import threading
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

RAW_CDP_DISABLED_MESSAGE = (
    "raw TCP CDP is disabled because Chrome does not authenticate local clients; "
    "run the complete command on the browser-owning host with --browser-channel"
)


class JsonArgumentParser(argparse.ArgumentParser):
    """Emit argument-validation failures through the public JSON channel."""

    def error(self, message: str) -> None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "error_type": "ArgumentError",
                },
                ensure_ascii=True,
            )
        )
        raise SystemExit(2)


def reject_raw_cdp_path(_value: str) -> str:
    """Reject the legacy unauthenticated transport during argument parsing."""

    raise argparse.ArgumentTypeError(RAW_CDP_DISABLED_MESSAGE)


class SigtermCancellation(KeyboardInterrupt):
    """Translate a scheduler SIGTERM into the existing cancellation contract."""


def _raise_sigterm_cancellation(_signum: int, _frame: Any) -> None:
    raise SigtermCancellation


def sigterm_cancellable(function: Callable[P, R]) -> Callable[P, R]:
    """Route SIGTERM through ``KeyboardInterrupt`` cleanup on the main thread."""

    @functools.wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        sigterm = getattr(signal, "SIGTERM", None)
        if sigterm is None or threading.current_thread() is not threading.main_thread():
            return function(*args, **kwargs)

        previous = signal.getsignal(sigterm)
        if previous not in {signal.SIG_DFL, _raise_sigterm_cancellation}:
            return function(*args, **kwargs)

        signal.signal(sigterm, _raise_sigterm_cancellation)
        try:
            try:
                return function(*args, **kwargs)
            except SigtermCancellation:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "operation cancelled by SIGTERM",
                            "error_type": "SigtermCancellation",
                            "cleanup": {"status": "complete-or-not-required"},
                        },
                        ensure_ascii=True,
                    )
                )
                return 130  # type: ignore[return-value]
        finally:
            signal.signal(sigterm, previous)

    return wrapped
