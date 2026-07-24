#!/usr/bin/env python3
"""Create a Playwright login-state file with restrictive permissions."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any


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


def _secure_write_json(output_file: str, payload: dict[str, Any], force: bool) -> Path:
    output = _credential_output_path(output_file)
    parent_created = False
    try:
        output.parent.mkdir(parents=True)
        parent_created = True
    except FileExistsError:
        pass
    if parent_created:
        try:
            output.parent.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
    if output.is_symlink():
        raise ValueError(f"refusing to write login state through a symlink: {output}")
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
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
        if force:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"{output} already exists; pass --force to replace it"
                ) from exc
            temporary.unlink()
        try:
            output.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def create_storage_state(
    cookie_string: str, output_file: str, *, force: bool = False
) -> Path:
    storage_state = {
        "cookies": parse_cookie_string(cookie_string),
        "origins": [],
    }
    return _secure_write_json(output_file, storage_state, force)


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
    parser = argparse.ArgumentParser(description="Create secure Playwright login state")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cookie-stdin", action="store_true")
    source.add_argument("--cookie-file")
    source.add_argument("--cookie", "-c", help="legacy; visible to the process list")
    parser.add_argument("--output", "-o", default="state.json")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cookie_string = _read_cookie_input(args)
        output = create_storage_state(cookie_string, args.output, force=args.force)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "cookies": len(parse_cookie_string(cookie_string)),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
