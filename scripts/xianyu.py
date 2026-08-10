#!/usr/bin/env python3
"""Unified, backwards-compatible command dispatcher for xianyu-monitor."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Sequence

# ``xianyu doctor`` promises a read-only preflight. Keep that promise when the
# doctor is reached through this dispatcher as well as through doctor.py.
sys.dont_write_bytecode = True

if __package__:
    from .cli_contract import sigterm_cancellable
else:
    from cli_contract import sigterm_cancellable

COMMANDS = {
    "doctor": ("doctor", "check Python, dependencies, browser, and private dirs"),
    "login": ("login_state", "open a dedicated browser and save candidate state"),
    "search": ("spider", "run one Xianyu search and emit JSON"),
    "task": ("task_manager", "create, list, stop, resume, or delete monitor tasks"),
    "monitor": ("monitor", "run persistent monitor tasks and emit new listings"),
    "install": ("install_skill", "install this skill for supported agent hosts"),
}


def _help_text() -> str:
    command_lines = "\n".join(
        f"  {name:<8} {description}" for name, (_, description) in COMMANDS.items()
    )
    return (
        "usage: xianyu.py <command> [arguments]\n\n"
        "Unified CLI for the xianyu-monitor Agent Skill.\n\n"
        f"commands:\n{command_lines}\n\n"
        "Run 'xianyu.py <command> --help' for command-specific options."
    )


def _emit_argument_error(message: str) -> int:
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
    return 2


def _load_entrypoint(module_name: str) -> Callable[[list[str] | None], int]:
    qualified_name = f"{__package__}.{module_name}" if __package__ else module_name
    module = importlib.import_module(qualified_name)
    entrypoint = getattr(module, "main")
    if not callable(entrypoint):
        raise TypeError(f"{qualified_name}.main is not callable")
    return entrypoint


@sigterm_cancellable
def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(_help_text())
        return 0

    if arguments[0] == "help":
        if len(arguments) == 1:
            print(_help_text())
            return 0
        if len(arguments) == 2 and arguments[1] in COMMANDS:
            arguments = [arguments[1], "--help"]
        else:
            return _emit_argument_error("help accepts exactly one known command")

    command = arguments[0]
    command_spec = COMMANDS.get(command)
    if command_spec is None:
        return _emit_argument_error("unknown command; run --help to list commands")

    module_name, _description = command_spec
    entrypoint = _load_entrypoint(module_name)
    original_argv0 = sys.argv[0]
    sys.argv[0] = f"{original_argv0} {command}"
    try:
        return entrypoint(arguments[1:])
    finally:
        sys.argv[0] = original_argv0


if __name__ == "__main__":
    raise SystemExit(main())
