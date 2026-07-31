#!/usr/bin/env python3
"""Search Xianyu with Playwright and return normalized JSON results."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

try:
    from playwright.async_api import (
        BrowserContext,
        Page,
        Route,
        async_playwright,
    )
    from playwright.async_api import (
        Error as PlaywrightError,
    )
    from playwright.async_api import (
        TimeoutError as PlaywrightTimeoutError,
    )
except ImportError:  # Keep the module importable for setup/help commands.
    BrowserContext = Any
    Page = Any
    Route = Any
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


BASE_URL = "https://www.goofish.com"
SEARCH_API_HOST = "h5api.m.goofish.com"
SEARCH_API_PATH = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
SEARCH_API_ROUTE = f"https://{SEARCH_API_HOST}{SEARCH_API_PATH}**"
NEXT_PAGE_SELECTOR = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
DEFAULT_TIMEOUT_MS = 30_000
PAGINATION_WAIT_MS = 5_000
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CDP_PROFILE_SENTINEL_NAME = ".xianyu-monitor-cdp-profile"
CDP_PROFILE_SENTINEL_VALUE = "xianyu-monitor dedicated cdp profile v1\n"


def _read_small_profile_file(profile: Path, name: str, max_bytes: int) -> str:
    """Read one regular profile file without following a POSIX symlink race."""

    if Path(name).name != name or max_bytes < 1:
        raise ValueError("invalid private profile file request")
    if os.name == "nt":
        path = profile / name
        try:
            before = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(before.st_mode):
                raise ValueError("invalid private profile marker")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            raise ValueError("unable to read private profile marker") from exc
        try:
            after = os.fstat(descriptor)
            if not os.path.samestat(before, after) or after.st_size > max_bytes:
                raise ValueError("invalid private profile marker")
            payload = os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)
    else:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            expected_directory = profile.stat()
            directory_descriptor = os.open(profile, directory_flags)
            try:
                actual_directory = os.fstat(directory_descriptor)
                if not os.path.samestat(expected_directory, actual_directory):
                    raise ValueError("private profile directory changed during read")
                descriptor = os.open(
                    name,
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise ValueError("unable to read private profile marker") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise ValueError("invalid private profile marker")
            if metadata.st_uid != os.getuid():
                raise ValueError("private profile marker must be user-owned")
            payload = os.read(descriptor, max_bytes + 1)
        finally:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise ValueError("invalid private profile marker")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid private profile marker") from exc


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _windows_local_app_data() -> Path:
    """Resolve LocalAppData through the Windows Known Folder API."""

    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]

    folder_id = Guid(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    raw_path = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    com_result = ole32.CoInitializeEx(None, 0x2)
    changed_mode = ctypes.c_int32(0x80010106).value
    if com_result not in {0, 1, changed_mode}:
        raise OSError("unable to initialize Windows known-folder resolution")
    uninitialize = com_result in {0, 1}
    try:
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id),
            0,
            None,
            ctypes.byref(raw_path),
        )
        if result != 0 or raw_path.value is None:
            raise OSError("unable to resolve the Windows LocalAppData known folder")
        return Path(ctypes.wstring_at(raw_path.value))
    finally:
        ole32.CoTaskMemFree(raw_path)
        if uninitialize:
            ole32.CoUninitialize()


def _command_line_uses_profile(arguments: Any, expected_profile: Path) -> bool:
    """Verify Chrome reports the exact approved --user-data-dir argument."""

    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        return False
    candidates: list[str] = []
    for index, argument in enumerate(arguments):
        if argument.startswith("--user-data-dir="):
            candidates.append(argument.partition("=")[2])
        elif argument == "--user-data-dir" and index + 1 < len(arguments):
            candidates.append(arguments[index + 1])
    if len(candidates) != 1:
        return False
    reported = Path(candidates[0]).expanduser()
    return reported.is_absolute() and _same_existing_path(reported, expected_profile)


def _windows_cdp_temp_root_aliases() -> tuple[tuple[Path, Path], ...]:
    """Trust Known Folder redirection, but never a reparse of its Temp child."""

    lexical_base = _windows_local_app_data().expanduser().absolute()
    canonical_base = lexical_base.resolve(strict=False)
    lexical_root = lexical_base / "Temp"
    canonical_root = canonical_base / "Temp"
    if lexical_root.resolve(strict=False) != canonical_root:
        raise ValueError("Windows LocalAppData Temp must not be a reparse point")
    aliases = [(lexical_root, canonical_root)]
    canonical_pair = (canonical_root, canonical_root)
    if canonical_pair not in aliases:
        aliases.append(canonical_pair)
    return tuple(aliases)


def _temporary_cdp_root_aliases() -> tuple[tuple[Path, Path], ...]:
    """Return approved lexical temp roots paired with canonical paths."""

    if os.name == "nt":
        return _windows_cdp_temp_root_aliases()

    roots = [
        Path("/tmp"),  # noqa: S108
        Path("/var/tmp"),  # noqa: S108
    ]
    if hasattr(os, "getuid"):
        roots.append(Path("/run/user") / str(os.getuid()))
    if sys.platform == "darwin":
        configured = Path(tempfile.gettempdir()).expanduser().absolute()
        canonical = configured.resolve(strict=False)
        try:
            relative = canonical.relative_to("/private/var/folders")
        except ValueError:
            pass
        else:
            if len(relative.parts) == 3 and relative.parts[-1] == "T":
                roots.append(configured)

    aliases: list[tuple[Path, Path]] = []
    for root in roots:
        lexical = root.expanduser().absolute()
        canonical = lexical.resolve(strict=False)
        pair = (lexical, canonical)
        if pair not in aliases:
            aliases.append(pair)
        canonical_pair = (canonical, canonical)
        if canonical_pair not in aliases:
            aliases.append(canonical_pair)
    return tuple(aliases)


def _require_temporary_cdp_path(profile: Path) -> None:
    roots = {canonical for _, canonical in _temporary_cdp_root_aliases()}
    if not any(profile != root and profile.is_relative_to(root) for root in roots):
        raise ValueError(
            "CDP user-data directory must be inside an operating-system "
            "temporary directory"
        )


def _resolve_temporary_cdp_directory(user_data_dir: str) -> Path:
    """Resolve a CDP directory while allowing only trusted temp-root aliases."""

    requested = Path(user_data_dir).expanduser()
    if not requested.is_absolute():
        raise ValueError("CDP user-data directory must be an absolute path")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ValueError("CDP user-data directory does not exist") from exc

    lexical = requested.absolute()
    if resolved != lexical:
        trusted_alias = False
        for lexical_root, canonical_root in _temporary_cdp_root_aliases():
            try:
                relative = lexical.relative_to(lexical_root)
            except ValueError:
                continue
            if relative.parts and canonical_root.joinpath(relative) == resolved:
                trusted_alias = True
                break
        if not trusted_alias:
            raise ValueError("CDP user-data directory must not traverse symlinks")
    if not resolved.is_dir():
        raise ValueError("CDP user-data path must be a directory")
    _require_temporary_cdp_path(resolved)
    return resolved


def _private_cdp_profile_path(user_data_dir: str) -> Path:
    """Validate an explicitly dedicated, user-private Chromium profile."""

    resolved = _resolve_temporary_cdp_directory(user_data_dir)

    home = Path.home()
    default_roots: list[Path] = []
    if sys.platform == "darwin":
        default_roots.extend(
            [
                home / "Library/Application Support/Google/Chrome",
                home / "Library/Application Support/Chromium",
                home / "Library/Application Support/Google/Chrome for Testing",
            ]
        )
    elif os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data)
            default_roots.extend(
                [
                    base / "Google/Chrome/User Data",
                    base / "Chromium/User Data",
                ]
            )
    else:
        config_home = Path(os.getenv("XDG_CONFIG_HOME", home / ".config"))
        default_roots.extend([config_home / "google-chrome", config_home / "chromium"])
    for default_root in default_roots:
        candidate_root = default_root.expanduser().resolve(strict=False)
        lexical_match = resolved == candidate_root or resolved.is_relative_to(
            candidate_root
        )
        identity_match = any(
            _same_existing_path(ancestor, candidate_root)
            for ancestor in (resolved, *resolved.parents)
        )
        if lexical_match or identity_match:
            raise ValueError("refusing to use a default browser profile for CDP")

    if os.name != "nt":
        metadata = resolved.stat()
        if metadata.st_uid != os.getuid():
            raise ValueError(
                "CDP user-data directory must be owned by the current user"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(
                "CDP user-data directory must be private; run chmod 700 on it"
            )
    try:
        sentinel = _read_small_profile_file(
            resolved,
            CDP_PROFILE_SENTINEL_NAME,
            128,
        )
    except ValueError as exc:
        raise ValueError(
            "CDP profile was not initialized by scripts/cdp_profile.py"
        ) from exc
    if sentinel != CDP_PROFILE_SENTINEL_VALUE:
        raise ValueError("invalid dedicated CDP profile sentinel")
    return resolved


def _cdp_endpoint_from_user_data_dir(
    user_data_dir: str,
    *,
    timeout_seconds: float = 15.0,
) -> str:
    """Read Chrome's loopback endpoint from a dedicated profile marker."""

    profile = _private_cdp_profile_path(user_data_dir)
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        try:
            marker.lstat()
            break
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("unable to inspect DevToolsActivePort") from exc
        if time.monotonic() >= deadline:
            raise ValueError(
                "dedicated Chrome is not ready; DevToolsActivePort was not found"
            )
        time.sleep(0.1)
    try:
        lines = _read_small_profile_file(
            profile,
            "DevToolsActivePort",
            1024,
        ).splitlines()
    except ValueError as exc:
        raise ValueError("unable to read DevToolsActivePort") from exc
    if len(lines) != 2:
        raise ValueError("invalid DevToolsActivePort marker")
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise ValueError("invalid DevToolsActivePort marker") from exc
    browser_path = lines[1]
    if (
        not 1 <= port <= 65535
        or re.fullmatch(
            r"/devtools/browser/[A-Za-z0-9._-]+",
            browser_path,
        )
        is None
    ):
        raise ValueError("invalid DevToolsActivePort marker")
    return f"ws://127.0.0.1:{port}{browser_path}"


class SpiderError(RuntimeError):
    """Base error for a user-actionable spider failure."""


class DependencyError(SpiderError):
    """Raised when a required runtime dependency is missing."""


class BrowserConnectionError(SpiderError):
    """Raised when a dedicated local CDP browser cannot be reached."""


async def _verify_async_cdp_profile(browser: Any, expected_profile: Path) -> None:
    """Fail closed unless Chrome proves which user-data directory it uses."""

    session: Any | None = None
    try:
        session = await browser.new_browser_cdp_session()
        payload = await session.send("Browser.getBrowserCommandLine")
    except PlaywrightError as exc:
        raise BrowserConnectionError(
            "connected Chrome could not prove its dedicated user-data directory; "
            "restart it with --enable-automation"
        ) from exc
    finally:
        if session is not None:
            try:
                await session.detach()
            except PlaywrightError:
                pass
    arguments = payload.get("arguments") if isinstance(payload, dict) else None
    if not _command_line_uses_profile(arguments, expected_profile):
        raise BrowserConnectionError(
            "connected Chrome did not use the approved dedicated user-data directory"
        )


class StateFileError(SpiderError):
    """Raised when a browser-state file is invalid or not accepted."""


class StorageStateValidationError(ValueError):
    """Raised when browser-state structure cannot be sanitized safely."""


class StateRejectedError(StateFileError):
    """Raised when Xianyu rejects a supplied browser state."""


class LoginRequiredError(StateFileError):
    """Raised when Xianyu requires login and no browser state was supplied."""


class SearchRejectedError(SpiderError):
    """Raised when Xianyu rejects a search request."""


class SearchCaptureError(SpiderError):
    """Raised when a page does not emit the expected search request."""


class BrowserCleanupError(SpiderError):
    """Raised when a dedicated browser cannot be closed cleanly."""

    def __init__(self, message: str, *, search_passed: bool):
        super().__init__(message)
        self.search_passed = search_passed
        self.capability_status = (
            "passed-for-this-run" if search_passed else "not-established"
        )
        self.cleanup_failures = [message]


class SearchCancelledError(BrowserCleanupError):
    """Raised when cancellation and incomplete cleanup occur together."""

    def __init__(
        self,
        message: str,
        *,
        capability_status: str,
    ):
        super().__init__(
            message,
            search_passed=capability_status == "passed-for-this-run",
        )
        self.capability_status = capability_status
        self.cancelled = True


def search_capability_status(error: BaseException) -> str:
    status = getattr(error, "capability_status", None)
    if status in {
        "passed-for-this-run",
        "rejected-for-this-run",
        "not-established",
    }:
        return status
    if isinstance(error, BrowserCleanupError) and error.search_passed:
        return "passed-for-this-run"
    if isinstance(
        error,
        (LoginRequiredError, SearchRejectedError, StateRejectedError),
    ):
        return "rejected-for-this-run"
    return "not-established"


def _append_cleanup_failure(error: BaseException, message: str) -> None:
    failures = getattr(error, "cleanup_failures", None)
    if not isinstance(failures, list):
        failures = []
        setattr(error, "cleanup_failures", failures)
    if message not in failures:
        failures.append(message)


def _cancelled_cleanup_failure(
    error: asyncio.CancelledError,
    message: str,
) -> SearchCancelledError:
    """Keep cleanup evidence across Python 3.10 Task cancellation boundaries."""

    _append_cleanup_failure(error, message)
    terminal_error = SearchCancelledError(
        "search was cancelled; browser cleanup was incomplete",
        capability_status=search_capability_status(error),
    )
    terminal_error.cleanup_failures = list(error.cleanup_failures)
    return terminal_error


def cleanup_evidence(error: BaseException | None = None) -> dict[str, Any]:
    failures = getattr(error, "cleanup_failures", None) if error else None
    if isinstance(failures, list) and failures:
        return {"status": "failed", "errors": list(failures)}
    return {"status": "complete-or-not-required"}


async def _cleanup_interrupted_playwright_start(
    manager: Any,
    error: BaseException,
) -> None:
    """Stop a partially initialized async Playwright manager."""

    message = "failed to stop the partially started browser runtime"
    exit_action = getattr(manager, "__aexit__", None)
    if not callable(exit_action):
        if isinstance(error, asyncio.CancelledError):
            raise _cancelled_cleanup_failure(error, message) from error
        _append_cleanup_failure(error, message)
        return
    try:
        await exit_action(type(error), error, error.__traceback__)
    except BaseException as cleanup_error:  # noqa: BLE001
        if not isinstance(cleanup_error, Exception):
            for failure in getattr(error, "cleanup_failures", []):
                _append_cleanup_failure(cleanup_error, failure)
            setattr(
                cleanup_error,
                "capability_status",
                search_capability_status(error),
            )
            if isinstance(cleanup_error, asyncio.CancelledError):
                raise _cancelled_cleanup_failure(
                    cleanup_error,
                    message,
                ) from cleanup_error
            _append_cleanup_failure(cleanup_error, message)
            raise
        if isinstance(error, asyncio.CancelledError):
            # Python 3.10 replaces a CancelledError that escapes a Task, so
            # attributes attached to that instance do not survive asyncio.run.
            raise _cancelled_cleanup_failure(error, message) from error
        _append_cleanup_failure(error, message)


@dataclass(frozen=True)
class CapturedSearchResponse:
    """A search response captured before it reaches the page."""

    status: int | None
    payload: dict[str, Any] | None
    error: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class CaptureTicket:
    """Identify one explicitly armed search-request capture window."""

    generation: int
    expected_page: int


class RateLimiter:
    """Keep browser actions separated by a configurable interval."""

    def __init__(self, min_delay: float = 2.0, max_delay: float = 5.0):
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("invalid rate-limit interval")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_action = 0.0

    async def wait(self) -> None:
        import random

        delay = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last_action
        if self._last_action and elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_action = time.monotonic()


def _is_exact_https_origin(url: str, hostname: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and parsed.netloc.lower() == hostname


def _is_goofish_hostname(hostname: str | None) -> bool:
    normalized = (hostname or "").lower()
    return normalized == "goofish.com" or normalized.endswith(".goofish.com")


def _contains_control_characters(value: str) -> bool:
    return any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
    )


def _normalized_cookie_domain(domain: Any) -> str:
    if not isinstance(domain, str) or not domain:
        raise StorageStateValidationError("browser state cookie has invalid domain")
    dotted = domain.startswith(".")
    hostname = domain[1:] if dotted else domain
    if hostname.startswith(".") or len(hostname) > 253:
        raise StorageStateValidationError("browser state cookie has invalid domain")
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StorageStateValidationError(
            "browser state cookie has invalid domain"
        ) from exc
    labels = hostname.lower().split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise StorageStateValidationError("browser state cookie has invalid domain")
    normalized = ".".join(labels)
    return f".{normalized}" if dotted else normalized


def _normalized_goofish_origin(origin: Any) -> str | None:
    if not isinstance(origin, str) or not origin:
        raise StorageStateValidationError("browser state origin is invalid")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise StorageStateValidationError("browser state origin is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise StorageStateValidationError("browser state origin is invalid")
    normalized_hostname = _normalized_cookie_domain(parsed.hostname)
    if normalized_hostname.startswith("."):
        raise StorageStateValidationError("browser state origin is invalid")
    if parsed.scheme.lower() != "https" or not _is_goofish_hostname(
        normalized_hostname
    ):
        return None
    return f"https://{normalized_hostname}"


def _sanitize_indexed_db(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise StorageStateValidationError("browser state IndexedDB data is invalid")
    databases: list[dict[str, Any]] = []
    database_names: set[str] = set()
    for database in value:
        if not isinstance(database, dict):
            raise StorageStateValidationError("browser state IndexedDB data is invalid")
        name = database.get("name")
        version = database.get("version")
        stores = database.get("stores")
        if (
            not isinstance(name, str)
            or not name
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(stores, list)
        ):
            raise StorageStateValidationError("browser state IndexedDB data is invalid")
        if name in database_names:
            raise StorageStateValidationError("browser state IndexedDB data is invalid")
        database_names.add(name)
        clean_stores: list[dict[str, Any]] = []
        store_names: set[str] = set()
        for store in stores:
            if not isinstance(store, dict):
                raise StorageStateValidationError(
                    "browser state IndexedDB data is invalid"
                )
            store_name = store.get("name")
            records = store.get("records")
            indexes = store.get("indexes")
            auto_increment = store.get("autoIncrement")
            if (
                not isinstance(store_name, str)
                or not store_name
                or not isinstance(auto_increment, bool)
                or not isinstance(records, list)
                or not isinstance(indexes, list)
            ):
                raise StorageStateValidationError(
                    "browser state IndexedDB data is invalid"
                )
            if store_name in store_names:
                raise StorageStateValidationError(
                    "browser state IndexedDB data is invalid"
                )
            store_names.add(store_name)
            clean_store: dict[str, Any] = {
                "name": store_name,
                "autoIncrement": auto_increment,
                "records": [],
                "indexes": [],
            }
            _copy_key_path(store, clean_store)
            for record in records:
                if not isinstance(record, dict):
                    raise StorageStateValidationError(
                        "browser state IndexedDB data is invalid"
                    )
                key_fields = [key for key in ("key", "keyEncoded") if key in record]
                value_fields = [
                    key for key in ("value", "valueEncoded") if key in record
                ]
                if len(key_fields) > 1 or len(value_fields) != 1:
                    raise StorageStateValidationError(
                        "browser state IndexedDB data is invalid"
                    )
                clean_record = {
                    value_fields[0]: record[value_fields[0]],
                }
                if key_fields:
                    clean_record[key_fields[0]] = record[key_fields[0]]
                clean_store["records"].append(clean_record)
            for index in indexes:
                if not isinstance(index, dict):
                    raise StorageStateValidationError(
                        "browser state IndexedDB data is invalid"
                    )
                index_name = index.get("name")
                multi_entry = index.get("multiEntry")
                unique = index.get("unique")
                if (
                    not isinstance(index_name, str)
                    or not index_name
                    or not isinstance(multi_entry, bool)
                    or not isinstance(unique, bool)
                    or not (("keyPath" in index) ^ ("keyPathArray" in index))
                ):
                    raise StorageStateValidationError(
                        "browser state IndexedDB data is invalid"
                    )
                if any(
                    existing["name"] == index_name
                    for existing in clean_store["indexes"]
                ):
                    raise StorageStateValidationError(
                        "browser state IndexedDB data is invalid"
                    )
                clean_index: dict[str, Any] = {
                    "name": index_name,
                    "multiEntry": multi_entry,
                    "unique": unique,
                }
                _copy_key_path(index, clean_index)
                clean_store["indexes"].append(clean_index)
            clean_stores.append(clean_store)
        databases.append({"name": name, "version": version, "stores": clean_stores})
    return databases


def _copy_key_path(source: dict[str, Any], target: dict[str, Any]) -> None:
    has_string = "keyPath" in source
    has_array = "keyPathArray" in source
    if has_string and has_array:
        raise StorageStateValidationError("browser state IndexedDB data is invalid")
    if has_string:
        if not isinstance(source["keyPath"], str):
            raise StorageStateValidationError("browser state IndexedDB data is invalid")
        target["keyPath"] = source["keyPath"]
    if has_array:
        key_path = source["keyPathArray"]
        if not isinstance(key_path, list) or not all(
            isinstance(part, str) for part in key_path
        ):
            raise StorageStateValidationError("browser state IndexedDB data is invalid")
        target["keyPathArray"] = list(key_path)


def _filter_goofish_storage_state(state: Any) -> dict[str, Any]:
    """Keep only Goofish-scoped cookies and HTTPS origin storage."""

    if not isinstance(state, dict):
        raise StorageStateValidationError("browser state must be a JSON object")

    cookies = state.get("cookies")
    if not isinstance(cookies, list):
        raise StorageStateValidationError("browser state cookies must be an array")
    filtered_cookies: list[dict[str, Any]] = []
    cookie_identities: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise StorageStateValidationError("browser state cookie is invalid")
        name = cookie.get("name")
        value = cookie.get("value")
        path = cookie.get("path")
        domain = _normalized_cookie_domain(cookie.get("domain"))
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or not isinstance(path, str)
            or not path.startswith("/")
            or _contains_control_characters(name)
            or _contains_control_characters(value)
            or _contains_control_characters(path)
        ):
            raise StorageStateValidationError("browser state cookie is invalid")
        if not _is_goofish_hostname(domain.lstrip(".")):
            continue
        # Do not silently turn URL- or partition-scoped input into a broader
        # unpartitioned domain cookie.
        if "url" in cookie or "partitionKey" in cookie:
            continue
        identity = (name, domain, path)
        if identity in cookie_identities:
            raise StorageStateValidationError("browser state cookie is duplicated")
        cookie_identities.add(identity)
        clean_cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }
        if "expires" in cookie:
            expires = cookie["expires"]
            if (
                isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not math.isfinite(expires)
            ):
                raise StorageStateValidationError("browser state cookie is invalid")
            clean_cookie["expires"] = expires
        for field in ("httpOnly", "secure"):
            if field in cookie:
                if not isinstance(cookie[field], bool):
                    raise StorageStateValidationError("browser state cookie is invalid")
                clean_cookie[field] = cookie[field]
        if "sameSite" in cookie:
            if cookie["sameSite"] not in {"Lax", "None", "Strict"}:
                raise StorageStateValidationError("browser state cookie is invalid")
            clean_cookie["sameSite"] = cookie["sameSite"]
        filtered_cookies.append(clean_cookie)

    origins = state.get("origins")
    if origins is None:
        origins = []
    if not isinstance(origins, list):
        raise StorageStateValidationError("browser state origins must be an array")
    filtered_origins: list[dict[str, Any]] = []
    seen_origins: set[str] = set()
    for origin in origins:
        if not isinstance(origin, dict):
            raise StorageStateValidationError("browser state origin is invalid")
        normalized_origin = _normalized_goofish_origin(origin.get("origin"))
        if normalized_origin is None:
            continue
        if normalized_origin in seen_origins:
            raise StorageStateValidationError("browser state origin is duplicated")
        seen_origins.add(normalized_origin)
        local_storage = origin.get("localStorage")
        if not isinstance(local_storage, list):
            raise StorageStateValidationError("browser state localStorage is invalid")
        clean_local_storage: list[dict[str, str]] = []
        local_storage_names: set[str] = set()
        for entry in local_storage:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("value"), str)
            ):
                raise StorageStateValidationError(
                    "browser state localStorage is invalid"
                )
            if entry["name"] in local_storage_names:
                raise StorageStateValidationError(
                    "browser state localStorage is duplicated"
                )
            local_storage_names.add(entry["name"])
            clean_local_storage.append({"name": entry["name"], "value": entry["value"]})
        clean_origin: dict[str, Any] = {
            "origin": normalized_origin,
            "localStorage": clean_local_storage,
        }
        if "indexedDB" in origin:
            clean_origin["indexedDB"] = _sanitize_indexed_db(origin["indexedDB"])
        filtered_origins.append(clean_origin)
    return {"cookies": filtered_cookies, "origins": filtered_origins}


def _is_search_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        _is_exact_https_origin(url, SEARCH_API_HOST)
        and parsed.path == SEARCH_API_PATH
        and not parsed.fragment
    )


def _decode_search_request_data(request: Any) -> list[dict[str, Any]] | None:
    """Decode MTop data objects without logging their signed request envelope."""

    encoded_values: list[str] = []
    request_url = str(getattr(request, "url", "") or "")
    try:
        request_query = urlsplit(request_url).query
    except ValueError:
        return None
    query_values = parse_qs(request_query, keep_blank_values=True).get("data", [])
    encoded_values.extend(query_values)

    post_data = getattr(request, "post_data", None)
    if isinstance(post_data, str) and post_data:
        if len(post_data) > 1_000_000:
            return None
        form_values = parse_qs(post_data, keep_blank_values=True).get("data")
        if form_values:
            encoded_values.extend(form_values)
        elif post_data.lstrip().startswith("{"):
            encoded_values.append(post_data)

    if not encoded_values:
        return None

    decoded: list[dict[str, Any]] = []
    for encoded in encoded_values:
        if not encoded or len(encoded) > 1_000_000:
            return None
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        decoded.append(value)
    return decoded


def _collect_named_values(
    value: Any,
    names: set[str],
    *,
    depth: int = 0,
) -> list[Any]:
    if depth > 8:
        return []
    found: list[Any] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() in names:
                found.append(nested)
            found.extend(_collect_named_values(nested, names, depth=depth + 1))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_collect_named_values(nested, names, depth=depth + 1))
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return found
        found.extend(_collect_named_values(nested, names, depth=depth + 1))
    return found


def _matching_search_request_page(
    request: Any,
    expected_keyword: str,
) -> int | None:
    """Return the unambiguous page number for one exact search request."""

    request_url = str(getattr(request, "url", "") or "")
    if not _is_search_api_url(request_url):
        return None

    data_objects = _decode_search_request_data(request)
    if not data_objects:
        return None

    keyword_values: list[Any] = []
    page_values: list[Any] = []
    for data in data_objects:
        keyword_values.extend(_collect_named_values(data, {"keyword"}))
        page_values.extend(_collect_named_values(data, {"pagenumber"}))

    normalized_keywords = {
        value.strip()
        for value in keyword_values
        if isinstance(value, str) and value.strip()
    }
    if normalized_keywords != {expected_keyword}:
        return None

    normalized_pages: set[int] = set()
    for value in page_values:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            page_number = value
        elif isinstance(value, str) and value.strip().isdigit():
            page_number = int(value.strip())
        else:
            return None
        if not 1 <= page_number <= 10_000:
            return None
        normalized_pages.add(page_number)
    if len(normalized_pages) != 1:
        return None
    return normalized_pages.pop()


class SearchResponseCollector:
    """Capture only the exact GET or POST search endpoint.

    Routing the request keeps the response body available even when Chromium's
    DevTools response cache discards it before an asynchronous event callback
    reads it.
    """

    def __init__(self, expected_keyword: str) -> None:
        self.expected_keyword = expected_keyword
        self._queue: asyncio.Queue[tuple[int, CapturedSearchResponse]] = asyncio.Queue()
        self._generation = 0
        self._active_ticket: CaptureTicket | None = None

    async def install(self, context: BrowserContext) -> None:
        await context.route(SEARCH_API_ROUTE, self._handle_route)

    def arm(self, expected_page: int) -> CaptureTicket:
        """Open a new capture window immediately before the triggering action."""

        if self._active_ticket is not None:
            raise SearchCaptureError("a search capture window is already active")
        self._generation += 1
        ticket = CaptureTicket(self._generation, expected_page)
        self._active_ticket = ticket
        return ticket

    def disarm(self, ticket: CaptureTicket) -> None:
        """Close ``ticket`` without disturbing a newer capture window."""

        if self._active_ticket == ticket:
            self._active_ticket = None

    async def _handle_route(self, route: Route) -> None:
        request = route.request
        if request.method.upper() not in {"GET", "POST"}:
            await route.continue_()
            return
        page_number = _matching_search_request_page(
            request,
            self.expected_keyword,
        )
        if page_number is None:
            await route.continue_()
            return

        # Snapshot the active generation before the first await. A request from
        # an older action may finish after a later page has been armed; tagging
        # here prevents that late response from satisfying the newer action.
        ticket = self._active_ticket
        if ticket is None or ticket.expected_page != page_number:
            await route.continue_()
            return
        generation = ticket.generation

        try:
            response = await route.fetch(max_redirects=0)
            body = await response.body()
            await route.fulfill(response=response, body=body)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await self._queue.put(
                    (
                        generation,
                        CapturedSearchResponse(
                            status=response.status,
                            payload=None,
                            error=f"search API returned invalid JSON: {exc}",
                            page_number=page_number,
                        ),
                    )
                )
                return
            if not isinstance(payload, dict):
                await self._queue.put(
                    (
                        generation,
                        CapturedSearchResponse(
                            status=response.status,
                            payload=None,
                            error="search API returned a non-object JSON payload",
                            page_number=page_number,
                        ),
                    )
                )
                return

            await self._queue.put(
                (
                    generation,
                    CapturedSearchResponse(
                        status=response.status,
                        payload=payload,
                        page_number=page_number,
                    ),
                )
            )
        except PlaywrightError:
            try:
                await route.continue_()
            except PlaywrightError:
                pass
            await self._queue.put(
                (
                    generation,
                    CapturedSearchResponse(
                        status=None,
                        payload=None,
                        error="failed to capture search API response",
                        page_number=page_number,
                    ),
                )
            )

    async def next(
        self,
        ticket: CaptureTicket,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> CapturedSearchResponse:
        if self._active_ticket != ticket:
            raise SearchCaptureError("search capture window is not active")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_ms, 1) / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SearchCaptureError("timed out waiting for Xianyu search API")
            try:
                generation, capture = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise SearchCaptureError(
                    "timed out waiting for Xianyu search API"
                ) from exc
            if (
                generation == ticket.generation
                and capture.page_number == ticket.expected_page
            ):
                return capture


def build_proxy_settings(proxy: str) -> dict[str, str]:
    """Convert a proxy URL into Playwright's credential-safe launch shape."""

    raw = proxy.strip()
    if not raw:
        raise ValueError("proxy URL must not be empty")
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError("proxy scheme must be http, https, or socks5")
    if not parsed.hostname:
        raise ValueError("proxy URL must include a host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL must not include a path, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL has an invalid port") from exc

    has_credentials = parsed.username is not None or parsed.password is not None
    if scheme == "socks5" and has_credentials:
        raise ValueError("Playwright does not support authenticated SOCKS5 proxies")

    server_netloc = parsed.netloc.rsplit("@", 1)[-1]
    settings = {"server": f"{scheme}://{server_netloc}"}
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        settings["password"] = unquote(parsed.password)
    return settings


def _redact_proxy(proxy: str) -> str:
    """Return a proxy label that never includes credentials."""

    try:
        return build_proxy_settings(proxy)["server"]
    except ValueError:
        return "<configured proxy>"


def resolve_proxy(proxy: str | None, proxy_file: str | None = None) -> str | None:
    """Resolve proxy input without requiring credentials in process arguments."""

    if proxy:
        return proxy
    if proxy_file:
        path = Path(proxy_file).expanduser()
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"proxy file is empty: {path.resolve()}")
        return value
    value = os.getenv("XIANYU_PROXY", "").strip()
    return value or None


def _resolve_browser_channel(
    explicit_channel: str | None,
    cdp_user_data_dir: str | None,
) -> str | None:
    """Apply the environment default only when CDP was not selected explicitly."""

    if explicit_channel:
        return explicit_channel
    if cdp_user_data_dir:
        return None
    configured = os.getenv("XIANYU_BROWSER_CHANNEL", "").strip()
    return configured or None


def _load_state_file(
    state_file: str | None,
) -> tuple[dict[str, Any] | str | None, dict[str, Any], dict[str, str]]:
    """Load standard Playwright state or an enhanced browser snapshot."""

    if not state_file:
        return None, {}, {}

    path = Path(state_file).expanduser()
    if not path.is_file():
        raise StateFileError(f"browser state not found: {path}")

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateFileError(f"invalid browser state {path}: {exc}") from exc

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cookies"), list):
        raise StateFileError(
            "browser state must be a JSON object containing a cookies array"
        )

    enhanced = any(key in snapshot for key in ("env", "headers", "page", "storage"))
    if not enhanced:
        try:
            storage_state = _filter_goofish_storage_state(snapshot)
        except StorageStateValidationError as exc:
            raise StateFileError("invalid browser state schema") from exc
        if not _has_storage_state_material(storage_state):
            raise StateFileError(
                "browser state contains no usable Goofish Cookie or origin storage data"
            )
        return storage_state, {}, {}

    storage = snapshot.get("storage")
    origins = snapshot.get("origins", [])
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

    env = snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {}
    headers = (
        snapshot.get("headers") if isinstance(snapshot.get("headers"), dict) else {}
    )
    navigator = env.get("navigator") if isinstance(env.get("navigator"), dict) else {}
    intl = env.get("intl") if isinstance(env.get("intl"), dict) else {}

    overrides: dict[str, Any] = {}
    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    if isinstance(accept_language, str) and accept_language:
        overrides["locale"] = accept_language.split(",", 1)[0].strip()
    elif isinstance(navigator.get("language"), str):
        overrides["locale"] = navigator["language"]

    timezone = intl.get("timeZone")
    if isinstance(timezone, str) and timezone:
        overrides["timezone_id"] = timezone

    allowed_headers = {"accept", "accept-language", "cache-control", "pragma"}
    safe_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if isinstance(key, str)
        and value is not None
        and not key.startswith(":")
        and key.lower() in allowed_headers
    }
    return storage_state, overrides, safe_headers


def _has_storage_state_material(storage_state: dict[str, Any]) -> bool:
    """Return whether a browser state contains data, without inferring login."""

    cookies = storage_state.get("cookies")
    if isinstance(cookies, list):
        for cookie in cookies:
            if (
                isinstance(cookie, dict)
                and isinstance(cookie.get("name"), str)
                and cookie["name"].strip()
                and isinstance(cookie.get("value"), str)
                and cookie["value"]
            ):
                return True

    origins = storage_state.get("origins")
    if not isinstance(origins, list):
        return False
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        for storage_key in ("localStorage", "indexedDB"):
            entries = origin.get(storage_key)
            if isinstance(entries, list) and entries:
                return True
    return False


def _default_context() -> dict[str, Any]:
    """Use a desktop context that matches Xianyu's PC search API."""

    return {
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "color_scheme": "light",
        "viewport": {"width": 1440, "height": 900},
    }


def _context_options(
    storage_state: dict[str, Any] | str | None,
    overrides: dict[str, Any],
    extra_headers: dict[str, str],
) -> dict[str, Any]:
    options = _default_context()
    # The captured endpoint is the PC search API. Preserve only regional
    # metadata from enhanced snapshots so a legacy mobile snapshot cannot
    # silently switch the browser back to the incompatible mobile route.
    options.update(
        {key: overrides[key] for key in ("locale", "timezone_id") if key in overrides}
    )
    if storage_state is not None:
        options["storage_state"] = storage_state
    if extra_headers:
        options["extra_http_headers"] = extra_headers
    # Playwright routes cannot reliably observe requests handled by a service
    # worker, so blocking workers is part of the collector contract.
    options["service_workers"] = "block"
    return options


def _safe_page_location(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return "<unknown page>"
    safe_host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        return "<unknown page>"
    port_suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{safe_host}{port_suffix}"


def _validate_search_navigation(
    *,
    url: str,
    status: int | None,
    state_supplied: bool,
) -> None:
    parsed = urlsplit(url)
    path = parsed.path.lower()
    trusted_search_origin = _is_exact_https_origin(url, "www.goofish.com")
    trusted_login_origin = _is_exact_https_origin(url, "passport.goofish.com")
    if trusted_login_origin or (trusted_search_origin and "login" in path):
        if state_supplied:
            raise StateRejectedError("Xianyu did not accept the supplied browser state")
        raise LoginRequiredError("Xianyu login is required; provide --state")
    if not trusted_search_origin or not path.startswith("/search"):
        raise SearchCaptureError(
            f"unexpected Xianyu search navigation: {_safe_page_location(url)}"
        )
    if status is not None and not 200 <= status < 300:
        raise SearchRejectedError(f"Xianyu search page returned HTTP {status}")


async def _navigate_to_search(
    page: Page,
    *,
    search_url: str,
    timeout_ms: int,
    state_supplied: bool,
    headless: bool,
) -> None:
    try:
        navigation = await page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        _validate_search_navigation(
            url=page.url,
            status=None,
            state_supplied=state_supplied,
        )
        headed_hint = "; retry once with --headed" if headless else ""
        raise SearchCaptureError(
            "timed out loading Xianyu search page; "
            f"final page: {_safe_page_location(page.url)}"
            f"{headed_hint}"
        ) from exc

    _validate_search_navigation(
        url=page.url,
        status=navigation.status if navigation else None,
        state_supplied=state_supplied,
    )


class XianyuSpider:
    """Search Xianyu using optional private browser state."""

    def __init__(
        self,
        state_file: str | None = None,
        proxy: str | None = None,
        *,
        headless: bool = True,
        browser_channel: str | None = None,
        cdp_user_data_dir: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        verbose: bool = True,
    ):
        if browser_channel and cdp_user_data_dir:
            raise ValueError(
                "--browser-channel and --cdp-user-data-dir are mutually exclusive"
            )
        if cdp_user_data_dir and not state_file:
            raise ValueError(
                "--cdp-user-data-dir requires --state; capture candidate state first"
            )
        self.state_file = state_file
        self.proxy_settings = build_proxy_settings(proxy) if proxy else None
        self.headless = headless
        self.browser_channel = browser_channel
        self.cdp_user_data_dir = (
            str(_private_cdp_profile_path(cdp_user_data_dir))
            if cdp_user_data_dir
            else None
        )
        if self.cdp_user_data_dir and state_file:
            state_path = Path(state_file).expanduser().resolve(strict=False)
            if state_path.is_relative_to(Path(self.cdp_user_data_dir)):
                raise ValueError("--state must be outside the CDP profile")
        self.timeout_ms = timeout_ms
        self.verbose = verbose
        self.rate_limiter = RateLimiter()
        self.pages_scraped = 0
        self.debug = False
        self.last_capability_status = "not-established"

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)

    def _safe_error_message(self, error: Exception) -> str:
        message = str(error)
        for url in re.findall(r"""https?://[^\s"'<>]+""", message):
            message = message.replace(url, _safe_page_location(url))
        if self.proxy_settings:
            for field in ("username", "password"):
                credential = self.proxy_settings.get(field)
                if credential:
                    message = message.replace(credential, "<redacted>")
        return message

    async def search(
        self,
        keyword: str,
        max_price: float | None = None,
        min_price: float | None = None,
        location: str | None = None,
        pages: int = 1,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """Search, paginate, normalize, deduplicate, and locally filter items."""

        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword must not be empty")
        if pages < 1:
            raise ValueError("pages must be at least 1")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if min_price is not None and min_price < 0:
            raise ValueError("min_price must not be negative")
        if max_price is not None and max_price < 0:
            raise ValueError("max_price must not be negative")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("min_price must not exceed max_price")

        last_error: str | None = None
        self.last_capability_status = "not-established"
        for attempt in range(max_retries):
            try:
                items = await self._search_once(keyword, pages)
                self.last_capability_status = "passed-for-this-run"
                try:
                    return self._filter_items(
                        items,
                        min_price=min_price,
                        max_price=max_price,
                        location=location,
                    )
                except (KeyboardInterrupt, asyncio.CancelledError) as exc:
                    setattr(exc, "search_passed", True)
                    setattr(exc, "capability_status", "passed-for-this-run")
                    raise
            except asyncio.CancelledError as exc:
                cleanup_failures = getattr(exc, "cleanup_failures", None)
                if isinstance(cleanup_failures, list) and cleanup_failures:
                    capability_status = search_capability_status(exc)
                    self.last_capability_status = capability_status
                    cleanup_message = (
                        "search completed but browser cleanup was interrupted"
                        if capability_status == "passed-for-this-run"
                        else "search was cancelled; browser cleanup was incomplete"
                    )
                    terminal_error = SearchCancelledError(
                        cleanup_message,
                        capability_status=capability_status,
                    )
                    terminal_error.cleanup_failures = list(cleanup_failures)
                    raise terminal_error from exc
                raise
            except (
                BrowserCleanupError,
                BrowserConnectionError,
                SearchCaptureError,
                SearchRejectedError,
                StateFileError,
                DependencyError,
            ) as exc:
                self.last_capability_status = search_capability_status(exc)
                raise
            except (PlaywrightError, SpiderError) as exc:
                cleanup_failures = getattr(exc, "cleanup_failures", None)
                if isinstance(cleanup_failures, list) and cleanup_failures:
                    if isinstance(exc, SpiderError):
                        raise
                    terminal_error = SpiderError(
                        "search failed and browser cleanup was incomplete"
                    )
                    terminal_error.cleanup_failures = list(cleanup_failures)
                    raise terminal_error from None
                last_error = self._safe_error_message(exc)
                if attempt >= max_retries - 1:
                    break
                wait_seconds = min(5 * (2**attempt), 30)
                self._log(
                    f"[retry] search failed; retrying in {wait_seconds}s "
                    f"({attempt + 1}/{max_retries}): {last_error}"
                )
                await asyncio.sleep(wait_seconds)

        raise SpiderError(
            f"search failed after {max_retries} attempt(s): {last_error}"
        ) from None

    async def _search_once(self, keyword: str, pages: int) -> list[dict[str, Any]]:
        if async_playwright is None:
            raise DependencyError(
                "playwright is not installed; run: "
                "python -m pip install -r requirements.txt && "
                "python -m playwright install chromium"
            )

        if self.cdp_user_data_dir and self.state_file:
            current_state = Path(self.state_file).expanduser().resolve(strict=False)
            if current_state.is_relative_to(Path(self.cdp_user_data_dir)):
                raise ValueError("--state must be outside the CDP profile")
        storage_state, context_overrides, extra_headers = _load_state_file(
            self.state_file
        )
        if storage_state is None:
            self._log("[warning] no browser state supplied; Xianyu may require login")

        self.pages_scraped = 0
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        playwright_manager = async_playwright()
        playwright: Any | None = None
        browser: Any | None = None
        context: Any | None = None
        try:
            try:
                playwright = await playwright_manager.start()
            except BaseException as exc:  # noqa: BLE001
                # start() can initialize the manager before raising (including
                # cancellation before its return value is assigned).
                await _cleanup_interrupted_playwright_start(playwright_manager, exc)
                raise

            if self.cdp_user_data_dir:
                cdp_endpoint = _cdp_endpoint_from_user_data_dir(
                    self.cdp_user_data_dir,
                    timeout_seconds=0,
                )
                try:
                    browser = await playwright.chromium.connect_over_cdp(
                        cdp_endpoint,
                        timeout=min(max(self.timeout_ms, 1), 60_000),
                    )
                except PlaywrightError as exc:
                    raise BrowserConnectionError(
                        "unable to connect to the dedicated local Chrome browser"
                    ) from exc
                await _verify_async_cdp_profile(
                    browser,
                    Path(self.cdp_user_data_dir),
                )
                self._log("[browser] connected to dedicated local Chrome")
            else:
                launch_kwargs: dict[str, Any] = {"headless": self.headless}
                if self.proxy_settings:
                    launch_kwargs["proxy"] = self.proxy_settings
                    self._log(f"[proxy] using {self.proxy_settings['server']}")
                if self.browser_channel:
                    launch_kwargs["channel"] = self.browser_channel
                browser = await playwright.chromium.launch(**launch_kwargs)
            if browser is not None:
                context_kwargs = _context_options(
                    storage_state,
                    context_overrides,
                    extra_headers,
                )
                if self.cdp_user_data_dir and self.proxy_settings:
                    context_kwargs["proxy"] = self.proxy_settings
                    self._log(f"[proxy] using {self.proxy_settings['server']}")

                context = await browser.new_context(**context_kwargs)
                collector = SearchResponseCollector(keyword)
                await collector.install(context)
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                search_url = f"{BASE_URL}/search?{urlencode({'q': keyword})}"
                self._log(f"[search] {keyword!r}, page 1/{pages}")
                ticket = collector.arm(1)
                try:
                    await _navigate_to_search(
                        page,
                        search_url=search_url,
                        timeout_ms=self.timeout_ms,
                        state_supplied=storage_state is not None,
                        headless=self.headless and not self.cdp_user_data_dir,
                    )
                    try:
                        capture = await collector.next(ticket, self.timeout_ms)
                    except SearchCaptureError as exc:
                        _validate_search_navigation(
                            url=page.url,
                            status=None,
                            state_supplied=storage_state is not None,
                        )
                        headed_hint = (
                            "; Xianyu may suppress search requests in headless mode; "
                            "retry once with --headed"
                            if self.headless and not self.cdp_user_data_dir
                            else ""
                        )
                        raise SearchCaptureError(
                            f"{exc}; final page: {_safe_page_location(page.url)}"
                            f"{headed_hint}"
                        ) from exc
                finally:
                    collector.disarm(ticket)

                for page_number in range(1, pages + 1):
                    page_items = self._parse_capture(capture)
                    self.pages_scraped += 1
                    for item in page_items:
                        item_id = item["id"]
                        if item_id not in seen_ids:
                            seen_ids.add(item_id)
                            items.append(item)

                    if page_number >= pages:
                        break
                    capture = await self._advance_page(
                        page, collector, page_number + 1, pages
                    )
                    if capture is None:
                        break
        finally:
            try:
                if self.cdp_user_data_dir and context is not None:
                    primary_error = sys.exc_info()[1]
                    try:
                        await context.close()
                    except BaseException as exc:  # noqa: BLE001
                        message = "failed to close the isolated search context"
                        if not isinstance(exc, Exception):
                            capability_status = (
                                search_capability_status(primary_error)
                                if primary_error is not None
                                else "passed-for-this-run"
                            )
                            self.last_capability_status = capability_status
                            setattr(exc, "capability_status", capability_status)
                            if primary_error is not None:
                                for failure in getattr(
                                    primary_error,
                                    "cleanup_failures",
                                    [],
                                ):
                                    _append_cleanup_failure(exc, failure)
                                if capability_status == "passed-for-this-run":
                                    setattr(exc, "search_passed", True)
                            else:
                                setattr(exc, "search_passed", True)
                            _append_cleanup_failure(exc, message)
                            raise
                        if primary_error is not None:
                            _append_cleanup_failure(primary_error, message)
                        else:
                            raise BrowserCleanupError(
                                message,
                                search_passed=True,
                            ) from exc
            finally:
                try:
                    if browser is not None:
                        primary_error = sys.exc_info()[1]
                        try:
                            await browser.close()
                        except BaseException as exc:  # noqa: BLE001
                            message = (
                                "failed to disconnect from the connected search browser"
                                if self.cdp_user_data_dir
                                else "failed to close the dedicated search browser"
                            )
                            if not isinstance(exc, Exception):
                                capability_status = (
                                    search_capability_status(primary_error)
                                    if primary_error is not None
                                    else "passed-for-this-run"
                                )
                                self.last_capability_status = capability_status
                                setattr(exc, "capability_status", capability_status)
                                if primary_error is not None:
                                    for failure in getattr(
                                        primary_error,
                                        "cleanup_failures",
                                        [],
                                    ):
                                        _append_cleanup_failure(exc, failure)
                                    if capability_status == "passed-for-this-run":
                                        setattr(exc, "search_passed", True)
                                else:
                                    setattr(exc, "search_passed", True)
                                _append_cleanup_failure(exc, message)
                                raise
                            if primary_error is not None:
                                _append_cleanup_failure(primary_error, message)
                            else:
                                raise BrowserCleanupError(
                                    message,
                                    search_passed=True,
                                ) from exc
                finally:
                    if playwright is not None:
                        primary_error = sys.exc_info()[1]
                        try:
                            await playwright.stop()
                        except BaseException as exc:  # noqa: BLE001
                            message = "failed to stop the dedicated browser runtime"
                            if not isinstance(exc, Exception):
                                capability_status = (
                                    search_capability_status(primary_error)
                                    if primary_error is not None
                                    else "passed-for-this-run"
                                )
                                self.last_capability_status = capability_status
                                setattr(exc, "capability_status", capability_status)
                                if primary_error is not None:
                                    for failure in getattr(
                                        primary_error,
                                        "cleanup_failures",
                                        [],
                                    ):
                                        _append_cleanup_failure(exc, failure)
                                    if capability_status == "passed-for-this-run":
                                        setattr(exc, "search_passed", True)
                                else:
                                    setattr(exc, "search_passed", True)
                                _append_cleanup_failure(exc, message)
                                raise
                            if primary_error is not None:
                                _append_cleanup_failure(primary_error, message)
                            else:
                                raise BrowserCleanupError(
                                    message,
                                    search_passed=True,
                                ) from exc

        return items

    async def _advance_page(
        self,
        page: Page,
        collector: SearchResponseCollector,
        page_number: int,
        total_pages: int,
    ) -> CapturedSearchResponse | None:
        next_button = page.locator(NEXT_PAGE_SELECTOR).first
        try:
            await next_button.wait_for(
                state="visible",
                timeout=min(self.timeout_ms, PAGINATION_WAIT_MS),
            )
        except PlaywrightTimeoutError:
            self._log("[pagination] reached the last page")
            return None

        await self.rate_limiter.wait()
        self._log(f"[search] requesting page {page_number}/{total_pages}")
        await next_button.scroll_into_view_if_needed()
        ticket = collector.arm(page_number)
        try:
            await next_button.click(timeout=self.timeout_ms)
            return await collector.next(ticket, self.timeout_ms)
        finally:
            collector.disarm(ticket)

    def _parse_capture(self, capture: CapturedSearchResponse) -> list[dict[str, Any]]:
        if capture.error:
            raise SearchCaptureError(capture.error)
        if capture.status is None or not 200 <= capture.status < 300:
            raise SearchRejectedError(
                f"search API returned HTTP {capture.status or 'unknown'}"
            )
        if not capture.payload:
            raise SearchCaptureError("search API returned an empty payload")

        ret = capture.payload.get("ret")
        ret_values = ret if isinstance(ret, list) else [ret]
        if not ret_values or any(
            not isinstance(value, str) or not value for value in ret_values
        ):
            raise SearchCaptureError("search API response has malformed ret markers")
        failures = [
            value for value in ret_values if not value.upper().startswith("SUCCESS::")
        ]
        if failures:
            raise SearchRejectedError("; ".join(failures))

        data = capture.payload.get("data")
        if not isinstance(data, dict):
            raise SearchCaptureError("search API data is not an object")
        if "resultList" not in data:
            raise SearchCaptureError("search API resultList is missing")
        result_list = data["resultList"]
        if not isinstance(result_list, list):
            raise SearchCaptureError("search API resultList is not a list")

        parsed: list[dict[str, Any]] = []
        for wrapper in result_list:
            if not isinstance(wrapper, dict):
                raise SearchCaptureError(
                    "search API resultList contains a non-object entry"
                )
            item = self._parse_api_item(wrapper)
            if item is None:
                raise SearchCaptureError(
                    "search API resultList contains an unrecognized listing"
                )
            parsed.append(item)
        return parsed

    @staticmethod
    def _filter_items(
        items: list[dict[str, Any]],
        *,
        min_price: float | None,
        max_price: float | None,
        location: str | None,
    ) -> list[dict[str, Any]]:
        location_query = location.strip().casefold() if location else None
        filtered: list[dict[str, Any]] = []
        for item in items:
            price = item.get("price")
            if min_price is not None and (
                not isinstance(price, (int, float)) or price < min_price
            ):
                continue
            if max_price is not None and (
                not isinstance(price, (int, float)) or price > max_price
            ):
                continue
            if (
                location_query
                and location_query not in str(item.get("location", "")).casefold()
            ):
                continue
            filtered.append(item)
        return filtered

    def _parse_api_item(self, wrapper: dict[str, Any]) -> dict[str, Any] | None:
        try:
            data = wrapper.get("data", {})
            if not isinstance(data, dict):
                return None

            item = data.get("item")
            main = item.get("main", {}) if isinstance(item, dict) else {}
            nested = isinstance(main, dict) and isinstance(main.get("exContent"), dict)
            if nested:
                ex_content = main["exContent"]
                click_args = main.get("clickParam", {}).get("args", {})
            else:
                main = data
                ex_content = data
                click_args = data.get("clickParam", {}).get("args", {})
            if not isinstance(ex_content, dict):
                return None
            if not isinstance(click_args, dict):
                click_args = {}

            item_id = str(
                ex_content.get("itemId") or ex_content.get("id") or ""
            ).strip()
            if not item_id:
                return None

            price_text = self._price_text(ex_content.get("price"))

            raw_link = str(
                main.get("targetUrl")
                or main.get("itemUrl")
                or ex_content.get("targetUrl")
                or ""
            )
            url = raw_link.replace("fleamarket://", f"{BASE_URL}/", 1)
            if not url:
                url = f"{BASE_URL}/item?id={item_id}"

            published = self._format_timestamp(
                click_args.get("publishTime") or ex_content.get("publishTime")
            )
            tags: list[str] = []
            if click_args.get("tag") == "freeship":
                tags.append("包邮")
            fish_tags = ex_content.get("fishTags")
            if isinstance(fish_tags, dict):
                rank_one = fish_tags.get("r1")
                tag_list = (
                    rank_one.get("tagList", []) if isinstance(rank_one, dict) else []
                )
            elif isinstance(fish_tags, list):
                tag_list = fish_tags
            else:
                tag_list = []
            if isinstance(tag_list, list):
                for tag_item in tag_list:
                    if isinstance(tag_item, dict):
                        tag_data = tag_item.get("data")
                        content = str(
                            (
                                tag_data.get("content")
                                if isinstance(tag_data, dict)
                                else None
                            )
                            or tag_item.get("content")
                            or ""
                        )
                    else:
                        content = str(tag_item)
                    if "验货宝" in content and "验货宝" not in tags:
                        tags.append("验货宝")

            return {
                "id": item_id,
                "title": str(ex_content.get("title") or ""),
                "price": self._parse_price(price_text),
                "url": url,
                "image": str(ex_content.get("picUrl") or ""),
                "location": str(ex_content.get("area") or ex_content.get("city") or ""),
                "seller": str(
                    ex_content.get("userNickName") or ex_content.get("userNick") or ""
                ),
                "publish_time": published,
                "wants": click_args.get("wantNum", ex_content.get("wantNum", 0)),
                "tags": tags,
            }
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _price_text(raw_price: Any) -> str:
        if isinstance(raw_price, list):
            return "".join(
                str(part.get("text", ""))
                for part in raw_price
                if isinstance(part, dict)
            )
        if isinstance(raw_price, dict):
            return str(
                raw_price.get("text")
                or raw_price.get("price")
                or raw_price.get("value")
                or ""
            )
        return str(raw_price or "")

    @staticmethod
    def _parse_price(price_text: str) -> int | float | None:
        import re

        match = re.search(r"\d+(?:\.\d+)?", price_text.replace(",", ""))
        if not match:
            return None
        value = float(match.group(0))
        return int(value) if value.is_integer() else value

    @staticmethod
    def _format_timestamp(raw_timestamp: Any) -> str:
        try:
            timestamp = int(raw_timestamp)
        except (TypeError, ValueError):
            return ""
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=SHANGHAI_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Xianyu and emit JSON")
    parser.add_argument("--keyword", "-k", required=True, help="search keyword")
    parser.add_argument("--max-price", type=float, help="maximum price")
    parser.add_argument("--min-price", type=float, help="minimum price")
    parser.add_argument("--location", help="location substring")
    parser.add_argument("--pages", "-p", type=int, default=1, help="pages to fetch")
    parser.add_argument("--state", "-s", help="Playwright browser-state JSON")
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--proxy",
        help="HTTP(S) or SOCKS proxy URL; may be visible in process arguments",
    )
    proxy_group.add_argument(
        "--proxy-file",
        help="read proxy URL from a user-private UTF-8 file",
    )
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--browser-channel",
        help=(
            "Playwright browser executable channel, for example chrome; "
            "does not reuse an existing Chrome profile"
        ),
    )
    browser_group.add_argument(
        "--cdp-user-data-dir",
        help=(
            "connect through DevToolsActivePort in an explicitly dedicated, "
            "private Chrome user-data directory; requires --state"
        ),
    )
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--debug", action="store_true", help="enable debug metadata")
    parser.add_argument(
        "--quiet", action="store_true", help="suppress routine diagnostic logs"
    )
    parser.add_argument("--retries", "-r", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    browser_channel = _resolve_browser_channel(
        args.browser_channel,
        args.cdp_user_data_dir,
    )
    spider: XianyuSpider | None = None
    try:
        spider = XianyuSpider(
            state_file=args.state,
            proxy=resolve_proxy(args.proxy, args.proxy_file),
            headless=not args.headed,
            browser_channel=browser_channel,
            cdp_user_data_dir=args.cdp_user_data_dir,
            verbose=not args.quiet,
        )
        spider.debug = args.debug
        results = asyncio.run(
            spider.search(
                keyword=args.keyword,
                max_price=args.max_price,
                min_price=args.min_price,
                location=args.location,
                pages=args.pages,
                max_retries=args.retries,
            )
        )
        payload: dict[str, Any] = {
            "ok": True,
            "keyword": args.keyword,
            "count": len(results),
            "pages_scraped": spider.pages_scraped,
            "items": results,
            "search_capability": {"status": "passed-for-this-run"},
            "authentication": {"status": "not-evaluated"},
            "identity": {"status": "not-evaluated"},
            "cleanup": cleanup_evidence(),
        }
        if args.debug:
            payload["filters"] = {
                "min_price": args.min_price,
                "max_price": args.max_price,
                "location": args.location,
            }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0  # noqa: TRY300 - success emission must stay cancellation-protected.
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        capability_status = search_capability_status(exc)
        if capability_status == "not-established" and spider is not None:
            capability_status = getattr(
                spider,
                "last_capability_status",
                "not-established",
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "keyword": args.keyword,
                    "error": "search cancelled",
                    "error_type": type(exc).__name__,
                    "search_capability": {"status": capability_status},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup_evidence(exc),
                },
                ensure_ascii=True,
            )
        )
        return 130
    except SearchCancelledError as exc:
        capability_status = search_capability_status(exc)
        if capability_status == "not-established" and spider is not None:
            capability_status = getattr(
                spider,
                "last_capability_status",
                "not-established",
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "keyword": args.keyword,
                    "error": "search cancelled; browser cleanup was incomplete",
                    "error_type": type(exc).__name__,
                    "search_capability": {"status": capability_status},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup_evidence(exc),
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (OSError, SpiderError, ValueError) as exc:
        capability_status = search_capability_status(exc)
        if capability_status == "not-established" and spider is not None:
            capability_status = getattr(
                spider,
                "last_capability_status",
                "not-established",
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "keyword": args.keyword,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "search_capability": {"status": capability_status},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup_evidence(exc),
                },
                ensure_ascii=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
