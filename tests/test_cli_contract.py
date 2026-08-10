from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "arguments"),
    [
        ("spider.py", ["--pages", "not-an-integer"]),
        ("monitor.py", ["--not-a-monitor-option"]),
        ("task_manager.py", ["create"]),
        ("login_state.py", ["--timeout", "not-an-integer"]),
        ("create_state.py", ["--output", "state.json"]),
        ("install_skill.py", ["--host", "not-a-host"]),
        ("cdp_profile.py", []),
    ],
)
def test_argument_errors_are_machine_readable_json(
    script: str,
    arguments: list[str],
    tmp_path: Path,
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / script), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_type"] == "ArgumentError"
    assert payload["error"]


@pytest.mark.skipif(
    not hasattr(signal, "raise_signal") or not hasattr(signal, "SIGTERM"),
    reason="SIGTERM signal delivery is unavailable",
)
def test_sigterm_uses_the_controlled_cancellation_path(tmp_path: Path) -> None:
    code = """
import signal
from cli_contract import sigterm_cancellable

@sigterm_cancellable
def main():
    try:
        signal.raise_signal(signal.SIGTERM)
    except KeyboardInterrupt as exc:
        print(type(exc).__name__)
        return 130
    return 0

raise SystemExit(main())
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 130
    assert result.stdout.strip() == "SigtermCancellation"
    assert result.stderr == ""


@pytest.mark.skipif(
    not hasattr(signal, "raise_signal") or not hasattr(signal, "SIGTERM"),
    reason="SIGTERM signal delivery is unavailable",
)
def test_sigterm_before_entrypoint_try_still_emits_json(tmp_path: Path) -> None:
    code = """
import signal
from cli_contract import sigterm_cancellable

@sigterm_cancellable
def main():
    signal.raise_signal(signal.SIGTERM)
    return 0

raise SystemExit(main())
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 130
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_type"] == "SigtermCancellation"
    assert payload["cleanup"] == {"status": "complete-or-not-required"}
    assert result.stderr == ""
