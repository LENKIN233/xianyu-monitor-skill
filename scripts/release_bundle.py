#!/usr/bin/env python3
"""Build and verify the deterministic minimal Skill release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .distribution import BUNDLE_FILES, BUNDLE_FORMAT, GENERATED_BUNDLE_FILES
    from .version_info import SEMVER_PATTERN, SKILL_NAME, read_version
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from distribution import BUNDLE_FILES, BUNDLE_FORMAT, GENERATED_BUNDLE_FILES
    from version_info import SEMVER_PATTERN, SKILL_NAME, read_version

MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
RELEASE_SCHEMA = 1
SBOM_FILENAME = "SBOM.spdx.json"
MANIFEST_FILENAME = "MANIFEST.json"
LOCKED_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)\Z"
)
PACKAGE_LICENSES = {
    "greenlet": "MIT AND PSF-2.0",
    "playwright": "Apache-2.0",
    "pyee": "MIT",
    "typing-extensions": "PSF-2.0",
    "tzdata": "Apache-2.0",
}


class ReleaseBundleError(ValueError):
    """The release input or artifact violates the deterministic contract."""


@dataclass(frozen=True)
class BuiltBundle:
    archive: Path
    checksum: Path
    sha256: str
    version: str


@dataclass(frozen=True)
class VerifiedBundle:
    archive_name: str
    root_name: str
    version: str
    sha256: str
    entries: Mapping[str, bytes]


def _git_release_evidence(source: Path) -> dict[str, str | bool]:
    """Require an exact clean release tag before publication, never during tests."""

    git_executable = shutil.which("git")
    if git_executable is None:
        raise ReleaseBundleError("Git release evidence could not be established")
    try:
        inside = subprocess.run(  # noqa: S603
            [git_executable, "rev-parse", "--is-inside-work-tree"],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        commit = subprocess.run(  # noqa: S603
            [git_executable, "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        status = subprocess.run(  # noqa: S603
            [  # noqa: S607
                git_executable,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        tags = subprocess.run(  # noqa: S603
            [git_executable, "tag", "--points-at", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBundleError(
            "Git release evidence could not be established"
        ) from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ReleaseBundleError("release source must be a Git working tree")
    if (
        commit.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}", commit.stdout.strip()) is None
    ):
        raise ReleaseBundleError("release commit could not be established")
    if status.returncode != 0 or status.stdout:
        raise ReleaseBundleError(
            "release source must be clean, including untracked files"
        )
    version = read_version(source)
    expected_tag = f"v{version}"
    exact_tags = set(tags.stdout.splitlines()) if tags.returncode == 0 else set()
    if expected_tag not in exact_tags:
        raise ReleaseBundleError(
            f"release commit must carry the exact tag {expected_tag}"
        )
    return {
        "commit": commit.stdout.strip(),
        "tag": expected_tag,
        "clean": True,
    }


def _bounded_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseBundleError("release source is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBundleError(
            "release source entries must be regular non-symlink files"
        )
    if metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise ReleaseBundleError("release source entry exceeds the safety limit")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_SOURCE_FILE_BYTES + 1)
    except OSError as exc:
        raise ReleaseBundleError("release source entry is unreadable") from exc
    if len(payload) > MAX_SOURCE_FILE_BYTES:
        raise ReleaseBundleError("release source entry exceeds the safety limit")
    return payload


def _source_entries(source: Path) -> dict[str, bytes]:
    return {
        relative_name: _bounded_regular_file(source / relative_name)
        for relative_name in BUNDLE_FILES
    }


def _locked_dependencies(lock_payload: bytes) -> list[tuple[str, str]]:
    try:
        lines = lock_payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseBundleError("requirements lock must be ASCII") from exc
    dependencies: list[tuple[str, str]] = []
    observed: set[str] = set()
    for line in lines:
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        match = LOCKED_REQUIREMENT.fullmatch(normalized)
        if match is None:
            raise ReleaseBundleError(
                "requirements lock must contain exact package versions"
            )
        name = match.group("name").lower().replace("_", "-")
        if name in observed:
            raise ReleaseBundleError("requirements lock contains a duplicate package")
        observed.add(name)
        dependencies.append((name, match.group("version")))
    if not dependencies or observed != set(PACKAGE_LICENSES):
        raise ReleaseBundleError(
            "requirements lock and SBOM package metadata are inconsistent"
        )
    return sorted(dependencies)


def _spdx_id(name: str) -> str:
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]", "-", name)


def _sbom_payload(version: str, lock_payload: bytes) -> bytes:
    dependencies = _locked_dependencies(lock_payload)
    lock_digest = hashlib.sha256(lock_payload).hexdigest()
    root_id = _spdx_id(SKILL_NAME)
    packages = [
        {
            "SPDXID": root_id,
            "name": SKILL_NAME,
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "NOASSERTION",
        }
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    for name, dependency_version in dependencies:
        dependency_id = _spdx_id(name)
        license_id = PACKAGE_LICENSES[name]
        packages.append(
            {
                "SPDXID": dependency_id,
                "name": name,
                "versionInfo": dependency_version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": license_id,
                "licenseDeclared": license_id,
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": root_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": dependency_id,
            }
        )
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{SKILL_NAME}-{version}",
        "documentNamespace": (
            "https://github.com/LENKIN233/xianyu-monitor-skill/spdx/"
            f"{version}-{lock_digest[:16]}"
        ),
        "creationInfo": {
            "created": "1970-01-01T00:00:00Z",
            "creators": ["Tool: xianyu-monitor-release/1"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _entry_mode(relative_name: str) -> int:
    return 0o755 if relative_name.startswith("scripts/") else 0o644


def _file_record(relative_name: str, payload: bytes) -> dict[str, Any]:
    return {
        "path": relative_name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "mode": _entry_mode(relative_name),
    }


def _manifest_payload(
    version: str,
    entries: Mapping[str, bytes],
    release_evidence: Mapping[str, str | bool] | None = None,
) -> bytes:
    recorded_names = (*BUNDLE_FILES, SBOM_FILENAME)
    document = {
        "schema_version": RELEASE_SCHEMA,
        "bundle_format": BUNDLE_FORMAT,
        "skill": {"name": SKILL_NAME, "version": version},
        "source_date_epoch": 0,
        "files": [_file_record(name, entries[name]) for name in recorded_names],
    }
    if release_evidence is not None:
        commit = release_evidence.get("commit")
        tag = release_evidence.get("tag")
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or tag != f"v{version}"
            or release_evidence.get("clean") is not True
        ):
            raise ReleaseBundleError("release evidence is invalid")
        document["source"] = {"commit": commit, "tag": tag}
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _deterministic_archive(root_name: str, entries: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=0,
    ) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.GNU_FORMAT,
        ) as archive:
            for relative_name in sorted(entries):
                payload = entries[relative_name]
                info = tarfile.TarInfo(f"{root_name}/{relative_name}")
                info.size = len(payload)
                info.mode = _entry_mode(relative_name)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(payload))
    payload = output.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ReleaseBundleError("release archive exceeds the safety limit")
    return payload


def _require_entries_match_commit(
    source: Path,
    entries: Mapping[str, bytes],
    release_evidence: Mapping[str, str | bool],
) -> None:
    git_executable = shutil.which("git")
    commit = release_evidence.get("commit")
    if git_executable is None or not isinstance(commit, str):
        raise ReleaseBundleError("Git release evidence could not be established")
    for relative_name in BUNDLE_FILES:
        try:
            completed = subprocess.run(  # noqa: S603
                [git_executable, "show", f"{commit}:{relative_name}"],
                cwd=source,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseBundleError(
                "release commit payload could not be established"
            ) from exc
        if completed.returncode != 0 or not secrets.compare_digest(
            hashlib.sha256(completed.stdout).digest(),
            hashlib.sha256(entries[relative_name]).digest(),
        ):
            raise ReleaseBundleError(
                "release source payload does not match the tagged commit"
            )


def build_bundle(
    source: Path,
    output_dir: Path,
    *,
    release_evidence: Mapping[str, str | bool] | None = None,
) -> BuiltBundle:
    """Build the minimal deterministic bundle without consulting Git or network."""

    source = source.expanduser().resolve()
    version = read_version(source)
    entries = _source_entries(source)
    if release_evidence is not None:
        _require_entries_match_commit(source, entries, release_evidence)
    entries[SBOM_FILENAME] = _sbom_payload(
        version,
        entries["requirements-lock.txt"],
    )
    entries[MANIFEST_FILENAME] = _manifest_payload(
        version,
        entries,
        release_evidence,
    )
    root_name = f"{SKILL_NAME}-{version}"
    archive_payload = _deterministic_archive(root_name, entries)
    digest = hashlib.sha256(archive_payload).hexdigest()

    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{root_name}.tar.gz"
    checksum = output_dir / f"{archive.name}.sha256"
    if (
        archive.exists()
        or archive.is_symlink()
        or checksum.exists()
        or checksum.is_symlink()
    ):
        raise FileExistsError("refusing to replace an existing release artifact")
    _write_new_file(archive, archive_payload, mode=0o644)
    _write_new_file(
        checksum,
        f"{digest}  {archive.name}\n".encode("ascii"),
        mode=0o644,
    )
    return BuiltBundle(archive, checksum, digest, version)


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bounded_archive(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseBundleError("release archive is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBundleError("release archive must be a regular non-symlink file")
    if metadata.st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseBundleError("release archive exceeds the safety limit")
    with path.open("rb") as stream:
        payload = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ReleaseBundleError("release archive exceeds the safety limit")
    return payload


def _verify_checksum(archive: Path, archive_payload: bytes, checksum: Path) -> str:
    checksum_payload = _bounded_regular_file(checksum)
    try:
        checksum_line = checksum_payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseBundleError("release checksum must be ASCII") from exc
    digest = hashlib.sha256(archive_payload).hexdigest()
    if checksum_line != f"{digest}  {archive.name}\n":
        raise ReleaseBundleError("release checksum does not match the archive")
    return digest


def _safe_archive_entries(archive_payload: bytes) -> tuple[str, dict[str, bytes]]:
    entries: dict[str, bytes] = {}
    root_name: str | None = None
    unpacked = 0
    maximum_entries = len(BUNDLE_FILES) + len(GENERATED_BUNDLE_FILES)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_payload), mode="r:gz") as archive:
            for member in archive:
                if len(entries) >= maximum_entries:
                    raise ReleaseBundleError(
                        "release archive contains too many entries"
                    )
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or len(path.parts) < 2
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or not member.isreg()
                    or member.size < 0
                    or member.size > MAX_SOURCE_FILE_BYTES
                ):
                    raise ReleaseBundleError("release archive contains an unsafe entry")
                candidate_root = path.parts[0]
                if root_name is None:
                    root_name = candidate_root
                elif candidate_root != root_name:
                    raise ReleaseBundleError("release archive contains multiple roots")
                relative_name = PurePosixPath(*path.parts[1:]).as_posix()
                if relative_name in entries:
                    raise ReleaseBundleError(
                        "release archive contains a duplicate entry"
                    )
                if stat.S_IMODE(member.mode) != _entry_mode(relative_name):
                    raise ReleaseBundleError(
                        "release archive entry mode is inconsistent"
                    )
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ReleaseBundleError(
                        "release archive expands past the safety limit"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseBundleError("release archive entry is unreadable")
                payload = stream.read(member.size + 1)
                if len(payload) != member.size:
                    raise ReleaseBundleError(
                        "release archive entry size is inconsistent"
                    )
                entries[relative_name] = payload
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise ReleaseBundleError("release archive is unreadable") from exc
    if root_name is None:
        raise ReleaseBundleError("release archive is empty")
    return root_name, entries


def _parse_manifest(payload: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(payload.decode("ascii"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ReleaseBundleError("release manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ReleaseBundleError("release manifest is invalid")
    return manifest


def _verify_manifest(
    root_name: str,
    entries: Mapping[str, bytes],
) -> str:
    expected_entries = {*BUNDLE_FILES, *GENERATED_BUNDLE_FILES}
    if set(entries) != expected_entries:
        raise ReleaseBundleError("release archive file set is incomplete or unexpected")
    manifest = _parse_manifest(entries[MANIFEST_FILENAME])
    manifest_keys = set(manifest)
    base_manifest_keys = {
        "schema_version",
        "bundle_format",
        "skill",
        "source_date_epoch",
        "files",
    }
    if manifest_keys not in (base_manifest_keys, base_manifest_keys | {"source"}):
        raise ReleaseBundleError("release manifest contains unexpected fields")
    if (
        manifest.get("schema_version") != RELEASE_SCHEMA
        or manifest.get("bundle_format") != BUNDLE_FORMAT
        or manifest.get("source_date_epoch") != 0
    ):
        raise ReleaseBundleError("release manifest contract is unsupported")
    skill = manifest.get("skill")
    if not isinstance(skill, dict) or set(skill) != {"name", "version"}:
        raise ReleaseBundleError("release manifest Skill identity is invalid")
    version = skill.get("version")
    if skill.get("name") != SKILL_NAME or not isinstance(version, str):
        raise ReleaseBundleError("release manifest Skill identity is invalid")
    if SEMVER_PATTERN.fullmatch(version) is None:
        raise ReleaseBundleError("release manifest version is invalid")
    if root_name != f"{SKILL_NAME}-{version}":
        raise ReleaseBundleError("release archive root does not match its manifest")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ReleaseBundleError("release manifest file records are invalid")
    expected_recorded = {*BUNDLE_FILES, SBOM_FILENAME}
    observed: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "sha256",
            "size",
            "mode",
        }:
            raise ReleaseBundleError("release manifest file record is invalid")
        relative_name = record.get("path")
        if (
            not isinstance(relative_name, str)
            or relative_name not in expected_recorded
            or relative_name in observed
        ):
            raise ReleaseBundleError("release manifest file record is invalid")
        observed.add(relative_name)
        payload = entries[relative_name]
        if (
            record.get("size") != len(payload)
            or record.get("mode") != _entry_mode(relative_name)
            or record.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ReleaseBundleError("release manifest digest does not match payload")
    if observed != expected_recorded:
        raise ReleaseBundleError("release manifest file set is incomplete")
    source = manifest.get("source")
    if source is not None and (
        not isinstance(source, dict)
        or set(source) != {"commit", "tag"}
        or not isinstance(source.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", source["commit"]) is None
        or source.get("tag") != f"v{version}"
    ):
        raise ReleaseBundleError("release manifest source evidence is invalid")
    return version


def _verify_sbom(version: str, entries: Mapping[str, bytes]) -> None:
    expected = _sbom_payload(version, entries["requirements-lock.txt"])
    if not secrets_compare(entries[SBOM_FILENAME], expected):
        raise ReleaseBundleError("release SBOM does not match the locked dependencies")


def secrets_compare(observed: bytes, expected: bytes) -> bool:
    """Compare deterministic generated evidence without revealing a prefix."""

    return secrets.compare_digest(
        hashlib.sha256(observed).digest(),
        hashlib.sha256(expected).digest(),
    )


def verify_bundle(archive: Path, checksum: Path | None = None) -> VerifiedBundle:
    """Verify checksum, safe archive shape, manifest, payload hashes, and SBOM."""

    archive = archive.expanduser()
    checksum = (
        archive.with_name(f"{archive.name}.sha256")
        if checksum is None
        else checksum.expanduser()
    )
    archive_payload = _bounded_archive(archive)
    digest = _verify_checksum(archive, archive_payload, checksum)
    root_name, entries = _safe_archive_entries(archive_payload)
    version = _verify_manifest(root_name, entries)
    _verify_sbom(version, entries)
    canonical = _deterministic_archive(root_name, entries)
    if not secrets.compare_digest(
        hashlib.sha256(canonical).digest(),
        hashlib.sha256(archive_payload).digest(),
    ):
        raise ReleaseBundleError("release archive is valid but not canonical")
    return VerifiedBundle(archive.name, root_name, version, digest, entries)


def _extract_verified(bundle: VerifiedBundle, destination: Path) -> Path:
    destination.mkdir(mode=0o700)
    root = destination / bundle.root_name
    root.mkdir(mode=0o700)
    for relative_name in sorted(bundle.entries):
        target = root.joinpath(*PurePosixPath(relative_name).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, _entry_mode(relative_name))
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(bundle.entries[relative_name])
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if os.name != "nt":
            target.chmod(_entry_mode(relative_name))
    return root


def _run_json(arguments: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603
        arguments,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ReleaseBundleError("release smoke command returned invalid JSON") from exc
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or not payload.get("ok")
    ):
        raise ReleaseBundleError("release smoke command failed")
    return payload


def self_check(source: Path) -> dict[str, Any]:
    """Build twice, verify, and install the archive into one empty temporary home."""

    source = source.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="xianyu-release-check-") as temporary:
        temp = Path(temporary)
        first = build_bundle(source, temp / "first")
        second = build_bundle(source, temp / "second")
        if first.archive.read_bytes() != second.archive.read_bytes():
            raise ReleaseBundleError("two release builds were not byte-for-byte equal")
        if first.checksum.read_bytes() != second.checksum.read_bytes():
            raise ReleaseBundleError("two release checksums were not equal")
        verified = verify_bundle(first.archive, first.checksum)
        extracted = _extract_verified(verified, temp / "extracted")
        version_payload = _run_json(
            [sys.executable, str(extracted / "scripts/xianyu.py"), "version"],
            cwd=temp,
        )
        extracted_demo = _run_json(
            [sys.executable, str(extracted / "scripts/xianyu.py"), "demo"],
            cwd=temp,
        )
        empty_home = temp / "home"
        _run_json(
            [
                sys.executable,
                str(extracted / "scripts/xianyu.py"),
                "install",
                "--home",
                str(empty_home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ],
            cwd=temp,
        )
        installed = empty_home / ".agents/skills/xianyu-monitor"
        installed_version = _run_json(
            [sys.executable, str(installed / "scripts/xianyu.py"), "version"],
            cwd=temp,
        )
        installed_demo = _run_json(
            [sys.executable, str(installed / "scripts/xianyu.py"), "demo"],
            cwd=temp,
        )
        health = _run_json(
            [
                sys.executable,
                str(installed / "scripts/xianyu.py"),
                "install",
                "--home",
                str(empty_home),
                "--host",
                "codex",
                "--check",
                "--mode",
                "copy",
            ],
            cwd=temp,
        )
        if version_payload["skill"] != installed_version["skill"]:
            raise ReleaseBundleError("installed version differs from the bundle")
        if extracted_demo != installed_demo:
            raise ReleaseBundleError("installed offline demo differs from the bundle")
        if (
            extracted_demo.get("demo", {}).get("status") != "synthetic"
            or extracted_demo.get("demo", {}).get("network") != "not-used"
            or extracted_demo.get("evaluation", {}).get("passed") is not True
        ):
            raise ReleaseBundleError("release offline demo contract is invalid")
        return {
            "ok": True,
            "version": verified.version,
            "sha256": verified.sha256,
            "reproducible": True,
            "verified": True,
            "install_from_empty": health["ok"] is True,
            "offline_demo": True,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Build or verify the deterministic xianyu-monitor Skill bundle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build", help="build bundle, manifest, SBOM, checksum"
    )
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument(
        "--release",
        action="store_true",
        help="require clean Git state and exact vVERSION tag before building",
    )
    verify = subparsers.add_parser(
        "verify", help="verify one bundle without extraction"
    )
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--checksum", type=Path)
    subparsers.add_parser(
        "self-check",
        help="build twice, verify, and smoke install from an empty home",
    )
    return parser


@sigterm_cancellable
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    try:
        if args.command == "build":
            release_evidence = _git_release_evidence(source) if args.release else None
            bundle = build_bundle(
                source,
                args.output_dir,
                release_evidence=release_evidence,
            )
            payload = {
                "ok": True,
                "version": bundle.version,
                "archive": bundle.archive.name,
                "checksum": bundle.checksum.name,
                "sha256": bundle.sha256,
            }
            if release_evidence is not None:
                payload["release"] = release_evidence
        elif args.command == "verify":
            verified = verify_bundle(args.bundle, args.checksum)
            payload = {
                "ok": True,
                "version": verified.version,
                "archive": verified.archive_name,
                "sha256": verified.sha256,
                "files": len(verified.entries),
            }
        else:
            payload = self_check(source)
    except (OSError, ReleaseBundleError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
            )
        )
        return 2
    print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
