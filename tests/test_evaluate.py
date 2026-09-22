from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import evaluate
import pytest


def _analysis() -> dict[str, object]:
    hashes = {
        field: (str(index) * 64)[:64]
        for index, field in enumerate(evaluate.RUN_HASH_FIELDS, start=1)
    }
    result: dict[str, object] = {
        "ok": True,
        "summary": "test summary",
        "items": [
            {"id": "a", "score": 90, "match_level": "high_match"},
            {"id": "b", "score": 20, "match_level": "low_match"},
        ],
        "run_evidence": {
            "schema_version": 1,
            "model": "test-model",
            **hashes,
        },
    }
    output = {"summary": result["summary"], "items": result["items"]}
    encoded = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    result["run_evidence"]["output_sha256"] = hashlib.sha256(  # type: ignore[index]
        encoded
    ).hexdigest()
    return result


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_golden_evaluation_passes_and_is_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    analysis_path = tmp_path / "analysis.json"
    golden_path = tmp_path / "golden.json"
    _write(analysis_path, _analysis())
    _write(
        golden_path,
        {
            "schema_version": 1,
            "cases": [
                {
                    "item_id": "a",
                    "expected_label": "relevant",
                    "minimum_score": 80,
                },
                {"item_id": "b", "expected_label": "irrelevant"},
            ],
        },
    )

    assert (
        evaluate.main(
            [
                "golden",
                "--analysis",
                str(analysis_path),
                "--golden",
                str(golden_path),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert report["evaluation"]["passed"] is True
    assert report["evaluation"]["accuracy"] == 1.0
    assert report["run_evidence"]["model"] == "test-model"


def test_golden_regression_returns_one_with_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    analysis_path = tmp_path / "analysis.json"
    golden_path = tmp_path / "golden.json"
    _write(analysis_path, _analysis())
    _write(
        golden_path,
        {
            "schema_version": 1,
            "cases": [{"item_id": "a", "expected_label": "irrelevant"}],
        },
    )

    assert (
        evaluate.main(
            [
                "golden",
                "--analysis",
                str(analysis_path),
                "--golden",
                str(golden_path),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["evaluation"]["passed"] is False
    assert report["evaluation"]["cases"][0]["observed_label"] == "relevant"


def test_feedback_capture_is_private_minimal_and_no_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    analysis_path = tmp_path / "analysis.json"
    output_path = tmp_path / "feedback.json"
    _write(analysis_path, _analysis())

    arguments = [
        "feedback",
        "--analysis",
        str(analysis_path),
        "--output",
        str(output_path),
        "--item-id",
        "a",
        "--label",
        "relevant",
        "--note",
        "manual inspection agrees",
    ]
    assert evaluate.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    record = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["feedback"]["status"] == "recorded"
    assert record["observed"] == {"match_level": "high_match", "score": 90}
    assert record["run_evidence"]["model"] == "test-model"
    assert "title" not in json.dumps(record)
    if os.name == "posix":
        assert stat.S_IMODE(output_path.stat().st_mode) == 0o600

    assert evaluate.main(arguments) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]


def test_feedback_rejects_missing_item_without_creating_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    analysis_path = tmp_path / "analysis.json"
    output_path = tmp_path / "feedback.json"
    _write(analysis_path, _analysis())

    assert (
        evaluate.main(
            [
                "feedback",
                "--analysis",
                str(analysis_path),
                "--output",
                str(output_path),
                "--item-id",
                "absent",
                "--label",
                "unknown",
            ]
        )
        == 2
    )
    assert not output_path.exists()
    assert "absent" in json.loads(capsys.readouterr().out)["error"]


def test_analysis_requires_valid_run_hashes() -> None:
    payload = _analysis()
    payload["run_evidence"]["input_sha256"] = "not-a-hash"  # type: ignore[index]

    with pytest.raises(evaluate.EvaluationInputError, match="input_sha256"):
        evaluate._validated_analysis(payload)


def test_analysis_rejects_output_changed_after_evidence_was_recorded() -> None:
    payload = _analysis()
    payload["items"][0]["score"] = 1  # type: ignore[index]

    with pytest.raises(evaluate.EvaluationInputError, match="does not match"):
        evaluate._validated_analysis(payload)
