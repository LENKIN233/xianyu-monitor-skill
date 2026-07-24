from __future__ import annotations

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

    def create_racing_target(source_path: Path, target_path: Path) -> None:
        target_path.mkdir(parents=True)
        original_copy_skill(source_path, target_path)

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


def test_copy_install_cleans_owned_paths_when_publish_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _make_complete_source(source)
    home = tmp_path / "home"
    target = home / ".agents/skills/xianyu-monitor"

    def interrupt_publish(_source: Path, _target: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(installer.os, "replace", interrupt_publish)

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

    def fail_second_copy(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-target failure")
        original_copy_skill(source_path, target_path)

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
