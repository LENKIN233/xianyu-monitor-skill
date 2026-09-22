from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import setup

ROOT = Path(__file__).resolve().parents[1]


def _args(state: Path, **overrides: Any) -> argparse.Namespace:
    values = {
        "state": state,
        "keyword": "iPhone 15 Pro",
        "capture_state": False,
        "browser_channel": None,
        "timeout": 1_800,
        "headed_capability_test": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _doctor(*, local_chrome: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "checks": [{"id": "python-version", "status": "passed"}],
        "next_action": {
            "code": "ready-use-browser-channel" if local_chrome else "ready",
            "hint": "ready",
        },
    }


def _valid_state() -> dict[str, Any]:
    return {
        "ok": True,
        "state": {"status": "candidate-valid"},
        "privacy": {"status": "passed"},
        "authentication": {"status": "not-established"},
        "identity": {"status": "not-evaluated"},
        "search_capability": {"status": "not-tested"},
        "cleanup": {"status": "complete-or-not-required"},
    }


def _search_success() -> setup.CommandResult:
    return setup.CommandResult(
        0,
        {
            "ok": True,
            "count": 1,
            "pages_scraped": 1,
            "items": [{"id": "123"}],
            "search_capability": {"status": "passed-for-this-run"},
            "authentication": {"status": "not-evaluated"},
            "identity": {"status": "not-evaluated"},
            "cleanup": {"status": "complete-or-not-required"},
        },
    )


def test_setup_missing_state_returns_safe_login_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "private/state.json"
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    called = False

    def runner(_arguments: list[str]) -> setup.CommandResult:
        nonlocal called
        called = True
        raise AssertionError("no child command should run")

    return_code, report = setup.run_setup(_args(private_state), runner=runner)

    assert return_code == 2
    assert called is False
    assert report["phases"]["login"]["status"] == "handoff-required"
    assert report["next_action"]["code"] == "rerun-setup-with-capture-state"
    assert str(private_state) not in json.dumps(report)


def test_setup_existing_state_runs_exact_bounded_capability_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "state.json"
    private_state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        setup.doctor,
        "run_doctor",
        lambda **_kwargs: _doctor(local_chrome=True),
    )
    monkeypatch.setattr(
        setup.state_check, "inspect_state", lambda _path: _valid_state()
    )
    observed: list[list[str]] = []

    def runner(arguments: list[str]) -> setup.CommandResult:
        observed.append(arguments)
        return _search_success()

    return_code, report = setup.run_setup(_args(private_state), runner=runner)

    assert return_code == 0
    assert report["ok"] is True
    assert report["phases"]["login"]["status"] == "skipped-existing-candidate"
    assert report["phases"]["capability"]["status"] == "passed-for-this-run"
    assert report["browser"]["selection"] == "chrome"
    assert len(observed) == 1
    command = observed[0]
    assert command[command.index("--pages") + 1] == "1"
    assert command[command.index("--retries") + 1] == "1"
    assert command[command.index("--state") + 1] == str(private_state)
    assert command[command.index("--browser-channel") + 1] == "chrome"
    assert "items" not in report["phases"]["capability"]
    assert str(private_state) not in json.dumps(report)


def test_setup_capture_requires_browser_confirmation_then_checks_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "private/state.json"
    private_state.parent.mkdir(mode=0o700)
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    inspected: list[Path] = []

    def inspect(path: Path) -> dict[str, Any]:
        inspected.append(path)
        return _valid_state()

    monkeypatch.setattr(setup.state_check, "inspect_state", inspect)
    commands: list[list[str]] = []

    def runner(arguments: list[str]) -> setup.CommandResult:
        commands.append(arguments)
        if "login_state.py" in arguments[1]:
            private_state.write_text("{}", encoding="utf-8")
            return setup.CommandResult(
                0,
                {
                    "ok": True,
                    "state": {"status": "candidate-saved"},
                    "confirmation": {
                        "status": "interactive-token-received",
                        "channel": "browser",
                    },
                    "cleanup": {"status": "complete-or-not-required"},
                },
            )
        return _search_success()

    return_code, report = setup.run_setup(
        _args(private_state, capture_state=True),
        runner=runner,
    )

    assert return_code == 0
    assert report["phases"]["login"]["status"] == "candidate-saved"
    assert inspected == [private_state]
    assert len(commands) == 2
    login_command = commands[0]
    assert "--confirm-in-browser" in login_command
    assert "--force" not in login_command
    assert login_command[login_command.index("--output") + 1] == str(private_state)


def test_setup_doctor_failure_stops_before_state_or_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "state.json"
    private_state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        setup.doctor,
        "run_doctor",
        lambda **_kwargs: {
            "ok": False,
            "checks": [],
            "next_action": {"code": "install-dependencies", "hint": "install"},
        },
    )
    monkeypatch.setattr(
        setup.state_check,
        "inspect_state",
        lambda _path: (_ for _ in ()).throw(AssertionError("state should not run")),
    )

    return_code, report = setup.run_setup(
        _args(private_state),
        runner=lambda _arguments: (_ for _ in ()).throw(
            AssertionError("child should not run")
        ),
    )

    assert return_code == 2
    assert report["phases"]["doctor"]["status"] == "failed"
    assert report["phases"]["state"]["status"] == "not-run"


def test_setup_state_failure_retains_login_evidence_and_stops_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "private/state.json"
    private_state.parent.mkdir(mode=0o700)
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    monkeypatch.setattr(
        setup.state_check,
        "inspect_state",
        lambda _path: {
            "ok": False,
            "state": {"status": "invalid"},
            "privacy": {"status": "passed"},
            "next_action": {"code": "capture-or-import-state", "hint": "recapture"},
        },
    )
    calls = 0

    def runner(_arguments: list[str]) -> setup.CommandResult:
        nonlocal calls
        calls += 1
        private_state.write_text("{}", encoding="utf-8")
        return setup.CommandResult(
            0,
            {
                "ok": True,
                "state": {"status": "candidate-saved"},
                "cleanup": {"status": "complete-or-not-required"},
            },
        )

    return_code, report = setup.run_setup(
        _args(private_state, capture_state=True),
        runner=runner,
    )

    assert return_code == 2
    assert calls == 1
    assert report["phases"]["login"]["status"] == "candidate-saved"
    assert report["phases"]["state"]["status"] == "invalid"


def test_setup_failed_capability_keeps_valid_candidate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "state.json"
    private_state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    monkeypatch.setattr(
        setup.state_check, "inspect_state", lambda _path: _valid_state()
    )

    return_code, report = setup.run_setup(
        _args(private_state),
        runner=lambda _arguments: setup.CommandResult(
            2,
            {
                "ok": False,
                "error": "private path must not be forwarded",
                "search_capability": {"status": "failed-for-this-run"},
                "cleanup": {"status": "complete-or-not-required"},
            },
        ),
    )

    assert return_code == 2
    assert report["phases"]["state"]["status"] == "candidate-valid"
    assert report["phases"]["capability"]["status"] == "failed"
    assert "error" not in report["phases"]["capability"]


def test_setup_parser_does_not_echo_private_invalid_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_value = "relative/private/state.json"

    with pytest.raises(SystemExit) as raised:
        setup.build_parser().parse_args(
            ["--state", private_value, "--keyword", "phone"]
        )

    assert raised.value.code == 2
    output = capsys.readouterr().out
    assert private_value not in output
    assert json.loads(output)["error_type"] == "ArgumentError"


def test_setup_help_runs_from_foreign_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/setup.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--capture-state" in result.stdout
    assert "--headed-capability-test" in result.stdout


def test_run_child_retains_final_json_after_parent_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 130

        def __init__(self) -> None:
            self.communications = 0
            self.terminated = False

        def communicate(self, timeout: int | None = None) -> tuple[bytes, None]:
            self.communications += 1
            if self.communications == 1:
                raise KeyboardInterrupt
            assert timeout == 15
            return (
                b'{"ok":false,"cleanup":{"status":"failed"}}',
                None,
            )

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("graceful termination should be enough")

    process = FakeProcess()
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(setup.SetupChildCancelled) as raised:
        setup._run_child([sys.executable, "child.py"])

    assert process.terminated is True
    assert raised.value.result == setup.CommandResult(
        130,
        {"ok": False, "cleanup": {"status": "failed"}},
    )


def test_setup_login_parent_cancellation_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "private/state.json"
    private_state.parent.mkdir(mode=0o700)
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    retained = setup.CommandResult(
        130,
        {
            "ok": False,
            "state": {"status": "not-established"},
            "cleanup": {"status": "failed"},
        },
    )

    with pytest.raises(setup.SetupChildCancelled):
        setup.run_setup(
            _args(private_state, capture_state=True),
            runner=lambda _arguments: (_ for _ in ()).throw(
                setup.SetupChildCancelled(retained)
            ),
            progress=(progress := setup.SetupProgress()),
        )

    assert progress.report["phases"]["login"]["status"] == "not-established"
    assert progress.report["cleanup"]["status"] == "not-established"


def test_setup_search_parent_cancellation_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state = tmp_path / "state.json"
    private_state.write_text("{}")
    monkeypatch.setattr(setup.doctor, "run_doctor", lambda **_kwargs: _doctor())
    monkeypatch.setattr(
        setup.state_check,
        "inspect_state",
        lambda _path: _valid_state(),
    )
    retained = setup.CommandResult(
        130,
        {
            "ok": False,
            "search_capability": {"status": "not-established"},
            "cleanup": {"status": "failed"},
        },
    )

    with pytest.raises(setup.SetupChildCancelled):
        setup.run_setup(
            _args(private_state),
            runner=lambda _arguments: (_ for _ in ()).throw(
                setup.SetupChildCancelled(retained)
            ),
            progress=(progress := setup.SetupProgress()),
        )

    assert progress.report["phases"]["capability"]["status"] == "not-established"
    assert progress.report["cleanup"]["status"] == "not-established"
