from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import install_skill as installer
import pytest
from install_skill import REQUIRED_COPY_FILES, install_skill


def _make_complete_source(source: Path, *, name: str = "xianyu-monitor") -> None:
    for relative_name in REQUIRED_COPY_FILES:
        path = source / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative_name == "SKILL.md":
            content = f"---\nname: {name}\ndescription: test\n---\n"
        else:
            content = "# test\n"
        path.write_text(content, encoding="utf-8")


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy; copy mode is portable",
)
def test_all_hosts_share_two_symlink_targets(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"

    records = install_skill(
        source=source,
        home=home,
        hosts=["all"],
        mode="symlink",
    )

    shared = home / ".agents/skills/xianyu-monitor"
    claude = home / ".claude/skills/xianyu-monitor"
    assert shared.is_symlink()
    assert claude.is_symlink()
    assert shared.resolve() == source.resolve()
    assert claude.resolve() == source.resolve()
    assert len(records) == 2
    assert {tuple(record["hosts"]) for record in records} == {
        ("codex", "openclaw"),
        ("claude",),
    }


def test_copy_mode_uses_distributable_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    (source / "tests").mkdir()
    (source / "scripts/state.json").write_text('{"cookies": []}\n', encoding="utf-8")
    (source / "tests/secret.txt").write_text("not copied\n", encoding="utf-8")
    (source / "tasks.json").write_text('{"secret": true}\n', encoding="utf-8")

    home = tmp_path / "home"
    install_skill(
        source=source,
        home=home,
        hosts=["claude"],
        mode="copy",
    )

    target = home / ".claude/skills/xianyu-monitor"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts/cdp_profile.py").is_file()
    assert (target / "scripts/doctor.py").is_file()
    assert (target / "scripts/monitor.py").is_file()
    assert not (target / "scripts/state.json").exists()
    assert not (target / "tests").exists()
    assert not (target / "tasks.json").exists()


def test_installer_refuses_to_replace_existing_path(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    target = tmp_path / "home/.agents/skills/xianyu-monitor"
    target.mkdir(parents=True)
    (target / "unrelated.txt").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to replace"):
        install_skill(
            source=source,
            home=tmp_path / "home",
            hosts=["codex"],
            mode="copy",
        )

    assert (target / "unrelated.txt").read_text(encoding="utf-8") == "keep me\n"


def test_installer_preserves_target_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    original_copy_skill = installer._copy_skill

    def create_racing_target(
        source_path: Path,
        target_path: Path,
        **kwargs: object,
    ) -> None:
        target_path.mkdir(parents=True)
        original_copy_skill(source_path, target_path, **kwargs)

    monkeypatch.setattr(installer, "_copy_skill", create_racing_target)

    with pytest.raises(FileExistsError, match="refusing to replace"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert list(target.parent.glob(".xianyu-monitor.*")) == []


def test_action_rejects_unrelated_target_created_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    sentinel = target / "unrelated.txt"
    original_set_status = installer.InstallProgress.set_status
    injected = False

    def create_target_after_preflight(
        progress: installer.InstallProgress,
        install_target: Path,
        status: str,
    ) -> None:
        nonlocal injected
        original_set_status(progress, install_target, status)
        if status == "not-installed" and not injected:
            injected = True
            target.mkdir(parents=True)
            sentinel.write_text("preserve me\n", encoding="utf-8")

    monkeypatch.setattr(
        installer.InstallProgress,
        "set_status",
        create_target_after_preflight,
    )

    with pytest.raises(FileExistsError, match="refusing to replace"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"


def test_copy_install_cleans_owned_paths_when_publish_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"

    def interrupt_publish(
        _source: Path,
        _target: Path,
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_atomic_rename_noreplace", interrupt_publish)

    with pytest.raises(KeyboardInterrupt):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert not target.exists()
    assert list(target.parent.glob(".xianyu-monitor.*")) == []


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_staged_publish_does_not_replace_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    sentinel = target / "unrelated.txt"
    original_publish = installer._atomic_rename_noreplace
    replaced = False

    def create_target_before_publish(staged: Path, destination: Path) -> None:
        nonlocal replaced
        if destination == target and not replaced:
            replaced = True
            target.mkdir()
            sentinel.write_text("preserve me\n", encoding="utf-8")
        original_publish(staged, destination)

    monkeypatch.setattr(
        installer,
        "_atomic_rename_noreplace",
        create_target_before_publish,
    )

    with pytest.raises(FileExistsError, match="refusing to replace existing path"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode=mode,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert list(target.iterdir()) == [sentinel]
    assert list(target.parent.glob(".xianyu-monitor.install-*")) == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_symlink_publish_does_not_accept_concurrent_same_source_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    original_publish = installer._atomic_rename_noreplace

    def create_same_source_link(staged: Path, destination: Path) -> None:
        if destination == target:
            target.symlink_to(source, target_is_directory=True)
        original_publish(staged, destination)

    monkeypatch.setattr(
        installer,
        "_atomic_rename_noreplace",
        create_same_source_link,
    )

    with pytest.raises(FileExistsError, match="refusing to replace existing path"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="symlink",
        )

    assert target.is_symlink()
    assert target.resolve() == source.resolve()
    assert list(target.parent.glob(".xianyu-monitor.install-*")) == []


def test_copy_fails_closed_without_atomic_noreplace_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"

    def unsupported_publish(_staged: Path, _target: Path) -> None:
        raise OSError(
            installer.errno.ENOTSUP,
            "atomic no-replace publication is unavailable",
        )

    monkeypatch.setattr(installer, "_atomic_rename_noreplace", unsupported_publish)

    with pytest.raises(
        OSError,
        match="atomic no-replace publication is unavailable",
    ):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert not target.exists()
    assert not home.exists()


def test_copy_publishes_complete_staging_with_one_noreplace_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    original_publish = installer._atomic_rename_noreplace
    renames: list[tuple[Path, Path]] = []

    def record_publish(staged: Path, destination: Path) -> None:
        if destination == target:
            renames.append((staged, destination))
        original_publish(staged, destination)

    monkeypatch.setattr(installer, "_atomic_rename_noreplace", record_publish)

    installer.install_skill(
        source=source,
        home=home,
        hosts=["codex"],
        mode="copy",
    )

    assert len(renames) == 1
    assert renames[0][1] == target
    assert renames[0][0].name.startswith(".xianyu-monitor.install-")
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts/monitor.py").is_file()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_all_host_install_rolls_back_if_later_target_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    shared_target = home / ".agents/skills/xianyu-monitor"
    original_copy_skill = installer._copy_skill
    original_symlink_to = Path.symlink_to
    calls = 0

    def fail_second_copy(
        source_path: Path,
        target_path: Path,
        **kwargs: object,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-target failure")
        original_copy_skill(source_path, target_path, **kwargs)

    def fail_second_symlink(
        path: Path, target: Path, target_is_directory: bool = False
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-target failure")
        original_symlink_to(path, target, target_is_directory)

    if mode == "copy":
        monkeypatch.setattr(installer, "_copy_skill", fail_second_copy)
    else:
        monkeypatch.setattr(Path, "symlink_to", fail_second_symlink)

    with pytest.raises(OSError, match="simulated second-target failure"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["all"],
            mode=mode,
        )

    assert not shared_target.exists()
    assert not shared_target.is_symlink()
    assert not home.exists()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_rollback_preserves_concurrently_replaced_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    home = tmp_path / "SENSITIVE_REPLACEMENT_HOME"
    shared_target = home / ".agents/skills/xianyu-monitor"
    displaced_target = home / "owned-target-displaced"
    sentinel = shared_target / "unrelated.txt"
    original_copy = installer._copy_skill
    original_symlink = Path.symlink_to
    calls = 0

    def replace_first_target() -> None:
        shared_target.rename(displaced_target)
        shared_target.mkdir()
        sentinel.write_text("preserve me\n", encoding="utf-8")

    def cancel_second_copy(
        source: Path,
        target: Path,
        **kwargs: object,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            replace_first_target()
            raise KeyboardInterrupt
        original_copy(source, target, **kwargs)

    def cancel_second_symlink(
        path: Path,
        source: Path,
        target_is_directory: bool = False,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            replace_first_target()
            raise KeyboardInterrupt
        original_symlink(path, source, target_is_directory)

    if mode == "copy":
        monkeypatch.setattr(installer, "_copy_skill", cancel_second_copy)
    else:
        monkeypatch.setattr(Path, "symlink_to", cancel_second_symlink)

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "all",
                "--mode",
                mode,
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-established"
    assert payload["installs"][0]["status"] == "not-established"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove an installation target"],
    }
    assert str(home) not in output
    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert displaced_target.exists() or displaced_target.is_symlink()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_rollback_preserves_replacement_swapped_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    source = Path(__file__).resolve().parents[1]
    target = tmp_path / "home/.agents/skills/xianyu-monitor"
    displaced_target = target.parent / "owned-target-displaced"
    replacement_source = tmp_path / "replacement-source"
    target.parent.mkdir(parents=True)
    replacement_source.mkdir()
    if mode == "copy":
        target.mkdir()
        (target / "owned.txt").write_text("owned\n", encoding="utf-8")
    else:
        target.symlink_to(source, target_is_directory=True)
    ownership = installer._capture_target_ownership(target, mode)
    original_rename = Path.rename
    original_symlink = Path.symlink_to
    swapped = False

    def swap_before_rename(path: Path, destination: Path) -> Path:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            original_rename(path, displaced_target)
            if mode == "copy":
                path.mkdir()
                (path / "unrelated.txt").write_text(
                    "preserve me\n",
                    encoding="utf-8",
                )
            else:
                original_symlink(
                    path,
                    replacement_source,
                    target_is_directory=True,
                )
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", swap_before_rename)

    with pytest.raises(OSError, match="refusing to remove changed"):
        installer._remove_created_target(target, mode, source, ownership)

    assert swapped is True
    quarantined = list(target.parent.glob(".xianyu-monitor.rollback-*/target"))
    if installer._supports_safe_quarantine_restore():  # noqa: SLF001
        assert quarantined == []
        assert list(target.parent.glob(".xianyu-monitor.rollback-*")) == []
        preserved_replacement = target
    else:
        assert len(quarantined) == 1
        preserved_replacement = quarantined[0]
    if mode == "copy":
        assert (preserved_replacement / "unrelated.txt").read_text(
            encoding="utf-8"
        ) == "preserve me\n"
        assert (displaced_target / "owned.txt").is_file()
    else:
        assert preserved_replacement.is_symlink()
        assert preserved_replacement.resolve() == replacement_source.resolve()
        assert displaced_target.is_symlink()
        assert displaced_target.resolve() == source.resolve()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX rename can replace an existing destination",
)
def test_posix_quarantine_restore_never_renames_over_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantined = tmp_path / "quarantined"
    destination = tmp_path / "destination"
    quarantined.mkdir()
    destination.mkdir()

    def reject_rename(_path: Path, _destination: Path) -> Path:
        raise AssertionError("unsafe POSIX restoration attempted")

    monkeypatch.setattr(Path, "rename", reject_rename)

    assert installer._restore_quarantined_path(quarantined, destination) is False
    assert quarantined.is_dir()
    assert destination.is_dir()


def test_rollback_preserves_empty_parent_created_by_concurrent_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_CONCURRENT_PARENT"
    original_publish = installer._atomic_rename_noreplace
    injected = False

    def concurrent_parent_creation(staged: Path, destination: Path) -> None:
        nonlocal injected
        if destination == home and not injected:
            injected = True
            home.mkdir()
        original_publish(staged, destination)

    def cancel_before_target(
        _source: Path,
        _target: Path,
        **_kwargs: object,
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        installer,
        "_atomic_rename_noreplace",
        concurrent_parent_creation,
    )
    monkeypatch.setattr(installer, "_copy_skill", cancel_before_target)

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"]["status"] == "complete-or-not-required"
    assert str(home) not in output
    assert home.is_dir()
    assert list(home.iterdir()) == []


def test_parent_staging_cancelled_before_ownership_capture_saves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_UNOWNED_PARENT"

    def cancel_parent_ownership(
        _observed_path: Path,
        **_kwargs: object,
    ) -> installer.ParentOwnership:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        installer,
        "_capture_parent_ownership",
        cancel_parent_ownership,
    )

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"] == {"status": "complete-or-not-required"}
    assert str(home) not in output
    assert not home.exists()


def test_copy_fails_closed_if_owned_parent_is_replaced_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    displaced_parent = home / ".agents/owned-skills-displaced"
    original_copy = installer._copy_skill

    def move_parent_before_copy(
        source_path: Path,
        target_path: Path,
        **kwargs: object,
    ) -> None:
        target_path.parent.rename(displaced_parent)
        target_path.parent.mkdir()
        original_copy(source_path, target_path, **kwargs)

    monkeypatch.setattr(installer, "_copy_skill", move_parent_before_copy)

    with pytest.raises(
        OSError,
        match="installation parent changed before publication",
    ):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert target.parent.is_dir()
    assert list(target.parent.iterdir()) == []
    assert displaced_parent.is_dir()
    assert list(displaced_parent.iterdir()) == []


def test_copy_fails_closed_if_owned_parent_is_replaced_during_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    displaced_parent = home / ".agents/owned-skills-displaced"
    progress = installer.InstallProgress(mode="copy", dry_run=False)
    original_create = installer._create_private_directory
    replaced = False

    def replace_parent_after_staging_created(
        temporary: Path,
    ) -> None:
        nonlocal replaced
        original_create(temporary)
        if not replaced and temporary.name.startswith(
            f".{installer.SKILL_NAME}.install-"
        ):
            replaced = True
            target.parent.rename(displaced_parent)
            target.parent.mkdir()

    monkeypatch.setattr(
        installer,
        "_create_private_directory",
        replace_parent_after_staging_created,
    )

    with pytest.raises(
        OSError,
        match="installation parent changed before publication",
    ):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
            progress=progress,
        )

    assert target.parent.is_dir()
    assert list(target.parent.iterdir()) == []
    assert displaced_parent.is_dir()
    assert len(list(displaced_parent.glob(".xianyu-monitor.install-*"))) == 1
    assert progress.cleanup_failures == [
        "private installation staging state could not be established"
    ]


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_target_staging_creation_interruption_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    home = tmp_path / "SENSITIVE_STAGING_CREATE_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_create = installer._create_private_directory
    interrupted = False

    def create_then_interrupt(path: Path) -> None:
        nonlocal interrupted
        original_create(path)
        if not interrupted and path.name.startswith(
            f".{installer.SKILL_NAME}.install-"
        ):
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        installer,
        "_create_private_directory",
        create_then_interrupt,
    )

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                mode,
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == "KeyboardInterrupt"
    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"] == {"status": "complete-or-not-required"}
    assert str(home) not in output
    assert not target.exists()
    assert not home.exists()


def test_parent_staging_creation_interruption_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_PARENT_STAGING_HOME"
    original_create = installer._create_private_directory
    interrupted = False

    def create_then_interrupt(path: Path) -> None:
        nonlocal interrupted
        original_create(path)
        if not interrupted and not path.name.startswith(
            f".{installer.SKILL_NAME}.install-"
        ):
            interrupted = True
            raise asyncio.CancelledError

    monkeypatch.setattr(
        installer,
        "_create_private_directory",
        create_then_interrupt,
    )

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == "CancelledError"
    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"] == {"status": "complete-or-not-required"}
    assert str(home) not in output
    assert not home.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_symlink_staging_cleanup_failure_cannot_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    progress = installer.InstallProgress(mode="symlink", dry_run=False)
    original_rmdir = Path.rmdir

    def fail_private_staging_cleanup(path: Path) -> None:
        if path.name.startswith(f".{installer.SKILL_NAME}.install-"):
            raise OSError("SENSITIVE_STAGING_CLEANUP_DETAIL")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_private_staging_cleanup)

    with pytest.raises(OSError, match="cleanup also failed"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="symlink",
            progress=progress,
        )

    assert not target.exists()
    assert not target.is_symlink()
    assert progress.public_evidence()["installation"]["status"] == "not-installed"
    assert progress.cleanup_failures == [
        "failed to remove private installation staging"
    ]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_copy_publish_cancellation_survives_staging_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
) -> None:
    home = tmp_path / "SENSITIVE_STAGING_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_publish = installer._atomic_rename_noreplace
    original_rmtree = installer.shutil.rmtree

    def cancel_target_publish(staged: Path, destination: Path) -> None:
        if destination == target:
            raise interruption()
        original_publish(staged, destination)

    def fail_private_staging_cleanup(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if path.name.startswith(f".{installer.SKILL_NAME}.install-"):
            raise OSError("SENSITIVE_STAGING_CLEANUP_DETAIL")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        installer,
        "_atomic_rename_noreplace",
        cancel_target_publish,
    )
    monkeypatch.setattr(
        installer.shutil,
        "rmtree",
        fail_private_staging_cleanup,
    )

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == interruption.__name__
    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove private installation staging"],
    }
    assert str(home) not in output
    assert "SENSITIVE_STAGING_CLEANUP_DETAIL" not in output
    assert not target.exists()
    assert len(list(target.parent.glob(".xianyu-monitor.install-*"))) == 1


def test_rollback_preserves_replaced_empty_parent_without_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_REPLACED_PARENT"
    parent = home / ".agents/skills"
    displaced_parent = home / ".agents/owned-skills-displaced"

    def replace_parent_then_cancel(
        _source: Path,
        target: Path,
        **_kwargs: object,
    ) -> None:
        target.parent.rename(displaced_parent)
        target.parent.mkdir()
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_copy_skill", replace_parent_then_cancel)

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"]["status"] == "complete-or-not-required"
    assert str(home) not in output
    assert parent.is_dir()
    assert list(parent.iterdir()) == []
    assert displaced_parent.is_dir()


def test_parent_cleanup_preserves_replacement_swapped_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "home/.agents/skills"
    displaced_parent = parent.parent / "owned-parent-displaced"
    parent.mkdir(parents=True)
    ownership = installer._capture_parent_ownership(parent)
    original_rename = Path.rename
    swapped = False

    def swap_before_rename(path: Path, destination: Path) -> Path:
        nonlocal swapped
        if path == parent and not swapped:
            swapped = True
            original_rename(path, displaced_parent)
            path.mkdir()
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", swap_before_rename)

    if installer._supports_safe_quarantine_restore():  # noqa: SLF001
        installer._remove_created_parent(ownership)
    else:
        with pytest.raises(OSError, match="parent changed"):
            installer._remove_created_parent(ownership)

    assert swapped is True
    quarantined = list(parent.parent.glob(".skills.rollback-*/parent"))
    if installer._supports_safe_quarantine_restore():  # noqa: SLF001
        assert quarantined == []
        assert list(parent.parent.glob(".skills.rollback-*")) == []
        assert parent.is_dir()
        assert list(parent.iterdir()) == []
    else:
        assert len(quarantined) == 1
        assert quarantined[0].is_dir()
        assert list(quarantined[0].iterdir()) == []
    assert displaced_parent.is_dir()


def test_all_parent_paths_are_preflighted_before_install(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    blocker = home / ".claude"
    blocker.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="install parent"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["all"],
            mode="copy",
        )

    assert not (home / ".agents").exists()
    assert blocker.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_copy_mode_rejects_existing_symlink_install(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"
    target.parent.mkdir(parents=True)
    target.symlink_to(source, target_is_directory=True)

    with pytest.raises(FileExistsError, match="existing install mode is symlink"):
        installer.install_skill(
            source=source,
            home=home,
            hosts=["codex"],
            mode="copy",
        )

    assert target.is_symlink()
    assert target.resolve() == source.resolve()


def test_symlink_mode_rejects_existing_copy_directory(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    installer.install_skill(
        source=source,
        home=home,
        hosts=["codex"],
        mode="copy",
    )
    copied_source = home / ".agents/skills/xianyu-monitor"

    with pytest.raises(FileExistsError, match="existing install mode is directory"):
        installer.install_skill(
            source=copied_source,
            home=home,
            hosts=["codex"],
            mode="symlink",
        )

    assert copied_source.is_dir()
    assert not copied_source.is_symlink()


def test_installer_rejects_mismatched_skill_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_complete_source(source, name="another-skill")

    with pytest.raises(ValueError, match="name must be"):
        install_skill(
            source=source,
            home=tmp_path / "home",
            hosts=["codex"],
            mode="copy",
        )


def test_all_targets_are_preflighted_before_install(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    conflicting = home / ".claude/skills/xianyu-monitor"
    conflicting.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        install_skill(
            source=source,
            home=home,
            hosts=["all"],
            mode="copy",
        )

    assert not (home / ".agents/skills/xianyu-monitor").exists()


def test_installer_rejects_missing_required_resource_without_partial_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    (source / "scripts/spider.py").unlink()
    home = tmp_path / "home"

    with pytest.raises(ValueError, match="missing required skill resource"):
        install_skill(
            source=source,
            home=home,
            hosts=["claude"],
            mode="copy",
        )

    assert not (home / ".claude/skills/xianyu-monitor").exists()


def test_cli_success_keeps_dry_run_target_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "ok": True,
        "dry_run": True,
        "installs": [
            {
                "hosts": ["codex"],
                "mode": "copy",
                "status": "planned",
                "target": str(target),
            }
        ],
    }
    assert not home.exists()


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_cli_preflight_cancellation_is_path_private_and_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    home = tmp_path / "SENSITIVE_HOME"

    def cancel_preflight(_skill_file: Path) -> str:
        raise interruption()

    monkeypatch.setattr(installer, "_read_skill_name", cancel_preflight)

    assert installer.main(["--home", str(home), "--host", "codex"]) == 130
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["ok"] is False
    assert payload["error_type"] == error_type
    assert payload["installation"]["status"] == "not-installed"
    assert payload["installs"] == []
    assert payload["cleanup"]["status"] == "complete-or-not-required"
    assert str(home) not in output
    assert not home.exists()


def test_cli_copy_commit_cancellation_rolls_back_and_reports_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_COPY_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_copy = installer._copy_skill

    def copy_then_cancel(
        source: Path,
        install_target: Path,
        **kwargs: object,
    ) -> None:
        original_copy(source, install_target, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_copy_skill", copy_then_cancel)

    assert (
        installer.main(["--home", str(home), "--host", "codex", "--mode", "copy"])
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["installs"] == [
        {
            "hosts": ["codex"],
            "mode": "copy",
            "status": "not-installed",
        }
    ]
    assert str(home) not in output
    assert not target.exists()
    assert not home.exists()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_cli_cancellation_before_staged_ownership_capture_saves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    if mode == "symlink" and os.name == "nt":
        pytest.skip("Windows directory symlinks depend on host policy")

    home = tmp_path / "SENSITIVE_UNOWNED_HOME"
    target = home / ".agents/skills/xianyu-monitor"

    def cancel_ownership_capture(
        _observed_path: Path,
        _mode: str,
        **_kwargs: object,
    ) -> installer.TargetOwnership:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        installer,
        "_capture_target_ownership",
        cancel_ownership_capture,
    )

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                mode,
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["installs"][0]["status"] == "not-installed"
    assert payload["cleanup"] == {"status": "complete-or-not-required"}
    assert str(home) not in output
    assert not target.exists()
    assert not target.is_symlink()
    assert not home.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_cli_symlink_commit_cancellation_rolls_back_and_reports_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_LINK_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_set_status = installer.InstallProgress.set_status
    cancelled = False

    def cancel_after_owned_commit(
        progress: installer.InstallProgress,
        install_target: Path,
        status: str,
    ) -> None:
        nonlocal cancelled
        original_set_status(progress, install_target, status)
        if status == "installed" and not cancelled:
            cancelled = True
            raise asyncio.CancelledError

    monkeypatch.setattr(
        installer.InstallProgress,
        "set_status",
        cancel_after_owned_commit,
    )

    assert installer.main(["--home", str(home), "--host", "codex"]) == 130
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == "CancelledError"
    assert payload["installation"]["status"] == "not-installed"
    assert payload["installs"][0]["status"] == "not-installed"
    assert str(home) not in output
    assert not target.exists()
    assert not home.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_cli_cancelled_install_with_failed_rollback_is_not_established(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_UNCERTAIN_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_set_status = installer.InstallProgress.set_status
    cancelled = False

    def cancel_after_owned_commit(
        progress: installer.InstallProgress,
        install_target: Path,
        status: str,
    ) -> None:
        nonlocal cancelled
        original_set_status(progress, install_target, status)
        if status == "installed" and not cancelled:
            cancelled = True
            raise KeyboardInterrupt

    def fail_rollback(
        _target: Path,
        _mode: str,
        _source: Path,
        _ownership: installer.TargetOwnership,
    ) -> None:
        raise OSError("SENSITIVE_ROLLBACK_DETAIL")

    monkeypatch.setattr(
        installer.InstallProgress,
        "set_status",
        cancel_after_owned_commit,
    )
    monkeypatch.setattr(installer, "_remove_created_target", fail_rollback)

    assert installer.main(["--home", str(home), "--host", "codex"]) == 130
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-established"
    assert payload["installs"][0]["status"] == "not-established"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove an installation target"],
    }
    assert str(home) not in output
    assert "SENSITIVE_ROLLBACK_DETAIL" not in output
    assert target.is_symlink()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_cli_parent_cleanup_cancellation_keeps_primary_and_generic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_PARENT_HOME"
    original_set_status = installer.InstallProgress.set_status
    original_remove_parent = installer._remove_created_parent
    install_cancelled = False
    cleanup_cancelled = False

    def cancel_after_owned_commit(
        progress: installer.InstallProgress,
        install_target: Path,
        status: str,
    ) -> None:
        nonlocal install_cancelled
        original_set_status(progress, install_target, status)
        if status == "installed" and not install_cancelled:
            install_cancelled = True
            raise KeyboardInterrupt

    def cancel_parent_cleanup(ownership: installer.ParentOwnership) -> None:
        nonlocal cleanup_cancelled
        if not cleanup_cancelled and ownership.path.name == "skills":
            cleanup_cancelled = True
            raise asyncio.CancelledError
        original_remove_parent(ownership)

    monkeypatch.setattr(
        installer.InstallProgress,
        "set_status",
        cancel_after_owned_commit,
    )
    monkeypatch.setattr(
        installer,
        "_remove_created_parent",
        cancel_parent_cleanup,
    )

    assert installer.main(["--home", str(home), "--host", "codex"]) == 130
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == "KeyboardInterrupt"
    assert payload["installation"]["status"] == "not-installed"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove an installation parent directory"],
    }
    assert str(home) not in output


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_cli_success_json_cancellation_reports_completed_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    home = tmp_path / "SENSITIVE_SUCCESS_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    original_dumps = installer.json.dumps
    interrupted = False

    def cancel_success_json(
        payload: object,
        *args: object,
        **kwargs: object,
    ) -> str:
        nonlocal interrupted
        if isinstance(payload, dict) and payload.get("ok") is True and not interrupted:
            interrupted = True
            raise interruption()
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(installer.json, "dumps", cancel_success_json)

    assert (
        installer.main(["--home", str(home), "--host", "codex", "--mode", "copy"])
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["error_type"] == error_type
    assert payload["installation"]["status"] == "installed"
    assert payload["installs"] == [
        {
            "hosts": ["codex"],
            "mode": "copy",
            "status": "installed",
        }
    ]
    assert str(home) not in output
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()


def test_cli_dry_run_json_cancellation_maps_planned_to_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "SENSITIVE_DRY_RUN_HOME"
    original_dumps = installer.json.dumps
    interrupted = False

    def cancel_success_json(
        payload: object,
        *args: object,
        **kwargs: object,
    ) -> str:
        nonlocal interrupted
        if isinstance(payload, dict) and payload.get("ok") is True and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(installer.json, "dumps", cancel_success_json)

    assert (
        installer.main(
            [
                "--home",
                str(home),
                "--host",
                "codex",
                "--mode",
                "copy",
                "--dry-run",
            ]
        )
        == 130
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "not-installed"
    assert payload["installs"][0]["status"] == "not-installed"
    assert str(home) not in output
    assert not home.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows directory symlinks depend on host policy",
)
def test_cli_existing_install_json_cancellation_maps_to_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "SENSITIVE_EXISTING_HOME"
    target = home / ".agents/skills/xianyu-monitor"
    target.parent.mkdir(parents=True)
    target.symlink_to(source, target_is_directory=True)
    original_dumps = installer.json.dumps
    interrupted = False

    def cancel_success_json(
        payload: object,
        *args: object,
        **kwargs: object,
    ) -> str:
        nonlocal interrupted
        if isinstance(payload, dict) and payload.get("ok") is True and not interrupted:
            interrupted = True
            raise asyncio.CancelledError
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(installer.json, "dumps", cancel_success_json)

    assert installer.main(["--home", str(home), "--host", "codex"]) == 130
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["installation"]["status"] == "installed"
    assert payload["installs"][0]["status"] == "installed"
    assert str(home) not in output
    assert target.is_symlink()
    assert target.resolve() == source.resolve()
