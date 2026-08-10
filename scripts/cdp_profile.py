#!/usr/bin/env python3
"""Safely remove a legacy dedicated Xianyu CDP profile."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import stat
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

if __package__:
    from .spider import (
        _cdp_endpoint_from_user_data_dir,
        _private_cdp_profile_path,
    )
else:
    from spider import (
        _cdp_endpoint_from_user_data_dir,
        _private_cdp_profile_path,
    )


def _same_profile_directory(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(actual.st_mode) and os.path.samestat(expected, actual)


def _require_same_profile_directory(
    path: Path,
    expected: os.stat_result,
    message: str,
) -> None:
    if not _same_profile_directory(path, expected):
        raise OSError(message)


def _require_profile_stopped(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        indicator = profile / name
        if indicator.exists() or indicator.is_symlink():
            raise ValueError("dedicated Chrome still appears active; close it first")

    marker = profile / "DevToolsActivePort"
    if not marker.exists() and not marker.is_symlink():
        return
    endpoint = _cdp_endpoint_from_user_data_dir(str(profile), timeout_seconds=0)
    parsed = urlsplit(endpoint)
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid dedicated Chrome endpoint")
    try:
        connection = socket.create_connection(
            (parsed.hostname, parsed.port),
            timeout=0.25,
        )
    except ConnectionRefusedError:
        return
    except OSError as exc:
        raise ValueError(
            "unable to prove the dedicated Chrome endpoint is stopped"
        ) from exc
    connection.close()
    raise ValueError("dedicated Chrome is still running; close it first")


def cleanup_cdp_profile(directory: str) -> None:
    """Remove one initialized temporary profile only after Chrome has stopped."""

    profile = _private_cdp_profile_path(directory)
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise ValueError(
            "guarded automatic cleanup is unavailable on this platform; "
            "use the operating-system file manager"
        )
    expected_profile = profile.lstat()
    _require_profile_stopped(profile)
    if not _same_profile_directory(profile, expected_profile):
        raise OSError("profile identity changed during cleanup preflight")
    quarantine = profile.with_name(
        f".{profile.name}.xianyu-remove-{secrets.token_hex(8)}"
    )
    try:
        profile.rename(quarantine)
        _require_same_profile_directory(
            quarantine,
            expected_profile,
            "profile identity changed during cleanup quarantine",
        )
        _require_profile_stopped(quarantine)
        _require_same_profile_directory(
            quarantine,
            expected_profile,
            "profile identity changed after cleanup preflight",
        )
        shutil.rmtree(quarantine)
    except BaseException as exc:  # noqa: BLE001 - preserve primary interruption.
        restored = _same_profile_directory(profile, expected_profile)
        if not restored and not profile.exists() and not profile.is_symlink():
            if not _same_profile_directory(quarantine, expected_profile):
                raise OSError("unable to restore interrupted profile cleanup") from exc
            try:
                quarantine.rename(profile)
                restored = _same_profile_directory(profile, expected_profile)
            except OSError:
                pass
        if not restored:
            raise OSError("unable to restore interrupted profile cleanup") from exc
        raise
    if profile.exists() or profile.is_symlink():
        raise OSError("profile path was recreated during cleanup")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Safely remove a legacy dedicated CDP profile"
    )
    parser.add_argument("--directory", required=True)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        required=True,
        help="remove the exact initialized profile after dedicated Chrome stops",
    )
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    action = "remove"
    try:
        cleanup_cdp_profile(args.directory)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"profile {action} cancelled",
                    "error_type": type(exc).__name__,
                    "profile": {"status": "not-established"},
                    "cleanup": {
                        "status": "failed",
                        "errors": ["profile operation was interrupted"],
                    },
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (OSError, ValueError) as exc:
        uncertain = isinstance(exc, OSError)
        error = f"unable to {action} dedicated CDP profile" if uncertain else str(exc)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error,
                    "profile": {
                        "status": "not-established" if uncertain else "not-removed"
                    },
                    "cleanup": (
                        {
                            "status": "failed",
                            "errors": ["profile operation did not finish cleanly"],
                        }
                        if uncertain
                        else {"status": "complete-or-not-required"}
                    ),
                },
                ensure_ascii=True,
            )
        )
        return 2
    status = "removed"
    print(
        json.dumps(
            {
                "ok": True,
                "profile": {"status": status},
                "cleanup": {"status": "complete-or-not-required"},
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
