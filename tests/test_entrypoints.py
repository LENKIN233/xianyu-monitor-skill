from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_monitor_module_entrypoint_from_skill_root() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scripts.monitor", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--tasks-file" in result.stdout


def test_monitor_script_entrypoint_from_foreign_working_directory(
    tmp_path: Path,
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/monitor.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--quiet-if-empty" in result.stdout


def test_login_state_script_help_from_foreign_working_directory(
    tmp_path: Path,
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/login_state.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--browser-channel" in result.stdout
    assert "--cdp-user-data-dir" in result.stdout
    assert "--confirm-in-browser" in result.stdout


def test_cdp_profile_script_help_from_foreign_working_directory(
    tmp_path: Path,
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/cdp_profile.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--directory" in result.stdout
    assert "--cleanup" in result.stdout


def test_json_stdout_is_ascii_safe_under_restrictive_encoding(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "ascii"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts/task_manager.py"),
            "--data-file",
            str(tmp_path / "tasks.json"),
            "create",
            "测试",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.isascii()
    assert json.loads(result.stdout)["result"]["keyword"] == "测试"
