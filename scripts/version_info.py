#!/usr/bin/env python3
"""Emit the installed Skill version and stable capability discovery JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

SKILL_NAME = "xianyu-monitor"
MINIMUM_PYTHON = "3.10"
VERSION_SCHEMA = 1
MAX_VERSION_BYTES = 128
SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z",
    re.ASCII,
)

CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "analyze",
        "command": "analyze",
        "network": "consent-gated",
        "credentials": "provider-key-environment-only",
    },
    {
        "id": "deliver",
        "command": "deliver",
        "network": "digest-and-send-gated",
        "credentials": "endpoint-environment-only",
    },
    {
        "id": "demo",
        "command": "demo",
        "network": "none",
        "credentials": "none",
    },
    {
        "id": "doctor",
        "command": "doctor",
        "network": "none",
        "credentials": "none",
    },
    {
        "id": "evaluate",
        "command": "evaluate",
        "network": "none",
        "credentials": "none",
    },
    {
        "id": "install",
        "command": "install",
        "network": "none",
        "credentials": "none",
    },
    {
        "id": "login",
        "command": "login",
        "network": "required",
        "credentials": "writes-private-state-after-user-confirmation",
    },
    {
        "id": "monitor",
        "command": "monitor",
        "network": "required",
        "credentials": "authorized-state-path",
    },
    {
        "id": "search",
        "command": "search",
        "network": "required",
        "credentials": "authorized-state-path-optional",
    },
    {
        "id": "setup",
        "command": "setup",
        "network": "required-after-local-preflight",
        "credentials": "authorized-state-path",
    },
    {
        "id": "state",
        "command": "state",
        "network": "none",
        "credentials": "authorized-state-path",
    },
    {
        "id": "task",
        "command": "task",
        "network": "none",
        "credentials": "stores-state-path-reference-only",
    },
    {
        "id": "version",
        "command": "version",
        "network": "none",
        "credentials": "none",
    },
)


def read_version(root: Path | None = None) -> str:
    """Read and validate the bounded single-source version file."""

    skill_root = Path(__file__).resolve().parents[1] if root is None else root
    version_file = skill_root / "VERSION"
    try:
        with version_file.open("rb") as stream:
            raw = stream.read(MAX_VERSION_BYTES + 1)
    except OSError as exc:
        raise ValueError("VERSION is missing or unreadable") from exc
    if len(raw) > MAX_VERSION_BYTES:
        raise ValueError("VERSION exceeds the safety limit")
    try:
        version = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("VERSION must be ASCII SemVer") from exc
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError("VERSION must contain one valid Semantic Version")
    return version


def capability_payload(*, root: Path | None = None) -> dict[str, Any]:
    """Return the stable discovery document used by hosts and diagnostics."""

    return {
        "ok": True,
        "schema_version": VERSION_SCHEMA,
        "skill": {
            "name": SKILL_NAME,
            "version": read_version(root),
        },
        "runtime": {
            "minimum_python": MINIMUM_PYTHON,
            "detected_python": ".".join(str(part) for part in sys.version_info[:3]),
        },
        "capabilities": [dict(item, status="available") for item in CAPABILITIES],
        "contracts": {
            "task_schema": 3,
            "task_transfer_schema": 1,
            "outbox_schema": 1,
            "delivery_schema": 1,
            "ai_run_evidence_schema": 1,
            "ai_evaluation_schema": 1,
            "ai_feedback_schema": 1,
            "demo_schema": 1,
            "version_schema": VERSION_SCHEMA,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Emit xianyu-monitor version and capability discovery JSON"
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="print only the Semantic Version string",
    )
    return parser


@sigterm_cancellable
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = capability_payload()
    except ValueError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=True,
            )
        )
        return 2
    if args.short:
        print(payload["skill"]["version"])
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
