#!/usr/bin/env python3
"""Evaluate AI analysis offline and capture privacy-minimized feedback records."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable

EVALUATION_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_GOLDEN_CASES = 1_000
MAX_NOTE_CHARS = 1_000
RUN_HASH_FIELDS = (
    "provider_sha256",
    "input_sha256",
    "prompt_sha256",
    "schema_sha256",
    "request_sha256",
    "output_sha256",
)
LABELS = {"relevant", "irrelevant", "unknown"}
LEVEL_LABELS = {
    "high_match": "relevant",
    "medium_match": "relevant",
    "low_match": "irrelevant",
    "insufficient_evidence": "unknown",
}


class EvaluationInputError(ValueError):
    """An analysis or golden document is invalid or unsafe."""


def _absolute_path(value: str) -> str:
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError("path must be absolute") from exc
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return str(path)


def _read_json(path: str, *, label: str) -> Any:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise EvaluationInputError(f"{label} file is unreadable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise EvaluationInputError(f"{label} file exceeds the 2 MiB safety limit")
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: _reject_nonfinite(),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise EvaluationInputError(f"{label} file is not valid UTF-8 JSON") from exc


def _reject_nonfinite() -> None:
    raise ValueError("non-finite JSON value")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_analysis(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise EvaluationInputError("analysis must be a successful analysis result")
    items = payload.get("items")
    summary = payload.get("summary")
    evidence = payload.get("run_evidence")
    if (
        not isinstance(items, list)
        or not isinstance(summary, str)
        or not isinstance(evidence, dict)
    ):
        raise EvaluationInputError("analysis is missing items or run evidence")
    hashes: dict[str, Any] = {
        "schema_version": evidence.get("schema_version"),
        "model": evidence.get("model"),
    }
    for field in RUN_HASH_FIELDS:
        value = evidence.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise EvaluationInputError(f"analysis run evidence has invalid {field}")
        hashes[field] = value
    if hashes["schema_version"] != 1 or not isinstance(hashes["model"], str):
        raise EvaluationInputError("analysis run evidence has an unsupported schema")
    if hashes["output_sha256"] != _canonical_sha256(
        {"summary": summary, "items": items}
    ):
        raise EvaluationInputError("analysis output does not match its run evidence")

    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise EvaluationInputError("analysis items must be objects")
        item_id = item.get("id")
        level = item.get("match_level")
        score = item.get("score")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in by_id
            or level not in LEVEL_LABELS
            or isinstance(score, bool)
            or not isinstance(score, int)
            or not 0 <= score <= 100
        ):
            raise EvaluationInputError("analysis contains an invalid item")
        by_id[item_id] = {"match_level": level, "score": score}
    return by_id, hashes


def _validated_golden(payload: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "cases"}
        or payload.get("schema_version") != EVALUATION_SCHEMA_VERSION
        or not isinstance(payload.get("cases"), list)
        or not 1 <= len(payload["cases"]) <= MAX_GOLDEN_CASES
    ):
        raise EvaluationInputError("golden dataset has an unsupported schema")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_case in payload["cases"]:
        if not isinstance(raw_case, dict) or not set(raw_case) <= {
            "item_id",
            "expected_label",
            "minimum_score",
            "maximum_score",
        }:
            raise EvaluationInputError("golden dataset contains an invalid case")
        item_id = raw_case.get("item_id")
        expected = raw_case.get("expected_label")
        minimum = raw_case.get("minimum_score", 0)
        maximum = raw_case.get("maximum_score", 100)
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in seen_ids
            or expected not in LABELS
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= minimum <= maximum <= 100
        ):
            raise EvaluationInputError("golden dataset contains an invalid case")
        seen_ids.add(item_id)
        cases.append(
            {
                "item_id": item_id,
                "expected_label": expected,
                "minimum_score": minimum,
                "maximum_score": maximum,
            }
        )
    return cases


def evaluate_golden(
    analysis_items: dict[str, dict[str, Any]],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in cases:
        item = analysis_items.get(case["item_id"])
        if item is None:
            results.append(
                {"item_id": case["item_id"], "passed": False, "reason": "missing"}
            )
            continue
        observed_label = LEVEL_LABELS[item["match_level"]]
        label_passed = observed_label == case["expected_label"]
        score_passed = case["minimum_score"] <= item["score"] <= case["maximum_score"]
        results.append(
            {
                "item_id": case["item_id"],
                "passed": label_passed and score_passed,
                "expected_label": case["expected_label"],
                "observed_label": observed_label,
                "observed_score": item["score"],
                "score_range": [case["minimum_score"], case["maximum_score"]],
            }
        )
    passed_count = sum(result["passed"] for result in results)
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "passed": passed_count == len(results),
        "case_count": len(results),
        "passed_count": passed_count,
        "failed_count": len(results) - passed_count,
        "accuracy": passed_count / len(results) if results else 1.0,
        "cases": results,
    }


def _write_private_new_json(path: str, payload: dict[str, Any]) -> None:
    output = Path(path)
    parent = output.parent
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise EvaluationInputError("feedback output directory is unavailable") from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise EvaluationInputError("feedback output directory is unavailable")
    temporary = parent / f".{output.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        data = (
            json.dumps(
                payload,
                ensure_ascii=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    except FileExistsError as exc:
        raise EvaluationInputError("feedback output already exists") from exc
    except OSError as exc:
        raise EvaluationInputError("feedback output could not be written") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _feedback_record(
    item_id: str,
    label: str,
    note: str,
    analysis_items: dict[str, dict[str, Any]],
    run_evidence: dict[str, Any],
) -> dict[str, Any]:
    item = analysis_items.get(item_id)
    if item is None:
        raise EvaluationInputError("feedback item ID is absent from the analysis")
    if len(note) > MAX_NOTE_CHARS:
        raise EvaluationInputError("feedback note exceeds 1000 characters")
    try:
        note.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvaluationInputError("feedback note contains invalid Unicode") from exc
    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "kind": "xianyu-ai-analysis-feedback",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "item_id": item_id,
        "label": label,
        "note": note,
        "observed": item,
        "run_evidence": run_evidence,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Evaluate AI analysis offline or capture one feedback record"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    golden = commands.add_parser("golden", help="compare analysis with a golden set")
    golden.add_argument("--analysis", type=_absolute_path, required=True)
    golden.add_argument("--golden", type=_absolute_path, required=True)
    feedback = commands.add_parser("feedback", help="write one private feedback record")
    feedback.add_argument("--analysis", type=_absolute_path, required=True)
    feedback.add_argument("--output", type=_absolute_path, required=True)
    feedback.add_argument("--item-id", required=True)
    feedback.add_argument("--label", choices=tuple(sorted(LABELS)), required=True)
    feedback.add_argument("--note", default="")
    return parser


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        analysis_items, run_evidence = _validated_analysis(
            _read_json(args.analysis, label="analysis")
        )
        if args.command == "golden":
            cases = _validated_golden(_read_json(args.golden, label="golden"))
            evaluation = evaluate_golden(analysis_items, cases)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "run_evidence": run_evidence,
                        "evaluation": evaluation,
                    },
                    ensure_ascii=True,
                    indent=2,
                    allow_nan=False,
                )
            )
            return 0 if evaluation["passed"] else 1

        record = _feedback_record(
            args.item_id,
            args.label,
            args.note,
            analysis_items,
            run_evidence,
        )
        _write_private_new_json(args.output, record)
        print(
            json.dumps(
                {
                    "ok": True,
                    "feedback": {
                        "status": "recorded",
                        "schema_version": FEEDBACK_SCHEMA_VERSION,
                        "item_id": args.item_id,
                        "label": args.label,
                        "private_mode": "0600-where-supported",
                    },
                },
                ensure_ascii=True,
                indent=2,
                allow_nan=False,
            )
        )
        return 0  # noqa: TRY300 - success stays inside cancellation boundary.
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "evaluation cancelled",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (EvaluationInputError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
