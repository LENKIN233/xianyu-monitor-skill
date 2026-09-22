from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import release_bundle

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()


def test_release_build_is_reproducible_and_verifiable(tmp_path: Path) -> None:
    first = release_bundle.build_bundle(ROOT, tmp_path / "first")
    second = release_bundle.build_bundle(ROOT, tmp_path / "second")

    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.checksum.read_bytes() == second.checksum.read_bytes()
    verified = release_bundle.verify_bundle(first.archive, first.checksum)
    assert verified.version == EXPECTED_VERSION
    assert verified.sha256 == hashlib.sha256(first.archive.read_bytes()).hexdigest()
    assert set(verified.entries) == {
        *release_bundle.BUNDLE_FILES,
        *release_bundle.GENERATED_BUNDLE_FILES,
    }


def test_release_manifest_and_sbom_match_payload(tmp_path: Path) -> None:
    built = release_bundle.build_bundle(ROOT, tmp_path)
    verified = release_bundle.verify_bundle(built.archive, built.checksum)
    manifest = json.loads(verified.entries["MANIFEST.json"])
    sbom = json.loads(verified.entries["SBOM.spdx.json"])

    assert manifest["skill"]["version"] == EXPECTED_VERSION
    assert manifest["source_date_epoch"] == 0
    assert {record["path"] for record in manifest["files"]} == {
        *release_bundle.BUNDLE_FILES,
        "SBOM.spdx.json",
    }
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert {package["name"] for package in sbom["packages"]} == {
        "xianyu-monitor",
        "greenlet",
        "playwright",
        "pyee",
        "typing-extensions",
        "tzdata",
    }


def test_release_verify_rejects_checksum_mismatch(tmp_path: Path) -> None:
    built = release_bundle.build_bundle(ROOT, tmp_path)
    built.checksum.write_bytes(f"{'0' * 64}  {built.archive.name}\n".encode("ascii"))

    with pytest.raises(release_bundle.ReleaseBundleError, match="checksum"):
        release_bundle.verify_bundle(built.archive, built.checksum)


def test_release_verify_rejects_path_traversal_archive(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    payload = b"escape"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("xianyu-monitor-2.0.0/../escape")
        info.size = len(payload)
        output.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = tmp_path / "unsafe.tar.gz.sha256"
    checksum.write_bytes(f"{digest}  {archive.name}\n".encode("ascii"))

    with pytest.raises(release_bundle.ReleaseBundleError, match="unsafe"):
        release_bundle.verify_bundle(archive, checksum)


def test_release_verify_rejects_noncanonical_archive(tmp_path: Path) -> None:
    built = release_bundle.build_bundle(ROOT, tmp_path / "built")
    verified = release_bundle.verify_bundle(built.archive, built.checksum)
    archive = tmp_path / "noncanonical.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for relative_name in sorted(verified.entries, reverse=True):
            payload = verified.entries[relative_name]
            info = tarfile.TarInfo(f"{verified.root_name}/{relative_name}")
            info.size = len(payload)
            info.mode = release_bundle._entry_mode(relative_name)  # noqa: SLF001
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            output.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = tmp_path / "noncanonical.tar.gz.sha256"
    checksum.write_bytes(f"{digest}  {archive.name}\n".encode("ascii"))

    with pytest.raises(release_bundle.ReleaseBundleError, match="canonical"):
        release_bundle.verify_bundle(archive, checksum)


def test_release_mode_rejects_dirty_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git_executable = shutil.which("git")
    assert git_executable is not None
    subprocess.run(  # noqa: S603
        [git_executable, "init", "-q"], cwd=repository, check=True
    )
    subprocess.run(  # noqa: S603
        [git_executable, "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(  # noqa: S603
        [git_executable, "config", "user.name", "Test"],
        cwd=repository,
        check=True,
    )
    (repository / "VERSION").write_text("2.0.0\n")
    subprocess.run(  # noqa: S603
        [git_executable, "add", "VERSION"], cwd=repository, check=True
    )
    subprocess.run(  # noqa: S603
        [git_executable, "commit", "-qm", "initial"],
        cwd=repository,
        check=True,
    )
    subprocess.run(  # noqa: S603
        [git_executable, "tag", "v2.0.0"], cwd=repository, check=True
    )
    (repository / "untracked.txt").write_text("dirty\n")

    with pytest.raises(release_bundle.ReleaseBundleError, match="clean"):
        release_bundle._git_release_evidence(repository)  # noqa: SLF001


def test_formal_release_embeds_exact_commit_provenance(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for relative_name in release_bundle.BUNDLE_FILES:
        source = ROOT / relative_name
        destination = repository / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    git_executable = shutil.which("git")
    assert git_executable is not None
    commands = (
        ("init", "-q"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-qm", "release"),
        ("tag", f"v{EXPECTED_VERSION}"),
    )
    for command in commands:
        subprocess.run(  # noqa: S603
            [git_executable, *command],
            cwd=repository,
            check=True,
        )

    evidence = release_bundle._git_release_evidence(repository)  # noqa: SLF001
    built = release_bundle.build_bundle(
        repository,
        tmp_path / "dist",
        release_evidence=evidence,
    )
    verified = release_bundle.verify_bundle(built.archive, built.checksum)
    manifest = json.loads(verified.entries["MANIFEST.json"])

    assert manifest["source"] == {
        "commit": evidence["commit"],
        "tag": f"v{EXPECTED_VERSION}",
    }


def test_release_self_check_installs_into_empty_home() -> None:
    report = release_bundle.self_check(ROOT)

    assert report["ok"] is True
    assert report["reproducible"] is True
    assert report["verified"] is True
    assert report["install_from_empty"] is True
    assert report["offline_demo"] is True
