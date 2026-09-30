from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import view_results

ROOT = Path(__file__).resolve().parents[1]


def listings() -> dict:
    return {
        "ok": True,
        "keyword": "相机",
        "items": [
            {"id": "1", "title": "相机机身", "price": 3000, "location": "上海"},
            {"id": "2", "title": "相机保护壳", "price": 20},
            {"id": "3", "title": "相机套装", "price": 4000},
            {"id": "4", "title": "相机（待报价）", "price": None},
        ],
    }


@pytest.mark.parametrize(
    ("sort", "expected"),
    [("price-asc", ["1", "3", "4"]), ("price-desc", ["3", "1", "4"])],
)
def test_price_order_keeps_unknown_prices_last(sort: str, expected: list[str]) -> None:
    source = listings()
    before = json.dumps(source)
    report = view_results.build_report(source, sort=sort, exclude=["保护壳"])

    assert [item["id"] for item in report["items"]] == expected
    assert report["received_count"] == 4
    assert report["excluded_count"] == 1
    assert report["matched_count"] == report["shown_count"] == 3
    assert json.dumps(source) == before


def test_limit_applies_after_sort_and_exclusion() -> None:
    report = view_results.build_report(
        listings(), limit=1, sort="price-asc", exclude=["保护壳"]
    )
    assert report["shown_count"] == 1
    assert report["matched_count"] == 3
    assert report["items"][0]["id"] == "1"


def test_display_escapes_untrusted_text_and_rebuilds_links() -> None:
    source = listings()
    source["state_file"] = "/private/do-not-display"
    source["cookies"] = "do-not-display"
    source["items"] = [
        {
            "id": "abc) injected",
            "title": "[标题](javascript:bad) <script>\x1b[2J\n下一行\u202e",
            "url": "https://evil.invalid/do-not-display",
            "seller": "do-not-display",
        }
    ]
    report = view_results.build_report(source)
    rendered = view_results.render_report(report, "markdown")

    assert "do-not-display" not in json.dumps(report) + rendered
    assert "\x1b" not in rendered and "\u202e" not in rendered
    assert "<script>" not in rendered
    assert "\\[标题\\]" in rendered
    assert (
        report["items"][0]["url"] == "https://www.goofish.com/item?id=abc%29%20injected"
    )


def test_failed_monitor_keeps_retained_items_and_failure_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = {
        "ok": False,
        "tasks": [
            {
                "ok": False,
                "items": listings()["items"],
                "persistence": {"status": "recorded"},
            }
        ],
    }
    file = tmp_path / "result.json"
    file.write_text(json.dumps(source), encoding="utf-8")

    assert view_results.main(["--input", str(file)]) == 2
    output = capsys.readouterr().out
    assert "未完成" in output
    assert "相机机身" in output
    assert "没有符合" not in output


def test_analysis_preserves_failed_source_status() -> None:
    source = listings()
    source.update(run_evidence={}, source_run={"status": "failed"})
    report = view_results.build_report(source)
    assert report["ok"] is False
    assert report["source_kind"] == "analysis"


def test_demo_envelope_cannot_hide_a_failure() -> None:
    source = {"ok": False, "demo": {"status": "synthetic"}, "search": listings()}
    assert view_results.build_report(source)["ok"] is False


def test_analysis_view_includes_specific_reasons_and_missing_information() -> None:
    source = listings()
    source["run_evidence"] = {}
    source["items"][0].update(
        score=90, observed_evidence=["上海可自提"], uncertainties=["未说明快门次数"]
    )
    text = view_results.render_report(view_results.build_report(source, limit=1))
    assert "上海可自提" in text
    assert "未说明快门次数" in text
    assert "90/100" in text


@pytest.mark.parametrize(
    "source",
    [
        None,
        [],
        {"ok": 1},
        {"ok": True},
        {"ok": True, "items": [None]},
        {"ok": True, "items": [{"title": "no id"}]},
    ],
)
def test_malformed_inputs_are_rejected(source: object) -> None:
    with pytest.raises(ValueError):
        view_results.build_report(source)


def test_filter_uses_full_title_before_display_truncation() -> None:
    source = {"ok": True, "items": [{"id": "1", "title": "长" * 500 + "配件"}]}
    assert view_results.build_report(source, exclude=["配件"])["shown_count"] == 0


def test_cli_reads_utf8_stdin_from_another_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts/xianyu.py"),
            "view",
            "--sort",
            "price-asc",
            "--limit",
            "1",
        ],
        input=json.dumps(listings()),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert "相机保护壳" in result.stdout
    assert "相机机身" not in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_input_size_and_invalid_json_fail_without_echoing_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "input.json"
    file.write_bytes(b"private-data-not-json")
    assert view_results.main(["--input", str(file), "--format", "json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert "private-data" not in report["error"]
    monkeypatch.setattr(view_results, "MAX_INPUT_BYTES", 4)
    with pytest.raises(ValueError, match="2 MiB"):
        view_results.read_input(str(file))
