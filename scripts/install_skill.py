#!/usr/bin/env python3
"""Install this skill into common Agent Skills discovery directories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

SKILL_NAME = "xianyu-monitor"
HOST_ROOTS = {
    "codex": Path(".agents/skills"),
    "claude": Path(".claude/skills"),
    # Current OpenClaw releases also discover the shared Agent Skills root.
    "openclaw": Path(".agents/skills"),
}
REQUIRED_COPY_FILES = (
    "references/api_reference.md",
    "references/architecture.md",
    "references/host_adapters.md",
    "requirements.txt",
    "scripts/__init__.py",
    "scripts/create_state.py",
    "scripts/install_skill.py",
    "scripts/login_state.py",
    "scripts/monitor.py",
    "scripts/spider.py",
    "scripts/task_manager.py",
    "SKILL.md",
)
OPTIONAL_COPY_FILES = (
    "LICENSE",
    "README.md",
    "agents/openai.yaml",
    "scripts/state_example.json",
)
COPY_FILES = (
    *OPTIONAL_COPY_FILES,
    *REQUIRED_COPY_FILES[:-1],
    # Copy the discovery entrypoint last so scanners never see a partial skill.
    "SKILL.md",
)


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


def _copy_skill(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}.install-", dir=target.parent)
    )
    target_created = False
    try:
        for relative_name in COPY_FILES:
            source_entry = source / relative_name
            if not source_entry.is_file():
                continue
            target_entry = temporary / relative_name
            target_entry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_entry, target_entry)

        _validate_staged_copy(temporary)

        try:
            target.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to replace existing path: {target}"
            ) from exc
        target_created = True

        for relative_name in COPY_FILES:
            staged_entry = temporary / relative_name
            if not staged_entry.is_file():
                continue
            target_entry = target / relative_name
            target_entry.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_entry, target_entry)
    except BaseException:
        if target_created:
            shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    else:
        shutil.rmtree(temporary)


def _remove_created_target(target: Path, mode: str, source: Path) -> None:
    if mode == "symlink":
        if not target.is_symlink() or target.resolve() != source:
            if target.exists() or target.is_symlink():
                raise OSError(f"refusing to remove changed install target: {target}")
            return
        target.unlink(missing_ok=True)
    elif target.exists():
        shutil.rmtree(target)


def _missing_parent_directories(parent: Path) -> list[Path]:
    missing: list[Path] = []
    candidate = parent
    while not candidate.exists() and not candidate.is_symlink():
        missing.append(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return missing


def _validate_parent_chain(parent: Path) -> None:
    candidate = parent
    while not candidate.exists():
        if candidate.is_symlink():
            raise NotADirectoryError(f"install parent is not a directory: {candidate}")
        candidate = candidate.parent
    if not candidate.is_dir():
        raise NotADirectoryError(f"install parent is not a directory: {candidate}")


def install_skill(
    *,
    source: Path,
    home: Path,
    hosts: list[str],
    mode: str,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Install the skill and return one record per distinct discovery target."""

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
    if mode not in {"symlink", "copy"}:
        raise ValueError("mode must be 'symlink' or 'copy'")
    if not hosts:
        raise ValueError("select at least one host")
    invalid_hosts = set(hosts) - {*HOST_ROOTS, "all"}
    if invalid_hosts:
        raise ValueError(f"unknown host: {sorted(invalid_hosts)[0]}")

    selections = _selected_targets(hosts, home)
    for selection in selections:
        target = selection["target"]
        _validate_parent_chain(target.parent)
        if target.is_symlink():
            if target.resolve() != source:
                raise FileExistsError(f"refusing to replace existing symlink: {target}")
            if mode != "symlink":
                raise FileExistsError(
                    f"existing install mode is symlink, requested {mode}: {target}"
                )
        elif target.exists():
            if target.resolve() != source:
                raise FileExistsError(f"refusing to replace existing path: {target}")
            if mode != "copy":
                raise FileExistsError(
                    f"existing install mode is directory, requested {mode}: {target}"
                )

    records: list[dict[str, Any]] = []
    created_targets: list[Path] = []
    created_parent_candidates: set[Path] = set()
    try:
        for selection in selections:
            target = selection["target"]
            hosts_for_target = selection["hosts"]

            if target.is_symlink():
                status = "already-installed"
            elif target.exists():
                status = "already-installed"
            elif dry_run:
                status = "planned"
            elif mode == "symlink":
                created_parent_candidates.update(
                    _missing_parent_directories(target.parent)
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
                created_targets.append(target)
                status = "installed"
            else:
                created_parent_candidates.update(
                    _missing_parent_directories(target.parent)
                )
                _copy_skill(source, target)
                created_targets.append(target)
                status = "installed"

            records.append(
                {
                    "hosts": hosts_for_target,
                    "mode": mode,
                    "status": status,
                    "target": str(target),
                }
            )
    except BaseException as install_error:
        cleanup_failures: list[str] = []
        for target in reversed(created_targets):
            try:
                _remove_created_target(target, mode, source)
            except OSError:
                cleanup_failures.append(str(target))
        for parent in sorted(
            created_parent_candidates,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # Preserve a directory that gained content concurrently. If it
                # is still empty, surface the cleanup failure to the caller.
                try:
                    is_empty = parent.is_dir() and not any(parent.iterdir())
                except OSError:
                    is_empty = True
                if is_empty:
                    cleanup_failures.append(str(parent))
        if cleanup_failures:
            paths = ", ".join(cleanup_failures)
            raise OSError(
                f"installation failed ({install_error}); cleanup also failed: {paths}"
            ) from install_error
        raise
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
        default="symlink",
        help="link to this checkout or copy distributable files",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    try:
        records = install_skill(
            source=source,
            home=args.home,
            hosts=args.host or ["all"],
            mode=args.mode,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 2

    print(
        json.dumps(
            {"ok": True, "dry_run": args.dry_run, "installs": records},
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
