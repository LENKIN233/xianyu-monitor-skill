#!/usr/bin/env python3
"""Run read-only prerequisite checks and emit path-private JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Executing this read-only diagnostic must not leave local bytecode caches.
sys.dont_write_bytecode = True

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

MINIMUM_PYTHON = (3, 10)
REQUIRED_IMPORTS = ("playwright", "tzdata")


class DoctorArgumentParser(JsonArgumentParser):
    """Keep malformed invocations machine-readable without echoing user paths."""

    def error(self, _message: str) -> None:
        super().error("invalid doctor arguments; run --help")


def _expanded_directory_path(value: str) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(
            "directory path could not be resolved"
        ) from exc


def _check_record(
    check_id: str,
    status: str,
    *,
    required: bool,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one stable, path-private check record."""

    return {
        "id": check_id,
        "status": status,
        "required": required,
        "details": dict(details or {}),
    }


def _python_check(version: tuple[int, int, int]) -> dict[str, Any]:
    supported = version[:2] >= MINIMUM_PYTHON
    return _check_record(
        "python-version",
        "passed" if supported else "failed",
        required=True,
        details={
            "detected": ".".join(str(part) for part in version),
            "minimum": ".".join(str(part) for part in MINIMUM_PYTHON),
        },
    )


def _required_imports_check() -> tuple[dict[str, Any], Path | None]:
    missing: list[str] = []
    playwright_package: Path | None = None
    for module_name in REQUIRED_IMPORTS:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, OSError, ValueError):
            spec = None
        if spec is None:
            missing.append(module_name)
            continue
        if module_name == "playwright" and spec.submodule_search_locations:
            try:
                package_location = next(iter(spec.submodule_search_locations))
            except StopIteration:
                package_location = None
            if package_location:
                playwright_package = Path(package_location)

    return (
        _check_record(
            "required-imports",
            "failed" if missing else "passed",
            required=True,
            details={"missing": missing},
        ),
        playwright_package,
    )


def _playwright_browser_root(
    playwright_package: Path,
    *,
    environment: Mapping[str, str],
    platform_name: str,
    home: Path,
) -> Path:
    configured = environment.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        return playwright_package / "driver" / "package" / ".local-browsers"
    if configured:
        return Path(configured).expanduser()
    if platform_name == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    if platform_name == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "ms-playwright"
        return home / "AppData" / "Local" / "ms-playwright"
    cache_home = environment.get("XDG_CACHE_HOME")
    return (Path(cache_home) if cache_home else home / ".cache") / "ms-playwright"


def _is_usable_executable(path: Path, *, os_name: str = os.name) -> bool:
    try:
        if not path.is_file():
            return False
        return os_name == "nt" or os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _bundled_chromium_available(
    playwright_package: Path | None,
    *,
    environment: Mapping[str, str] = os.environ,
    platform_name: str = sys.platform,
    os_name: str = os.name,
    home: Path | None = None,
) -> bool:
    """Check the installed Playwright revision without starting its driver."""

    if playwright_package is None:
        return False
    metadata_file = playwright_package / "driver" / "package" / "browsers.json"
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        browsers = metadata["browsers"]
        chromium = next(item for item in browsers if item.get("name") == "chromium")
        revisions = {
            str(chromium["revision"]),
            *(str(value) for value in chromium.get("revisionOverrides", {}).values()),
        }
    except (
        AttributeError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        StopIteration,
    ):
        return False

    try:
        root = _playwright_browser_root(
            playwright_package,
            environment=environment,
            platform_name=platform_name,
            home=Path.home() if home is None else home,
        )
    except (OSError, RuntimeError, ValueError):
        return False
    relative_executables = (
        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chrome-linux/chrome",
        "chrome-linux64/chrome",
        "chrome-win/chrome.exe",
        "chrome-win64/chrome.exe",
    )
    return any(
        _is_usable_executable(
            root / f"chromium-{revision}" / relative_path,
            os_name=os_name,
        )
        for revision in revisions
        for relative_path in relative_executables
    )


def _local_chrome_candidates(
    *,
    environment: Mapping[str, str],
    platform_name: str,
) -> list[Path]:
    if platform_name == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    if platform_name == "linux":
        return [Path("/opt/google/chrome/chrome")]
    if platform_name == "win32":
        candidates: list[Path] = []
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = environment.get(variable)
            if base:
                candidates.append(
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
        home_drive = environment.get("HOMEDRIVE")
        if home_drive:
            drive_name = home_drive.rstrip("\\/")
            for program_files in ("Program Files", "Program Files (x86)"):
                candidates.append(
                    Path(
                        f"{drive_name}/{program_files}/Google/Chrome/"
                        "Application/chrome.exe"
                    )
                )
        return candidates
    return []


def _local_chrome_available(
    *,
    environment: Mapping[str, str] = os.environ,
    platform_name: str = sys.platform,
    os_name: str = os.name,
) -> bool:
    try:
        return any(
            _is_usable_executable(candidate, os_name=os_name)
            for candidate in _local_chrome_candidates(
                environment=environment,
                platform_name=platform_name,
            )
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _directory_check(check_id: str, directory: Path | None) -> dict[str, Any]:
    if directory is None:
        return _check_record(
            check_id,
            "not-requested",
            required=False,
            details={"issues": []},
        )

    issues: list[str] = []
    try:
        metadata = directory.stat()
    except FileNotFoundError:
        issues.append("missing")
        metadata = None
    except (OSError, ValueError):
        issues.append("not-accessible")
        metadata = None

    if metadata is not None:
        if not stat.S_ISDIR(metadata.st_mode):
            issues.append("not-a-directory")
        else:
            access_mode = os.R_OK | os.W_OK
            if os.name != "nt":
                access_mode |= os.X_OK
            try:
                accessible = os.access(directory, access_mode)
            except (OSError, ValueError):
                accessible = False
            if not accessible:
                issues.append("insufficient-access")
            if os.name != "nt":
                if metadata.st_uid != os.getuid():
                    issues.append("not-owned-by-current-user")
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    issues.append("not-private")

    return _check_record(
        check_id,
        "failed" if issues else "passed",
        required=True,
        details={"issues": issues},
    )


def run_doctor(
    *,
    state_output_dir: Path | None = None,
    tasks_dir: Path | None = None,
    version: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """Return prerequisite evidence without launching a browser or writing files."""

    python_check = _python_check(
        tuple(sys.version_info[:3]) if version is None else version
    )
    imports_check, playwright_package = _required_imports_check()
    bundled_available = _bundled_chromium_available(playwright_package)
    local_available = _local_chrome_available()
    checks = [
        python_check,
        imports_check,
        _check_record(
            "playwright-chromium",
            "available" if bundled_available else "unavailable",
            required=False,
        ),
        _check_record(
            "local-chrome",
            "available" if local_available else "unavailable",
            required=False,
        ),
        _check_record(
            "browser-runtime",
            "passed" if bundled_available or local_available else "failed",
            required=True,
            details={
                "selection": (
                    "playwright-chromium"
                    if bundled_available
                    else "local-chrome"
                    if local_available
                    else "none"
                )
            },
        ),
        _directory_check("state-output-directory", state_output_dir),
        _directory_check("tasks-directory", tasks_dir),
    ]
    healthy = all(check["status"] != "failed" for check in checks if check["required"])

    if python_check["status"] == "failed":
        next_action = {
            "code": "upgrade-python",
            "hint": "Install Python 3.10 or newer, then run doctor again.",
        }
    elif imports_check["status"] == "failed":
        next_action = {
            "code": "install-dependencies",
            "hint": (
                "Install requirements.txt in the active environment, then run "
                "doctor again."
            ),
        }
    elif not (bundled_available or local_available):
        next_action = {
            "code": "install-browser",
            "hint": (
                "Install Playwright Chromium or a supported local Chrome, then run "
                "doctor again."
            ),
        }
    elif any(
        check["status"] == "failed"
        for check in checks
        if check["id"] in {"state-output-directory", "tasks-directory"}
    ):
        next_action = {
            "code": "fix-private-directories",
            "hint": (
                "Provide existing, private, readable and writable directories, "
                "then run doctor again."
            ),
        }
    elif bundled_available:
        next_action = {
            "code": "ready",
            "hint": "Prerequisites are ready; continue with login or search.",
        }
    else:
        next_action = {
            "code": "ready-use-browser-channel",
            "hint": (
                "Prerequisites are ready; pass --browser-channel chrome to browser "
                "commands."
            ),
        }

    return {"ok": healthy, "checks": checks, "next_action": next_action}


def build_parser() -> argparse.ArgumentParser:
    parser = DoctorArgumentParser(
        description=(
            "Run read-only Xianyu prerequisite checks without launching a browser"
        )
    )
    parser.add_argument(
        "--state-output-dir",
        type=_expanded_directory_path,
        help=(
            "check only this existing private state-output directory; "
            "the path is never included in JSON"
        ),
    )
    parser.add_argument(
        "--tasks-dir",
        type=_expanded_directory_path,
        help=(
            "check only this existing private task directory; "
            "the path is never included in JSON"
        ),
    )
    return parser


@sigterm_cancellable
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_doctor(
        state_output_dir=args.state_output_dir,
        tasks_dir=args.tasks_dir,
    )
    print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
