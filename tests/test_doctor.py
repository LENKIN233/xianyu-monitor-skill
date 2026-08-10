from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import doctor
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _check(payload: dict[str, object], check_id: str) -> dict[str, object]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    return next(check for check in checks if check["id"] == check_id)


def _healthy_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_required_imports_check",
        lambda: (
            {
                "id": "required-imports",
                "status": "passed",
                "required": True,
                "details": {"missing": []},
            },
            Path("playwright"),
        ),
    )


def test_doctor_reports_healthy_bundled_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: True)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: False)

    payload = doctor.run_doctor(version=(3, 12, 4))

    assert payload["ok"] is True
    assert _check(payload, "python-version")["status"] == "passed"
    assert _check(payload, "playwright-chromium")["status"] == "available"
    assert _check(payload, "local-chrome")["status"] == "unavailable"
    assert _check(payload, "browser-runtime")["details"] == {
        "selection": "playwright-chromium"
    }
    assert _check(payload, "state-output-directory")["status"] == "not-requested"
    assert _check(payload, "tasks-directory")["status"] == "not-requested"
    assert payload["next_action"]["code"] == "ready"


def test_local_chrome_is_a_healthy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: False)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: True)

    payload = doctor.run_doctor(version=(3, 10, 0))

    assert payload["ok"] is True
    assert _check(payload, "browser-runtime")["details"] == {
        "selection": "local-chrome"
    }
    assert payload["next_action"]["code"] == "ready-use-browser-channel"


def test_missing_browser_runtime_is_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: False)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: False)

    payload = doctor.run_doctor(version=(3, 12, 0))

    assert payload["ok"] is False
    assert _check(payload, "browser-runtime")["status"] == "failed"
    assert payload["next_action"]["code"] == "install-browser"


def test_python_and_import_failures_have_stable_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor,
        "_required_imports_check",
        lambda: (
            {
                "id": "required-imports",
                "status": "failed",
                "required": True,
                "details": {"missing": ["playwright"]},
            },
            None,
        ),
    )
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: False)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: False)

    payload = doctor.run_doctor(version=(3, 9, 18))

    assert payload["ok"] is False
    assert payload["next_action"]["code"] == "upgrade-python"
    assert _check(payload, "required-imports")["details"] == {"missing": ["playwright"]}


def test_required_import_check_does_not_import_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def find_spec(name: str) -> SimpleNamespace | None:
        observed.append(name)
        if name == "playwright":
            return SimpleNamespace(submodule_search_locations=["package"])
        return None

    monkeypatch.setattr(doctor.importlib.util, "find_spec", find_spec)

    check, package = doctor._required_imports_check()

    assert observed == ["playwright", "tzdata"]
    assert check["status"] == "failed"
    assert check["details"] == {"missing": ["tzdata"]}
    assert package == Path("package")


def test_bundled_chromium_check_uses_declared_revision_without_launching(
    tmp_path: Path,
) -> None:
    package = tmp_path / "site-packages/playwright"
    metadata = package / "driver/package/browsers.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "1234"},
                    {"name": "firefox", "revision": "9999"},
                ]
            }
        ),
        encoding="utf-8",
    )
    browser_root = tmp_path / "browser-cache"
    executable = browser_root / "chromium-1234/chrome-linux/chrome"
    executable.parent.mkdir(parents=True)
    executable.write_text("browser", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    assert doctor._bundled_chromium_available(
        package,
        environment={"PLAYWRIGHT_BROWSERS_PATH": str(browser_root)},
        platform_name="linux",
        os_name="posix",
        home=tmp_path / "unused-home",
    )
    source = (ROOT / "scripts/doctor.py").read_text(encoding="utf-8")
    assert "sync_playwright" not in source
    assert "async_playwright" not in source
    assert ".launch(" not in source
    assert "subprocess" not in source
    for write_operation in (
        ".mkdir(",
        ".touch(",
        ".unlink(",
        ".write_bytes(",
        ".write_text(",
        "shutil.rmtree(",
    ):
        assert write_operation not in source


def test_directory_arguments_expand_user_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_home = tmp_path / "private-home"
    monkeypatch.setenv("HOME", str(private_home))
    monkeypatch.setenv("USERPROFILE", str(private_home))

    args = doctor.build_parser().parse_args(
        ["--state-output-dir", "~/state", "--tasks-dir", "~/tasks"]
    )

    assert args.state_output_dir == private_home / "state"
    assert args.tasks_dir == private_home / "tasks"


@pytest.mark.parametrize(
    ("platform_name", "environment", "expected_suffix"),
    [
        (
            "darwin",
            {},
            "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
        (
            "linux",
            {},
            "/opt/google/chrome/chrome",
        ),
        (
            "win32",
            {"LOCALAPPDATA": "local"},
            "local/Google/Chrome/Application/chrome.exe",
        ),
    ],
)
def test_common_chrome_candidates_are_cross_platform(
    platform_name: str,
    environment: dict[str, str],
    expected_suffix: str,
) -> None:
    candidates = doctor._local_chrome_candidates(
        environment=environment,
        platform_name=platform_name,
    )

    normalized = [candidate.as_posix() for candidate in candidates]
    assert any(candidate.endswith(expected_suffix) for candidate in normalized)


def test_chrome_channel_candidates_match_playwright_fixed_locations(
    tmp_path: Path,
) -> None:
    path_only = tmp_path / "bin/google-chrome"
    user_application = (
        tmp_path / "home/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )

    linux = doctor._local_chrome_candidates(
        environment={"PATH": str(path_only.parent)},
        platform_name="linux",
    )
    mac = doctor._local_chrome_candidates(
        environment={},
        platform_name="darwin",
    )

    assert linux == [Path("/opt/google/chrome/chrome")]
    assert path_only not in linux
    assert mac == [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    assert user_application not in mac


def test_windows_chrome_candidates_include_playwright_home_drive_fallback() -> None:
    candidates = doctor._local_chrome_candidates(
        environment={"HOMEDRIVE": "C:"},
        platform_name="win32",
    )

    assert Path("C:/Program Files/Google/Chrome/Application/chrome.exe") in candidates
    assert (
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")
        in candidates
    )
    assert all(
        PureWindowsPath(str(candidate)).is_absolute() for candidate in candidates
    )


def test_optional_private_directories_are_checked_without_path_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_root = tmp_path / "account-name-must-not-leak"
    state_dir = private_root / "state"
    tasks_dir = private_root / "tasks"
    state_dir.mkdir(parents=True, mode=0o700)
    tasks_dir.mkdir(mode=0o700)
    if os.name != "nt":
        state_dir.chmod(0o700)
        tasks_dir.chmod(0o700)
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: True)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: False)

    exit_code = doctor.main(
        [
            "--state-output-dir",
            str(state_dir),
            "--tasks-dir",
            str(tasks_dir),
        ]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 0
    assert output.err == ""
    assert str(private_root) not in output.out
    assert _check(payload, "state-output-directory")["status"] == "passed"
    assert _check(payload, "tasks-directory")["status"] == "passed"
    assert list(state_dir.iterdir()) == []
    assert list(tasks_dir.iterdir()) == []


def test_missing_directory_fails_without_echoing_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "secret-account-name" / "missing"
    _healthy_dependencies(monkeypatch)
    monkeypatch.setattr(doctor, "_bundled_chromium_available", lambda _path: True)
    monkeypatch.setattr(doctor, "_local_chrome_available", lambda: False)

    exit_code = doctor.main(["--state-output-dir", str(missing)])

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 2
    assert str(missing) not in output.out
    assert _check(payload, "state-output-directory")["details"] == {
        "issues": ["missing"]
    }
    assert payload["next_action"]["code"] == "fix-private-directories"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable")
def test_non_private_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    check = doctor._directory_check("state-output-directory", directory)

    assert check["status"] == "failed"
    assert check["details"] == {"issues": ["not-private"]}


def test_script_help_works_from_a_foreign_working_directory(
    tmp_path: Path,
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/doctor.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--state-output-dir" in result.stdout
    assert "--tasks-dir" in result.stdout
    assert "never included in JSON" in result.stdout
    assert result.stderr == ""


def test_doctor_execution_leaves_no_bytecode_or_other_files(
    tmp_path: Path,
) -> None:
    isolated = tmp_path / "isolated-doctor"
    isolated.mkdir()
    shutil.copy2(ROOT / "scripts/doctor.py", isolated / "doctor.py")
    shutil.copy2(ROOT / "scripts/cli_contract.py", isolated / "cli_contract.py")
    before = sorted(path.relative_to(isolated) for path in isolated.rglob("*"))
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(isolated / "doctor.py")],
        cwd=isolated,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    after = sorted(path.relative_to(isolated) for path in isolated.rglob("*"))
    assert result.returncode in {0, 2}
    assert json.loads(result.stdout)["ok"] in {True, False}
    assert result.stderr == ""
    assert after == before
    assert not (isolated / "__pycache__").exists()


def test_argument_errors_are_machine_readable(tmp_path: Path) -> None:
    private_value = str(tmp_path / "account-name-must-not-leak")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts/doctor.py"),
            "--unknown",
            private_value,
        ],
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
    assert private_value not in result.stdout
