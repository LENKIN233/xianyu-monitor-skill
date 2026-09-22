from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import state_check


def _write_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "candidate",
                        "domain": ".goofish.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


def test_state_check_accepts_private_candidate_without_echoing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "private-state.json"
    _write_state(state_file)
    original_check = state_check._require_unchanged

    def check_with_metadata_diff(
        before: os.stat_result, after: os.stat_result, *, cross_api: bool = False
    ) -> None:
        try:
            original_check(before, after, cross_api=cross_api)
        except state_check.StateChangedError:
            fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_mode",
            )
            changed = {
                field: (getattr(before, field), getattr(after, field))
                for field in fields
                if getattr(before, field) != getattr(after, field)
            }
            pytest.fail(
                f"unchanged synthetic candidate has metadata differences: {changed}"
            )

    monkeypatch.setattr(state_check, "_require_unchanged", check_with_metadata_diff)

    assert state_check.main(["--state", str(state_file)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)

    assert str(state_file) not in output
    assert report["ok"] is True
    assert report["state"] == {"status": "candidate-valid"}
    assert report["search_capability"] == {"status": "not-tested"}
    assert report["next_action"]["code"] == "run-controlled-search"


def _windows_metadata(**changes: Any) -> Any:
    values = {
        "st_dev": 1,
        "st_ino": 2,
        "st_mode": 0o100600,
        "st_uid": 0,
        "st_size": 100,
        "st_mtime_ns": 300,
        "st_ctime_ns": 100,
        "st_birthtime_ns": 100,
    }
    return SimpleNamespace(**(values | changes))


def test_windows_stat_and_fstat_may_have_distinct_ctime_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state_check, "_WINDOWS_STAT", True)
    path_snapshot = _windows_metadata()
    opened_snapshot = _windows_metadata(st_ctime_ns=200)

    state_check._require_unchanged(opened_snapshot, path_snapshot, cross_api=True)

    # A real metadata change within the same API must still be rejected.
    with pytest.raises(state_check.StateChangedError):
        state_check._require_unchanged(opened_snapshot, path_snapshot)


@pytest.mark.parametrize(
    "field",
    [
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_birthtime_ns",
        "st_uid",
        "st_mode",
    ],
)
def test_windows_cross_api_checks_still_reject_changed_metadata(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(state_check, "_WINDOWS_STAT", True)
    opened = _windows_metadata(st_ctime_ns=200)
    path_snapshot = _windows_metadata(**{field: getattr(opened, field) + 1})

    with pytest.raises(state_check.StateChangedError):
        state_check._require_unchanged(opened, path_snapshot, cross_api=True)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges")
def test_state_check_accepts_stable_symlink_to_private_candidate(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "private-state.json"
    state_link = tmp_path / "state-link.json"
    _write_state(state_file)
    state_link.symlink_to(state_file)

    report = state_check.inspect_state(state_link)

    assert report["ok"] is True
    assert report["privacy"]["status"] == "passed"
    assert report["state"] == {"status": "candidate-valid"}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges")
def test_state_check_rejects_symlink_target_rotation_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    state_link = tmp_path / "state-link.json"
    _write_state(first)
    _write_state(second)
    second.chmod(0o644)
    state_link.symlink_to(first)
    original_validator = state_check._validate_state_payload

    def rotate_after_validation(payload: bytes) -> None:
        original_validator(payload)
        state_link.unlink()
        state_link.symlink_to(second)

    monkeypatch.setattr(
        state_check,
        "_validate_state_payload",
        rotate_after_validation,
    )

    report = state_check.inspect_state(state_link)

    assert report["ok"] is False
    assert report["error_type"] == "StateChangedError"
    assert report["next_action"]["code"] == "retry-state-check"


@pytest.mark.skipif(os.name == "nt", reason="symlink and POSIX mode coverage")
def test_state_check_rechecks_stable_symlink_target_mode_at_return_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "private-state.json"
    state_link = tmp_path / "state-link.json"
    _write_state(state_file)
    state_link.symlink_to(state_file)
    original_inspector = state_check._inspect_open_state

    def expose_after_inspection(
        stream: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        result = original_inspector(stream)  # type: ignore[arg-type]
        state_file.chmod(0o644)
        return result

    monkeypatch.setattr(state_check, "_inspect_open_state", expose_after_inspection)

    report = state_check.inspect_state(state_link)

    assert report["ok"] is False
    assert report["privacy"]["status"] == "failed"
    assert report["error_type"] == "StatePrivacyError"


@pytest.mark.skipif(os.name == "nt", reason="symlink and POSIX metadata coverage")
def test_state_check_rechecks_stable_symlink_target_content_at_return_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "private-state.json"
    state_link = tmp_path / "state-link.json"
    _write_state(state_file)
    state_link.symlink_to(state_file)
    original_inspector = state_check._inspect_open_state

    def mutate_after_inspection(
        stream: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        result = original_inspector(stream)  # type: ignore[arg-type]
        current = state_file.read_text(encoding="utf-8")
        state_file.write_text(
            current.replace("candidate", "substitute"), encoding="utf-8"
        )
        state_file.chmod(0o600)
        return result

    monkeypatch.setattr(state_check, "_inspect_open_state", mutate_after_inspection)

    report = state_check.inspect_state(state_link)

    assert report["ok"] is False
    assert report["error_type"] == "StateChangedError"
    assert report["next_action"]["code"] == "retry-state-check"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode coverage")
def test_state_check_rechecks_mode_on_same_open_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "private-state.json"
    _write_state(state_file)
    original_validator = state_check._validate_state_payload

    def expose_after_validation(payload: bytes) -> None:
        original_validator(payload)
        state_file.chmod(0o640)

    monkeypatch.setattr(state_check, "_validate_state_payload", expose_after_validation)

    report = state_check.inspect_state(state_file)

    assert report["ok"] is False
    assert report["privacy"]["status"] == "failed"
    assert report["error_type"] == "StatePrivacyError"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ctime coverage")
def test_state_check_rejects_ctime_change_even_when_private_mode_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "private-state.json"
    _write_state(state_file)
    original_validator = state_check._validate_state_payload

    def change_and_restore_mode(payload: bytes) -> None:
        original_validator(payload)
        state_file.chmod(0o640)
        state_file.chmod(0o600)

    monkeypatch.setattr(
        state_check,
        "_validate_state_payload",
        change_and_restore_mode,
    )

    report = state_check.inspect_state(state_file)

    assert report["ok"] is False
    assert report["state"] == {"status": "not-established"}
    assert report["error_type"] == "StateChangedError"
    assert report["next_action"]["code"] == "retry-state-check"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode coverage")
def test_state_check_rejects_group_readable_candidate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_file = tmp_path / "private-state.json"
    _write_state(state_file)
    state_file.chmod(0o640)

    assert state_check.main(["--state", str(state_file)]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)

    assert str(state_file) not in output
    assert report["state"] == {"status": "not-inspected"}
    assert report["privacy"]["status"] == "failed"
    assert report["next_action"]["code"] == "fix-state-permissions"


def test_state_check_rejects_malformed_candidate_without_echoing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_file = tmp_path / "private-state.json"
    state_file.write_text("{}\n", encoding="utf-8")
    if os.name != "nt":
        state_file.chmod(0o600)

    assert state_check.main(["--state", str(state_file)]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)

    assert str(state_file) not in output
    assert report["state"] == {"status": "invalid"}
    assert report["error_type"] == "StateFileError"
    assert report["next_action"]["code"] == "capture-or-import-state"


def test_state_check_rejects_deeply_nested_json_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_file = tmp_path / "deep-state.json"
    state_file.write_text("[" * 100_000 + "0" + "]" * 100_000, encoding="utf-8")
    if os.name != "nt":
        state_file.chmod(0o600)

    assert state_check.main(["--state", str(state_file)]) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["error_type"] == "StateFileError"
    assert report["state"] == {"status": "invalid"}


def test_state_check_missing_candidate_does_not_echo_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_file = tmp_path / "missing-private-state.json"

    assert state_check.main(["--state", str(state_file)]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)

    assert str(state_file) not in output
    assert report["ok"] is False
    assert report["state"] == {"status": "not-established"}
    assert report["error_type"] == "StateAccessError"
    assert report["next_action"]["code"] == "capture-or-import-state"


def test_state_parser_requires_absolute_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        state_check.main(["--state", "relative.json"])

    report = json.loads(capsys.readouterr().out)
    assert report["error_type"] == "ArgumentError"
    assert "absolute" in report["error"]
