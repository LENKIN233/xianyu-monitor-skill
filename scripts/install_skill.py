#!/usr/bin/env python3
"""Install this skill into common Agent Skills discovery directories."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .distribution import (
        BUNDLE_FILES,
        INSTALL_MANIFEST,
        OPTIONAL_PROVENANCE_FILES,
    )
    from .version_info import read_version
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from distribution import BUNDLE_FILES, INSTALL_MANIFEST, OPTIONAL_PROVENANCE_FILES
    from version_info import read_version

SKILL_NAME = "xianyu-monitor"
INSTALL_MANIFEST_SCHEMA = 1
MAX_DISTRIBUTABLE_FILE_BYTES = 16 * 1024 * 1024
MAX_INSTALL_MANIFEST_BYTES = 2 * 1024 * 1024
HOST_ROOTS = {
    "codex": Path(".agents/skills"),
    "claude": Path(".claude/skills"),
    # Current OpenClaw releases also discover the shared Agent Skills root.
    "openclaw": Path(".agents/skills"),
}
REQUIRED_COPY_FILES = BUNDLE_FILES
COPY_FILES = (*BUNDLE_FILES, *OPTIONAL_PROVENANCE_FILES)


@dataclass
class InstallProgress:
    """Path-private evidence for one multi-target install transaction."""

    mode: str
    dry_run: bool
    targets: list[dict[str, Any]] = field(default_factory=list)
    cleanup_failures: list[str] = field(default_factory=list)

    def configure(self, selections: list[dict[str, Any]]) -> None:
        self.targets = [
            {
                "target": selection["target"],
                "hosts": list(selection["hosts"]),
                "status": "not-installed",
            }
            for selection in selections
        ]

    def set_status(self, target: Path, status: str) -> None:
        for record in self.targets:
            if record["target"] == target:
                record["status"] = status
                return

    def add_cleanup_failure(self, message: str) -> None:
        if message not in self.cleanup_failures:
            self.cleanup_failures.append(message)

    def public_evidence(self) -> dict[str, Any]:
        public_statuses = [
            {
                "planned": "not-installed",
                "already-installed": "installed",
            }.get(str(record["status"]), str(record["status"]))
            for record in self.targets
        ]
        if self.dry_run:
            overall_status = "not-installed"
        elif public_statuses and all(
            status == "installed" for status in public_statuses
        ):
            overall_status = "installed"
        elif any(status == "not-established" for status in public_statuses):
            overall_status = "not-established"
        else:
            overall_status = "not-installed"
        return {
            "installation": {"status": overall_status},
            "installs": [
                {
                    "hosts": list(record["hosts"]),
                    "mode": self.mode,
                    "status": public_status,
                }
                for record, public_status in zip(
                    self.targets,
                    public_statuses,
                    strict=True,
                )
            ],
            "cleanup": (
                {
                    "status": "failed",
                    "errors": list(self.cleanup_failures),
                }
                if self.cleanup_failures
                else {"status": "complete-or-not-required"}
            ),
        }


@dataclass(frozen=True)
class TargetOwnership:
    """Filesystem identity retained when this invocation creates a target."""

    target: Path
    mode: str
    device: int
    inode: int


@dataclass(frozen=True)
class ParentOwnership:
    """Filesystem identity for a parent directory created by this invocation."""

    path: Path
    device: int
    inode: int


def _read_skill_name(skill_file: Path) -> str:
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"invalid SKILL.md frontmatter: {skill_file}")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"SKILL.md has no name: {skill_file}")


def _selected_targets(hosts: list[str], home: Path) -> list[dict[str, Any]]:
    selected_hosts = list(HOST_ROOTS) if "all" in hosts else hosts
    targets: dict[Path, list[str]] = {}
    for host in selected_hosts:
        root = home / HOST_ROOTS[host]
        target = root / SKILL_NAME
        targets.setdefault(target, []).append(host)
    return [
        {"target": target, "hosts": target_hosts}
        for target, target_hosts in targets.items()
    ]


def _validate_staged_copy(staging: Path) -> None:
    missing_staged = [
        relative_name
        for relative_name in REQUIRED_COPY_FILES
        if not (staging / relative_name).is_file()
    ]
    if missing_staged:
        raise ValueError(f"missing required staged resource: {missing_staged[0]}")


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    """Read one bounded regular non-symlink file for distribution evidence."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("distributable resource is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("distributable resources must be regular non-symlink files")
    if metadata.st_size > maximum:
        raise ValueError("distributable resource exceeds the safety limit")
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except OSError as exc:
        raise ValueError("distributable resource is unreadable") from exc
    if len(payload) > maximum:
        raise ValueError("distributable resource exceeds the safety limit")
    return payload


def _validate_source_payload(source: Path) -> None:
    provenance_present = {
        relative_name
        for relative_name in OPTIONAL_PROVENANCE_FILES
        if _path_exists(source / relative_name)
    }
    if provenance_present and provenance_present != set(OPTIONAL_PROVENANCE_FILES):
        raise ValueError("release provenance must be complete or absent")
    for relative_name in BUNDLE_FILES:
        _read_regular_file(
            source / relative_name,
            maximum=MAX_DISTRIBUTABLE_FILE_BYTES,
        )
    for relative_name in provenance_present:
        _read_regular_file(
            source / relative_name,
            maximum=MAX_DISTRIBUTABLE_FILE_BYTES,
        )


def _install_manifest_payload(staging: Path) -> dict[str, Any]:
    files = []
    recorded_files = [
        *BUNDLE_FILES,
        *(
            relative_name
            for relative_name in OPTIONAL_PROVENANCE_FILES
            if (staging / relative_name).is_file()
        ),
    ]
    for relative_name in recorded_files:
        payload = _read_regular_file(
            staging / relative_name,
            maximum=MAX_DISTRIBUTABLE_FILE_BYTES,
        )
        files.append(
            {
                "path": relative_name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return {
        "schema_version": INSTALL_MANIFEST_SCHEMA,
        "skill": {"name": SKILL_NAME, "version": read_version(staging)},
        "files": files,
    }


def _write_install_manifest(staging: Path) -> None:
    payload = (
        json.dumps(
            _install_manifest_payload(staging),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
    destination = staging / INSTALL_MANIFEST
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _capture_target_ownership(
    observed_path: Path,
    mode: str,
    *,
    target: Path | None = None,
) -> TargetOwnership:
    observed = observed_path.lstat()
    expected_type = (
        stat.S_ISLNK(observed.st_mode)
        if mode == "symlink"
        else stat.S_ISDIR(observed.st_mode)
    )
    if not expected_type:
        raise OSError("created installation target has an unexpected type")
    return TargetOwnership(
        target=observed_path if target is None else target,
        mode=mode,
        device=observed.st_dev,
        inode=observed.st_ino,
    )


def _target_matches_ownership(
    target: Path,
    ownership: TargetOwnership,
) -> bool:
    if ownership.target != target:
        return False
    return _path_matches_target_identity(target, ownership)


def _path_matches_target_identity(
    path: Path,
    ownership: TargetOwnership,
) -> bool:
    try:
        observed = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    expected_type = (
        stat.S_ISLNK(observed.st_mode)
        if ownership.mode == "symlink"
        else stat.S_ISDIR(observed.st_mode)
    )
    return (
        expected_type
        and observed.st_dev == ownership.device
        and observed.st_ino == ownership.inode
    )


def _capture_parent_ownership(
    observed_path: Path,
    *,
    path: Path | None = None,
) -> ParentOwnership:
    observed = observed_path.lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise OSError("created installation parent has an unexpected type")
    return ParentOwnership(
        path=observed_path if path is None else path,
        device=observed.st_dev,
        inode=observed.st_ino,
    )


def _parent_matches_ownership(ownership: ParentOwnership) -> bool:
    return _path_matches_parent_identity(ownership.path, ownership)


def _path_matches_parent_identity(
    path: Path,
    ownership: ParentOwnership,
) -> bool:
    try:
        observed = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_dev == ownership.device
        and observed.st_ino == ownership.inode
    )


def _require_parent_identity(
    path: Path,
    ownership: ParentOwnership,
) -> None:
    if not _path_matches_parent_identity(path, ownership):
        raise OSError("installation parent changed during publication")


def _owned_parent_chain(
    parent: Path,
    created_parents: Sequence[ParentOwnership],
) -> tuple[ParentOwnership, ...]:
    parent_chain = {parent, *parent.parents}
    return tuple(
        ownership for ownership in created_parents if ownership.path in parent_chain
    )


def _validate_owned_parents(
    ownerships: Sequence[ParentOwnership],
) -> None:
    if any(not _parent_matches_ownership(ownership) for ownership in ownerships):
        raise OSError("installation parent changed before publication")


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _supports_safe_quarantine_restore() -> bool:
    # Windows rename fails when the destination already exists. POSIX rename
    # can replace an empty directory or symlink and is therefore unsafe here.
    return os.name == "nt"


def _restore_quarantined_path(quarantined: Path, destination: Path) -> bool:
    """Best-effort restoration after quarantine identity does not match."""

    if not _supports_safe_quarantine_restore():
        return False
    try:
        quarantined.rename(destination)
    except OSError:
        return False
    return True


def _atomic_rename_noreplace(staged: Path, target: Path) -> None:
    """Atomically publish a staged object without replacing any target."""

    if os.name == "nt":
        staged.rename(target)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    staged_bytes = os.fsencode(staged)
    target_bytes = os.fsencode(target)
    ctypes.set_errno(0)
    if sys.platform == "darwin":
        try:
            rename_call = libc.renamex_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace publication is unavailable",
            ) from exc
        rename_call.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_call.restype = ctypes.c_int
        result = rename_call(staged_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename_call = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace publication is unavailable",
            ) from exc
        rename_call.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_call.restype = ctypes.c_int
        result = rename_call(-100, staged_bytes, -100, target_bytes, 1)
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace publication is unavailable",
        )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(
                error_number,
                os.strerror(error_number),
                str(target),
            )
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(target),
        )


def _private_staging_candidate(parent: Path, prefix: str) -> Path:
    """Choose an unguessable path before any filesystem object is created."""

    return parent / f"{prefix}{secrets.token_hex(16)}"


def _create_private_directory(path: Path) -> None:
    """Create a caller-observable private directory without replacing anything."""

    path.mkdir(mode=stat.S_IRWXU)


def _publish_staged_target(
    staged: Path,
    target: Path,
    mode: str,
    ownership_recorder: Callable[[TargetOwnership | None], None] | None,
) -> TargetOwnership:
    ownership = _capture_target_ownership(
        staged,
        mode,
        target=target,
    )
    if ownership_recorder is not None:
        ownership_recorder(ownership)
    try:
        _atomic_rename_noreplace(staged, target)
    except BaseException as publish_error:  # noqa: BLE001
        if ownership_recorder is not None and not _target_matches_ownership(
            target,
            ownership,
        ):
            ownership_recorder(None)
        if isinstance(publish_error, OSError) and _path_exists(target):
            raise FileExistsError(
                f"refusing to replace existing path: {target}"
            ) from publish_error
        raise
    if not _target_matches_ownership(target, ownership):
        raise OSError("installation target changed during publication")
    return ownership


def _remove_private_staging(
    staging: Path,
    cleanup_recorder: Callable[[str], None] | None,
    *,
    empty_only: bool = False,
) -> None:
    try:
        if empty_only:
            staging.rmdir()
        else:
            shutil.rmtree(staging)
    except FileNotFoundError:
        return
    except BaseException:  # noqa: BLE001
        if cleanup_recorder is not None:
            cleanup_recorder("failed to remove private installation staging")
        raise


def _reraise_after_staging_cleanup(
    primary_error: BaseException,
    primary_traceback: Any,
    staging: Path,
    cleanup_recorder: Callable[[str], None] | None,
    *,
    empty_only: bool = False,
) -> None:
    try:
        _remove_private_staging(
            staging,
            cleanup_recorder,
            empty_only=empty_only,
        )
    except BaseException as cleanup_error:  # noqa: BLE001
        if _is_interruption(primary_error):
            raise primary_error.with_traceback(primary_traceback)
        if _is_interruption(cleanup_error):
            raise cleanup_error from primary_error
        raise OSError("private installation staging cleanup failed") from primary_error
    raise primary_error.with_traceback(primary_traceback)


def _copy_skill(
    source: Path,
    target: Path,
    *,
    owned_parents: Sequence[ParentOwnership] = (),
    ownership_recorder: Callable[[TargetOwnership | None], None] | None = None,
    cleanup_recorder: Callable[[str], None] | None = None,
) -> None:
    _validate_owned_parents(owned_parents)
    temporary = _private_staging_candidate(
        target.parent,
        f".{SKILL_NAME}.install-",
    )
    staging_ready = False
    publish_attempted = False
    try:
        _create_private_directory(temporary)
        staging_ready = True
        _validate_owned_parents(owned_parents)
        for relative_name in COPY_FILES:
            source_entry = source / relative_name
            if not source_entry.is_file():
                continue
            target_entry = temporary / relative_name
            target_entry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_entry, target_entry)

        _validate_staged_copy(temporary)
        _write_install_manifest(temporary)
        publish_attempted = True
        _publish_staged_target(
            temporary,
            target,
            "copy",
            ownership_recorder,
        )
    except BaseException as primary_error:  # noqa: BLE001
        if isinstance(primary_error, FileExistsError) and not staging_ready:
            raise
        if (
            staging_ready
            and not publish_attempted
            and not _path_exists(temporary)
            and cleanup_recorder is not None
        ):
            cleanup_recorder(
                "private installation staging state could not be established"
            )
        _reraise_after_staging_cleanup(
            primary_error,
            primary_error.__traceback__,
            temporary,
            cleanup_recorder,
            empty_only=not staging_ready,
        )
    else:
        if _path_exists(temporary):
            _remove_private_staging(temporary, cleanup_recorder)


def _validate_published_symlink(
    target: Path,
    source: Path,
    ownership: TargetOwnership,
) -> None:
    if not _target_matches_ownership(target, ownership) or target.resolve() != source:
        raise OSError("installation symlink changed during publication")


def _symlink_skill(
    source: Path,
    target: Path,
    *,
    owned_parents: Sequence[ParentOwnership] = (),
    ownership_recorder: Callable[[TargetOwnership | None], None] | None = None,
    cleanup_recorder: Callable[[str], None] | None = None,
) -> None:
    _validate_owned_parents(owned_parents)
    temporary_parent = _private_staging_candidate(
        target.parent,
        f".{SKILL_NAME}.install-",
    )
    staging_ready = False
    staged = temporary_parent / "target"
    publish_attempted = False
    try:
        _create_private_directory(temporary_parent)
        staging_ready = True
        _validate_owned_parents(owned_parents)
        staged.symlink_to(source, target_is_directory=True)
        publish_attempted = True
        ownership = _publish_staged_target(
            staged,
            target,
            "symlink",
            ownership_recorder,
        )
        _validate_published_symlink(target, source, ownership)
    except BaseException as primary_error:  # noqa: BLE001
        if isinstance(primary_error, FileExistsError) and not staging_ready:
            raise
        if (
            staging_ready
            and not publish_attempted
            and not _path_exists(temporary_parent)
            and cleanup_recorder is not None
        ):
            cleanup_recorder(
                "private installation staging state could not be established"
            )
        _reraise_after_staging_cleanup(
            primary_error,
            primary_error.__traceback__,
            temporary_parent,
            cleanup_recorder,
            empty_only=not staging_ready,
        )
    else:
        _remove_private_staging(
            temporary_parent,
            cleanup_recorder,
            empty_only=True,
        )


def _remove_created_target(
    target: Path,
    mode: str,
    source: Path,
    ownership: TargetOwnership,
) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        return
    if ownership.mode != mode or not _target_matches_ownership(target, ownership):
        raise OSError(f"refusing to remove changed install target: {target}")
    quarantine_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.rollback-",
            dir=target.parent,
        )
    )
    quarantine_target = quarantine_parent / "target"
    deferred_rename_error: BaseException | None = None
    try:
        target.rename(quarantine_target)
    except BaseException as rename_error:  # noqa: BLE001
        if _path_matches_target_identity(quarantine_target, ownership):
            deferred_rename_error = rename_error
        else:
            restored = _path_exists(quarantine_target) and _restore_quarantined_path(
                quarantine_target, target
            )
            if restored or not _path_exists(quarantine_target):
                try:
                    quarantine_parent.rmdir()
                except OSError:
                    pass
            if (
                isinstance(rename_error, FileNotFoundError)
                and not _path_exists(target)
                and not _path_exists(quarantine_target)
            ):
                return
            raise
    if not _path_matches_target_identity(quarantine_target, ownership):
        if _restore_quarantined_path(quarantine_target, target):
            quarantine_parent.rmdir()
        raise OSError(f"refusing to remove changed install target: {target}")
    if mode == "symlink":
        if quarantine_target.resolve() != source or not _path_matches_target_identity(
            quarantine_target,
            ownership,
        ):
            raise OSError(f"refusing to remove changed install target: {target}")
        quarantine_target.unlink()
    else:
        if not _path_matches_target_identity(quarantine_target, ownership):
            raise OSError(f"refusing to remove changed install target: {target}")
        shutil.rmtree(quarantine_target)
    quarantine_parent.rmdir()
    if deferred_rename_error is not None:
        raise deferred_rename_error


def _remove_created_parent(ownership: ParentOwnership) -> None:
    parent = ownership.path
    if not _parent_matches_ownership(ownership):
        return
    try:
        if any(parent.iterdir()):
            return
    except FileNotFoundError:
        return
    if not _parent_matches_ownership(ownership):
        return

    quarantine_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{parent.name}.rollback-",
            dir=parent.parent,
        )
    )
    quarantine_target = quarantine_parent / "parent"
    deferred_rename_error: BaseException | None = None
    try:
        parent.rename(quarantine_target)
    except BaseException as rename_error:  # noqa: BLE001
        if _path_matches_parent_identity(quarantine_target, ownership):
            deferred_rename_error = rename_error
        else:
            restored = _path_exists(quarantine_target) and _restore_quarantined_path(
                quarantine_target, parent
            )
            if restored or not _path_exists(quarantine_target):
                try:
                    quarantine_parent.rmdir()
                except OSError:
                    pass
            if (
                isinstance(rename_error, FileNotFoundError)
                and not _path_exists(parent)
                and not _path_exists(quarantine_target)
            ):
                return
            raise

    if not _path_matches_parent_identity(quarantine_target, ownership):
        if _restore_quarantined_path(quarantine_target, parent):
            quarantine_parent.rmdir()
            return
        raise OSError("installation parent changed during cleanup")

    try:
        quarantine_target.rmdir()
    except OSError:
        if _restore_quarantined_path(quarantine_target, parent):
            quarantine_parent.rmdir()
            return
        raise
    quarantine_parent.rmdir()
    if deferred_rename_error is not None:
        raise deferred_rename_error


def _missing_parent_directories(parent: Path) -> list[Path]:
    missing: list[Path] = []
    candidate = parent
    while not candidate.exists() and not candidate.is_symlink():
        missing.append(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return missing


def _ensure_parent_directories(
    parent: Path,
    created_parents: list[ParentOwnership],
    progress: InstallProgress,
) -> None:
    for candidate in reversed(_missing_parent_directories(parent)):
        _validate_owned_parents(created_parents)
        staged = _private_staging_candidate(
            candidate.parent,
            f".{candidate.name}.install-",
        )
        ownership: ParentOwnership | None = None
        staging_ready = False
        publish_attempted = False
        concurrent_creator = False
        try:
            _create_private_directory(staged)
            staging_ready = True
            _validate_owned_parents(created_parents)
            ownership = _capture_parent_ownership(staged, path=candidate)
            created_parents.append(ownership)
            try:
                publish_attempted = True
                _atomic_rename_noreplace(staged, candidate)
            except BaseException as publish_error:  # noqa: BLE001
                if not _path_matches_parent_identity(candidate, ownership):
                    created_parents.remove(ownership)
                    ownership = None
                if isinstance(publish_error, OSError) and _path_exists(candidate):
                    if candidate.is_dir() and not candidate.is_symlink():
                        # A concurrent creator owns this directory.
                        concurrent_creator = True
                    else:
                        raise NotADirectoryError(
                            f"install parent is not a directory: {candidate}"
                        ) from publish_error
                else:
                    raise
            if not concurrent_creator:
                _require_parent_identity(candidate, ownership)
            _validate_owned_parents(created_parents)
        except BaseException as primary_error:  # noqa: BLE001
            if isinstance(primary_error, FileExistsError) and not staging_ready:
                raise
            if staging_ready and not publish_attempted and not _path_exists(staged):
                progress.add_cleanup_failure(
                    "private installation staging state could not be established"
                )
            _reraise_after_staging_cleanup(
                primary_error,
                primary_error.__traceback__,
                staged,
                progress.add_cleanup_failure,
                empty_only=not staging_ready,
            )
        else:
            if _path_exists(staged):
                _remove_private_staging(
                    staged,
                    progress.add_cleanup_failure,
                )
        if concurrent_creator:
            continue


def _validate_parent_chain(parent: Path) -> None:
    candidate = parent
    while not candidate.exists():
        if candidate.is_symlink():
            raise NotADirectoryError(f"install parent is not a directory: {candidate}")
        candidate = candidate.parent
    if not candidate.is_dir():
        raise NotADirectoryError(f"install parent is not a directory: {candidate}")


def _classify_existing_install(
    target: Path,
    mode: str,
    source: Path,
) -> str:
    if target.is_symlink():
        if target.resolve() != source:
            raise FileExistsError(f"refusing to replace existing symlink: {target}")
        if mode != "symlink":
            raise FileExistsError(
                f"existing install mode is symlink, requested {mode}: {target}"
            )
        return "already-installed"
    if target.exists():
        if target.resolve() != source:
            raise FileExistsError(f"refusing to replace existing path: {target}")
        if mode != "copy":
            raise FileExistsError(
                f"existing install mode is directory, requested {mode}: {target}"
            )
        return "already-installed"
    return "not-installed"


def _installed_target_status(
    target: Path,
    mode: str,
    source: Path,
    ownership: TargetOwnership | None = None,
) -> str:
    """Classify a target without exposing its path in public evidence."""

    try:
        target.lstat()
    except FileNotFoundError:
        return "not-installed"
    except OSError:
        return "not-established"

    if ownership is not None and not _target_matches_ownership(target, ownership):
        return "not-established"

    try:
        if target.is_symlink():
            if mode == "symlink" and target.resolve() == source:
                return "installed"
            return "not-established"
        if mode == "copy" and target.is_dir():
            _validate_staged_copy(target)
            return "installed"
    except (OSError, ValueError):
        pass
    return "not-established"


def _unowned_target_status(target: Path) -> str:
    """Report absence only; an extant path without ownership is unknown."""

    try:
        target.lstat()
    except FileNotFoundError:
        return "not-installed"
    except OSError:
        return "not-established"
    return "not-established"


def _require_target_ownership(
    ownership: TargetOwnership | None,
) -> TargetOwnership:
    if ownership is None:
        raise OSError("installation completed without ownership evidence")
    return ownership


def _is_interruption(error: BaseException) -> bool:
    return isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))


def install_skill(
    *,
    source: Path,
    home: Path,
    hosts: list[str],
    mode: str,
    dry_run: bool = False,
    progress: InstallProgress | None = None,
) -> list[dict[str, Any]]:
    """Install the skill and return one record per distinct discovery target."""

    install_progress = (
        progress
        if progress is not None
        else InstallProgress(mode=mode, dry_run=dry_run)
    )
    source = source.expanduser().resolve()
    home = home.expanduser().resolve()
    skill_file = source / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError(f"SKILL.md not found at source root: {source}")
    if _read_skill_name(skill_file) != SKILL_NAME:
        raise ValueError(f"SKILL.md name must be {SKILL_NAME!r}")
    missing_resources = [
        relative_name
        for relative_name in REQUIRED_COPY_FILES
        if not (source / relative_name).is_file()
    ]
    if missing_resources:
        raise ValueError(f"missing required skill resource: {missing_resources[0]}")
    _validate_source_payload(source)
    if mode not in {"symlink", "copy"}:
        raise ValueError("mode must be 'symlink' or 'copy'")
    if not hosts:
        raise ValueError("select at least one host")
    invalid_hosts = set(hosts) - {*HOST_ROOTS, "all"}
    if invalid_hosts:
        raise ValueError(f"unknown host: {sorted(invalid_hosts)[0]}")

    selections = _selected_targets(hosts, home)
    install_progress.configure(selections)
    for selection in selections:
        target = selection["target"]
        _validate_parent_chain(target.parent)
        install_progress.set_status(
            target,
            _classify_existing_install(target, mode, source),
        )

    records: list[dict[str, Any]] = []
    created_targets: list[TargetOwnership] = []
    created_parents: list[ParentOwnership] = []
    attempted_target: Path | None = None
    attempted_ownership: TargetOwnership | None = None

    def record_target_ownership(ownership: TargetOwnership | None) -> None:
        nonlocal attempted_ownership
        if ownership is None:
            attempted_ownership = None
            return
        if (
            attempted_target is None
            or ownership.target != attempted_target
            or ownership.mode != mode
        ):
            raise OSError("ownership evidence does not match the install target")
        attempted_ownership = ownership

    try:
        for selection in selections:
            target = selection["target"]
            hosts_for_target = selection["hosts"]
            current_status = _classify_existing_install(target, mode, source)

            if current_status == "already-installed":
                status = "already-installed"
            elif dry_run:
                status = "planned"
            elif mode == "symlink":
                attempted_target = target
                install_progress.set_status(target, "not-established")
                _ensure_parent_directories(
                    target.parent,
                    created_parents,
                    install_progress,
                )
                _symlink_skill(
                    source,
                    target,
                    owned_parents=_owned_parent_chain(
                        target.parent,
                        created_parents,
                    ),
                    ownership_recorder=record_target_ownership,
                    cleanup_recorder=install_progress.add_cleanup_failure,
                )
                attempted_ownership = _require_target_ownership(attempted_ownership)
                created_targets.append(attempted_ownership)
                status = "installed"
                install_progress.set_status(target, status)
                attempted_target = None
                attempted_ownership = None
            else:
                attempted_target = target
                attempted_ownership = None
                install_progress.set_status(target, "not-established")
                _ensure_parent_directories(
                    target.parent,
                    created_parents,
                    install_progress,
                )
                _copy_skill(
                    source,
                    target,
                    owned_parents=_owned_parent_chain(
                        target.parent,
                        created_parents,
                    ),
                    ownership_recorder=record_target_ownership,
                    cleanup_recorder=install_progress.add_cleanup_failure,
                )
                attempted_ownership = _require_target_ownership(attempted_ownership)
                created_targets.append(attempted_ownership)
                status = "installed"
                install_progress.set_status(target, status)
                attempted_target = None
                attempted_ownership = None

            if status in {"already-installed", "planned"}:
                install_progress.set_status(target, status)

            records.append(
                {
                    "hosts": hosts_for_target,
                    "mode": mode,
                    "status": status,
                    "target": str(target),
                }
            )
    except BaseException as install_error:
        cleanup_error_to_raise: BaseException | None = None
        if attempted_target is not None:
            try:
                if attempted_ownership is None:
                    attempted_status = _unowned_target_status(attempted_target)
                else:
                    attempted_status = _installed_target_status(
                        attempted_target,
                        mode,
                        source,
                        attempted_ownership,
                    )
            except (KeyboardInterrupt, asyncio.CancelledError) as cleanup_error:
                attempted_status = "not-established"
                cleanup_error_to_raise = cleanup_error
                install_progress.add_cleanup_failure(
                    "failed to determine installation target state"
                )
            install_progress.set_status(attempted_target, attempted_status)
            if (
                attempted_ownership is not None
                and attempted_ownership not in created_targets
            ):
                created_targets.append(attempted_ownership)
            elif attempted_status == "not-established" and _is_interruption(
                install_error
            ):
                install_progress.add_cleanup_failure(
                    "installation target state could not be established"
                )

        for ownership in reversed(created_targets):
            target = ownership.target
            try:
                _remove_created_target(target, mode, source, ownership)
            except BaseException as cleanup_error:  # noqa: BLE001
                install_progress.set_status(target, "not-established")
                install_progress.add_cleanup_failure(
                    "failed to remove an installation target"
                )
                if _is_interruption(cleanup_error):
                    cleanup_error_to_raise = cleanup_error_to_raise or cleanup_error
            else:
                try:
                    target_status = _installed_target_status(
                        target,
                        mode,
                        source,
                        ownership,
                    )
                except (KeyboardInterrupt, asyncio.CancelledError) as cleanup_error:
                    target_status = "not-established"
                    cleanup_error_to_raise = cleanup_error_to_raise or cleanup_error
                install_progress.set_status(target, target_status)
                if target_status != "not-installed":
                    install_progress.add_cleanup_failure(
                        "failed to confirm installation target removal"
                    )
        for ownership in sorted(
            created_parents,
            key=lambda item: len(item.path.parts),
            reverse=True,
        ):
            try:
                _remove_created_parent(ownership)
            except (KeyboardInterrupt, asyncio.CancelledError) as cleanup_error:
                install_progress.add_cleanup_failure(
                    "failed to remove an installation parent directory"
                )
                cleanup_error_to_raise = cleanup_error_to_raise or cleanup_error
            except OSError:
                install_progress.add_cleanup_failure(
                    "failed to remove an installation parent directory"
                )
        if cleanup_error_to_raise is not None and not _is_interruption(install_error):
            for message in install_progress.cleanup_failures:
                failures = getattr(cleanup_error_to_raise, "cleanup_failures", None)
                if not isinstance(failures, list):
                    failures = []
                    setattr(cleanup_error_to_raise, "cleanup_failures", failures)
                if message not in failures:
                    failures.append(message)
            raise cleanup_error_to_raise from install_error
        if install_progress.cleanup_failures:
            if _is_interruption(install_error):
                setattr(
                    install_error,
                    "cleanup_failures",
                    list(install_progress.cleanup_failures),
                )
                raise
            raise OSError(
                f"installation failed ({install_error}); cleanup also failed"
            ) from install_error
        raise
    return records


def _recognized_skill_root(target: Path) -> bool:
    try:
        return _read_skill_name(target / "SKILL.md") == SKILL_NAME
    except (OSError, UnicodeError, ValueError):
        return False


def _load_install_manifest(target: Path) -> dict[str, Any] | None:
    manifest_path = target / INSTALL_MANIFEST
    try:
        payload = _read_regular_file(
            manifest_path,
            maximum=MAX_INSTALL_MANIFEST_BYTES,
        )
    except ValueError:
        return None
    try:
        manifest = json.loads(payload.decode("ascii"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        return None
    return manifest if isinstance(manifest, dict) else None


def _manifest_file_hashes(
    manifest: dict[str, Any],
) -> dict[str, tuple[str, int]] | None:
    if manifest.get("schema_version") != INSTALL_MANIFEST_SCHEMA:
        return None
    skill = manifest.get("skill")
    if not isinstance(skill, dict) or skill.get("name") != SKILL_NAME:
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    observed: dict[str, tuple[str, int]] = {}
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            return None
        relative_name = record.get("path")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(relative_name, str)
            or relative_name in observed
            or relative_name not in {*BUNDLE_FILES, *OPTIONAL_PROVENANCE_FILES}
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_DISTRIBUTABLE_FILE_BYTES
        ):
            return None
        observed[relative_name] = (digest, size)
    if not set(BUNDLE_FILES) <= set(observed):
        return None
    present_provenance = set(observed) & set(OPTIONAL_PROVENANCE_FILES)
    if present_provenance and present_provenance != set(OPTIONAL_PROVENANCE_FILES):
        return None
    return observed


def _source_payload_hashes(source: Path) -> dict[str, tuple[str, int]]:
    _validate_source_payload(source)
    relative_names = [
        *BUNDLE_FILES,
        *(
            relative_name
            for relative_name in OPTIONAL_PROVENANCE_FILES
            if _path_exists(source / relative_name)
        ),
    ]
    hashes: dict[str, tuple[str, int]] = {}
    for relative_name in relative_names:
        payload = _read_regular_file(
            source / relative_name,
            maximum=MAX_DISTRIBUTABLE_FILE_BYTES,
        )
        hashes[relative_name] = (hashlib.sha256(payload).hexdigest(), len(payload))
    return hashes


def _installed_tree_is_exact(
    target: Path,
    hashes: Mapping[str, tuple[str, int]],
) -> bool:
    expected_files = {*hashes, INSTALL_MANIFEST}
    expected_directories = {
        PurePosixPath(*PurePosixPath(relative_name).parts[:depth]).as_posix()
        for relative_name in expected_files
        for depth in range(1, len(PurePosixPath(relative_name).parts))
    }
    pending = [target]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError:
            return False
        for child in children:
            try:
                metadata = child.lstat()
                relative_name = child.relative_to(target).as_posix()
            except (OSError, ValueError):
                return False
            if stat.S_ISLNK(metadata.st_mode):
                return False
            if stat.S_ISDIR(metadata.st_mode):
                if PurePosixPath(relative_name).parts[0] == ".venv":
                    continue
                if relative_name not in expected_directories:
                    return False
                pending.append(child)
            elif (
                not stat.S_ISREG(metadata.st_mode)
                or relative_name not in expected_files
            ):
                return False
    return True


def _copy_health(
    target: Path,
    source: Path,
    expected_version: str,
) -> tuple[str, str | None]:
    if not _recognized_skill_root(target):
        return "unrecognized", None
    try:
        installed_version = read_version(target)
    except ValueError:
        return "incomplete", None
    manifest = _load_install_manifest(target)
    if manifest is None:
        return "incomplete", installed_version
    hashes = _manifest_file_hashes(manifest)
    skill = manifest.get("skill")
    if hashes is None or not isinstance(skill, dict):
        return "modified", installed_version
    if skill.get("version") != installed_version:
        return "modified", installed_version
    if not _installed_tree_is_exact(target, hashes):
        return "modified", installed_version
    recorded_provenance = set(hashes) & set(OPTIONAL_PROVENANCE_FILES)
    actual_provenance = {
        relative_name
        for relative_name in OPTIONAL_PROVENANCE_FILES
        if _path_exists(target / relative_name)
    }
    if recorded_provenance != actual_provenance:
        return "modified", installed_version
    for relative_name, (expected_digest, expected_size) in hashes.items():
        try:
            payload = _read_regular_file(
                target / relative_name,
                maximum=MAX_DISTRIBUTABLE_FILE_BYTES,
            )
        except ValueError:
            return "modified", installed_version
        if len(payload) != expected_size or not secrets.compare_digest(
            hashlib.sha256(payload).hexdigest(),
            expected_digest,
        ):
            return "modified", installed_version
    try:
        source_hashes = _source_payload_hashes(source)
    except (OSError, ValueError):
        return "not-established", installed_version
    if source_hashes != hashes:
        return "stale", installed_version
    if installed_version != expected_version:
        return "stale", installed_version
    return "current", installed_version


def _symlink_health(
    target: Path,
    source: Path,
    expected_version: str,
) -> tuple[str, str | None]:
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return "unrecognized", None
    if not resolved.is_dir() or not _recognized_skill_root(resolved):
        return "unrecognized", None
    try:
        installed_version = read_version(resolved)
    except ValueError:
        return "incomplete", None
    if installed_version != expected_version:
        return "stale", installed_version
    if resolved != source:
        return "unrecognized", installed_version
    try:
        _validate_source_payload(resolved)
    except ValueError:
        return "incomplete", installed_version
    return "current", installed_version


def check_installations(
    *,
    source: Path,
    home: Path,
    hosts: list[str],
    expected_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Inspect selected discovery targets without writing or exposing paths."""

    source = source.expanduser().resolve()
    home = home.expanduser().resolve()
    expected_version = read_version(source)
    invalid_hosts = set(hosts) - {*HOST_ROOTS, "all"}
    if invalid_hosts:
        raise ValueError(f"unknown host: {sorted(invalid_hosts)[0]}")
    if expected_mode not in {None, "copy", "symlink"}:
        raise ValueError("expected mode must be copy, symlink, or unspecified")

    records: list[dict[str, Any]] = []
    for selection in _selected_targets(hosts, home):
        target = selection["target"]
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            status = "absent"
            detected_mode = "absent"
            installed_version = None
        except OSError:
            status = "not-established"
            detected_mode = "not-established"
            installed_version = None
        else:
            if stat.S_ISLNK(metadata.st_mode):
                detected_mode = "symlink"
                status, installed_version = _symlink_health(
                    target,
                    source,
                    expected_version,
                )
            elif stat.S_ISDIR(metadata.st_mode):
                detected_mode = "copy"
                status, installed_version = _copy_health(
                    target,
                    source,
                    expected_version,
                )
            else:
                detected_mode = "other"
                status = "unrecognized"
                installed_version = None
            if expected_mode is not None and detected_mode != expected_mode:
                status = "wrong-mode"

        record: dict[str, Any] = {
            "hosts": list(selection["hosts"]),
            "status": status,
            "mode": detected_mode,
            "expected_version": expected_version,
        }
        if expected_mode is not None:
            record["expected_mode"] = expected_mode
        if installed_version is not None:
            record["installed_version"] = installed_version
        records.append(record)
    return records


def _health_next_action(records: Sequence[dict[str, Any]]) -> dict[str, str]:
    statuses = {str(record["status"]) for record in records}
    if statuses == {"current"}:
        return {
            "code": "ready",
            "hint": "Selected Skill installations match this release.",
        }
    if "absent" in statuses:
        return {
            "code": "preview-install",
            "hint": "Preview an install with --dry-run before writing any target.",
        }
    return {
        "code": "inspect-existing-install",
        "hint": (
            "Inspect the selected existing installation; this command never "
            "overwrites, removes, repairs, or updates it."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Install xianyu-monitor into Agent Skills discovery roots"
    )
    parser.add_argument(
        "--host",
        action="append",
        choices=(*HOST_ROOTS, "all"),
        help="target host; repeat for several (default: all)",
    )
    parser.add_argument(
        "--mode",
        choices=("symlink", "copy"),
        help=(
            "link to this checkout or copy distributable files; defaults to "
            "symlink for install and means any mode for --check"
        ),
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help=argparse.SUPPRESS,
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--dry-run", action="store_true")
    action_group.add_argument(
        "--check",
        action="store_true",
        help="inspect installation health without writing or checking the network",
    )
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = args.mode or "symlink"
    if args.check:
        try:
            records = check_installations(
                source=Path(__file__).resolve().parents[1],
                home=args.home,
                hosts=args.host or ["all"],
                expected_mode=args.mode,
            )
            healthy = bool(records) and all(
                record["status"] == "current" for record in records
            )
            print(
                json.dumps(
                    {
                        "ok": healthy,
                        "check": "offline-installation-health",
                        "installs": records,
                        "next_action": _health_next_action(records),
                    },
                    ensure_ascii=True,
                    indent=2,
                    allow_nan=False,
                )
            )
            return 0 if healthy else 2  # noqa: TRY300 - protected JSON emission
        except (OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "installation health could not be established",
                        "error_type": type(exc).__name__,
                        "next_action": {
                            "code": "rerun-install-check",
                            "hint": (
                                "Inspect the selected local installation and rerun "
                                "the offline health check."
                            ),
                        },
                    },
                    ensure_ascii=True,
                )
            )
            return 2

    progress = InstallProgress(mode=mode, dry_run=args.dry_run)
    try:
        source = Path(__file__).resolve().parents[1]
        records = install_skill(
            source=source,
            home=args.home,
            hosts=args.host or ["all"],
            mode=mode,
            dry_run=args.dry_run,
            progress=progress,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": args.dry_run,
                    "installs": records,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0  # noqa: TRY300 - keep success emission cancellation-protected.
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        failures = getattr(exc, "cleanup_failures", None)
        if isinstance(failures, list) and failures and not progress.cleanup_failures:
            progress.add_cleanup_failure("installation cleanup was incomplete")
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "installation cancelled",
                    "error_type": type(exc).__name__,
                    "dry_run": args.dry_run,
                    **progress.public_evidence(),
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
