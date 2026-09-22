from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import version_info

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()


def test_repository_version_is_valid_semver() -> None:
    assert version_info.read_version() == EXPECTED_VERSION


def test_version_payload_has_stable_capability_discovery() -> None:
    payload = version_info.capability_payload()

    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert payload["skill"] == {
        "name": "xianyu-monitor",
        "version": EXPECTED_VERSION,
    }
    capability_ids = [item["id"] for item in payload["capabilities"]]
    assert capability_ids == sorted(capability_ids)
    assert {"setup", "state", "search", "analyze"} <= set(capability_ids)
    assert all(item["status"] == "available" for item in payload["capabilities"])
    assert payload["contracts"]["task_schema"] == 3
    assert payload["contracts"]["task_transfer_schema"] == 1
    assert payload["contracts"]["outbox_schema"] == 1
    assert payload["contracts"]["delivery_schema"] == 1
    assert payload["contracts"]["ai_run_evidence_schema"] == 1
    assert payload["contracts"]["ai_evaluation_schema"] == 1
    assert payload["contracts"]["ai_feedback_schema"] == 1
    assert payload["contracts"]["demo_schema"] == 1


@pytest.mark.parametrize(
    "content",
    ["", "v2.0.0\n", "02.0.0\n", "2.0\n", "2.0.0/../../bad\n", "\N{SNOWMAN}\n"],
)
def test_version_reader_rejects_invalid_content(
    tmp_path: Path,
    content: str,
) -> None:
    (tmp_path / "VERSION").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="VERSION"):
        version_info.read_version(tmp_path)


def test_version_cli_runs_from_foreign_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/version_info.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["skill"]["version"] == EXPECTED_VERSION
    assert payload["schema_version"] == 1


def test_version_short_output_is_semver(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/version_info.py"), "--short"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"{EXPECTED_VERSION}\n"
