from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import xianyu

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_contains_no_raw_cdp_attachment() -> None:
    runtime = "\n".join(
        (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in ("spider.py", "login_state.py", "monitor.py")
    )

    assert "connect_over_cdp" not in runtime


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
    assert "--cdp-user-data-dir" not in result.stdout
    assert "--confirm-in-browser" in result.stdout


def test_unified_cli_help_from_foreign_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/xianyu.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    for command in ("doctor", "login", "search", "task", "monitor", "install"):
        assert command in result.stdout


def test_unified_module_entrypoint_from_skill_root() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scripts.xianyu", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "Unified CLI" in result.stdout


def test_unified_cli_delegates_command_help_from_foreign_working_directory(
    tmp_path: Path,
) -> None:
    expected_options = {
        "doctor": "--state-output-dir",
        "login": "--confirm-in-browser",
        "search": "--keyword",
        "task": "--data-file",
        "monitor": "--tasks-file",
        "install": "--host",
    }

    for command, expected_option in expected_options.items():
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(ROOT / "scripts/xianyu.py"),
                command,
                "--help",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, command
        assert expected_option in result.stdout, command
        assert result.stdout.splitlines()[0].startswith(
            f"usage: xianyu.py {command}"
        ), command


def test_unified_cli_forwards_task_arguments(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.json"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts/xianyu.py"),
            "task",
            "--data-file",
            str(task_file),
            "create",
            "测试",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["result"]["keyword"] == "测试"
    assert task_file.is_file()


def test_unified_cli_unknown_command_is_structured_json(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/xianyu.py"), "unknown"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error_type"] == "ArgumentError"
    assert "unknown" in payload["error"]


def test_unified_cli_does_not_echo_unknown_command(tmp_path: Path) -> None:
    private_value = "state-path-should-stay-private"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/xianyu.py"), private_value],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert private_value not in result.stdout


def test_unified_cli_forwards_exact_argv_and_return_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_argv0 = sys.argv[0]
    observed: dict[str, object] = {}

    def load_entrypoint(module_name: str) -> object:
        def entrypoint(arguments: list[str] | None) -> int:
            observed.update(
                module_name=module_name,
                arguments=arguments,
                argv0=sys.argv[0],
            )
            return 17

        return entrypoint

    monkeypatch.setattr(xianyu, "_load_entrypoint", load_entrypoint)

    assert xianyu.main(["search", "--keyword", "测试", "--location", ""]) == 17
    assert observed == {
        "module_name": "spider",
        "arguments": ["--keyword", "测试", "--location", ""],
        "argv0": f"{original_argv0} search",
    }
    assert sys.argv[0] == original_argv0
    assert capsys.readouterr().out == ""


def test_unified_cli_preserves_streams_tty_cwd_and_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_stdin = sys.stdin
    original_cwd = Path.cwd()
    observed: dict[str, object] = {}

    def load_entrypoint(_module_name: str) -> object:
        def entrypoint(_arguments: list[str] | None) -> int:
            observed.update(
                stdin_same=sys.stdin is original_stdin,
                stdin_isatty=sys.stdin.isatty(),
                cwd=Path.cwd(),
            )
            print("商品 stdout")
            print("诊断 stderr", file=sys.stderr)
            return 0

        return entrypoint

    monkeypatch.setattr(xianyu, "_load_entrypoint", load_entrypoint)

    assert xianyu.main(["search"]) == 0
    assert observed == {
        "stdin_same": True,
        "stdin_isatty": original_stdin.isatty(),
        "cwd": original_cwd,
    }
    captured = capsys.readouterr()
    assert captured.out == "商品 stdout\n"
    assert captured.err == "诊断 stderr\n"


def test_unified_cli_restores_argv0_after_delegated_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_argv0 = sys.argv[0]

    def load_entrypoint(_module_name: str) -> object:
        def entrypoint(_arguments: list[str] | None) -> int:
            raise SystemExit(2)

        return entrypoint

    monkeypatch.setattr(xianyu, "_load_entrypoint", load_entrypoint)

    with pytest.raises(SystemExit, match="2"):
        xianyu.main(["search"])
    assert sys.argv[0] == original_argv0


def test_unified_doctor_leaves_isolated_runtime_unchanged(tmp_path: Path) -> None:
    isolated = tmp_path / "scripts"
    isolated.mkdir()
    for name in ("xianyu.py", "doctor.py", "cli_contract.py"):
        shutil.copy2(ROOT / "scripts" / name, isolated / name)
    before = sorted(path.relative_to(isolated) for path in isolated.rglob("*"))

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(isolated / "xianyu.py"), "doctor"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode in {0, 2}
    assert set(json.loads(result.stdout)) == {"ok", "checks", "next_action"}
    after = sorted(path.relative_to(isolated) for path in isolated.rglob("*"))
    assert after == before


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM") or not hasattr(signal, "raise_signal"),
    reason="catchable SIGTERM unavailable",
)
def test_unified_cli_controls_sigterm_during_command_loading(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def terminate_while_loading(
        _module_name: str,
    ) -> object:
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt command loading")

    monkeypatch.setattr(xianyu, "_load_entrypoint", terminate_while_loading)

    assert xianyu.main(["doctor"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "SigtermCancellation"


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM") or not hasattr(signal, "raise_signal"),
    reason="catchable SIGTERM unavailable",
)
def test_unified_cli_nested_sigterm_emits_one_result_and_restores_argv0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_argv0 = sys.argv[0]

    @xianyu.sigterm_cancellable
    def delegated_entrypoint(_arguments: list[str] | None) -> int:
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt delegated command")

    monkeypatch.setattr(
        xianyu,
        "_load_entrypoint",
        lambda _module_name: delegated_entrypoint,
    )

    assert xianyu.main(["monitor"]) == 130
    output_lines = capsys.readouterr().out.splitlines()
    assert len(output_lines) == 1
    assert json.loads(output_lines[0])["error_type"] == "SigtermCancellation"
    assert sys.argv[0] == original_argv0


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
