from __future__ import annotations

import builtins
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import demo
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_demo_is_deterministic_and_marks_everything_synthetic() -> None:
    first = demo.build_demo()
    second = demo.build_demo()

    assert first == second
    assert first["ok"] is True
    assert first["demo"] == {
        "status": "synthetic",
        "network": "not-used",
        "credentials": "not-used",
        "local_writes": "none",
        "claims_real_xianyu_state": False,
    }
    assert first["evaluation"]["passed"] is True
    assert first["delivery_preview"]["event_count"] == 1
    assert first["next_action"]["code"] == "run-setup"


def test_demo_strips_fields_that_must_not_reach_analysis_or_delivery() -> None:
    report = demo.build_demo()
    serialized = json.dumps(
        {
            "analysis": report["analysis"],
            "delivery_preview": report["delivery_preview"],
        },
        ensure_ascii=False,
    )

    for forbidden in (
        "synthetic-seller-not-forwarded",
        "images.invalid",
        "javascript:not-forwarded",
    ):
        assert forbidden not in serialized
    assert "https://www.goofish.com/item?id=demo-16gb-local" in serialized
    complete_demo = json.dumps(report, ensure_ascii=False)
    assert "javascript:not-forwarded" not in complete_demo
    assert all(
        item["url"].startswith("https://www.goofish.com/item?id=")
        for item in report["search"]["items"]
    )


def test_demo_performs_no_network_or_local_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("offline demo must not use external I/O")

    monkeypatch.setattr(demo.deliver, "send_delivery", reject)
    monkeypatch.setattr(demo.analyze, "_request_completion", reject)
    monkeypatch.setattr(builtins, "open", reject)

    assert demo.build_demo()["ok"] is True


def test_demo_cli_runs_from_foreign_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/xianyu.py"), "demo"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["demo"]["status"] == "synthetic"
    assert payload["delivery_preview"]["adapter"] == "webhook"
