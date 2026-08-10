from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import cdp_profile
import pytest
from spider import (
    CDP_PROFILE_SENTINEL_NAME,
    CDP_PROFILE_SENTINEL_VALUE,
    _private_cdp_profile_path,
)

SYMLINK_SAFE_RMTREE = bool(
    getattr(cdp_profile.shutil.rmtree, "avoids_symlink_attacks", False)
)


def _initialize_legacy_profile(profile: Path) -> Path:
    profile.mkdir(mode=0o700, exist_ok=True)
    profile.chmod(0o700)
    sentinel = profile / CDP_PROFILE_SENTINEL_NAME
    sentinel.write_text(CDP_PROFILE_SENTINEL_VALUE, encoding="utf-8", newline="\n")
    sentinel.chmod(0o600)
    return profile


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS temp-root alias")
def test_legacy_cleanup_accepts_standard_macos_temp_root_alias() -> None:
    profile = Path(tempfile.mkdtemp(prefix="xianyu-cdp-alias-test."))
    profile.chmod(0o700)
    if profile.absolute() == profile.resolve():
        profile.rmdir()
        pytest.skip("the configured macOS temp root has no lexical alias")

    try:
        _initialize_legacy_profile(profile)

        assert _private_cdp_profile_path(str(profile)).samefile(profile)
    finally:
        sentinel = profile / CDP_PROFILE_SENTINEL_NAME
        if sentinel.is_file():
            sentinel.unlink()
        if profile.is_dir():
            profile.rmdir()


def test_cdp_profile_main_rejects_legacy_initialization_without_echoing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "private-profile-name"
    profile.mkdir(mode=0o700)

    with pytest.raises(SystemExit) as captured:
        cdp_profile.main(["--directory", str(profile)])
    output = capsys.readouterr().out

    assert captured.value.code == 2
    assert "private-profile-name" not in output
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["error_type"] == "ArgumentError"
    assert "--cleanup" in payload["error"]
    assert list(profile.iterdir()) == []


@pytest.mark.skipif(not SYMLINK_SAFE_RMTREE, reason="guarded cleanup unavailable")
def test_cleanup_removes_only_initialized_stopped_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _initialize_legacy_profile(profile)
    (profile / "synthetic-data").write_text("safe fixture", encoding="utf-8")

    cdp_profile.cleanup_cdp_profile(str(profile))

    assert not profile.exists()


@pytest.mark.skipif(not SYMLINK_SAFE_RMTREE, reason="guarded cleanup unavailable")
def test_cleanup_refuses_profile_with_chrome_activity_indicator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    _initialize_legacy_profile(profile)
    (profile / "SingletonLock").write_text("synthetic", encoding="utf-8")

    def unexpected_rename(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an active profile must not be renamed")

    monkeypatch.setattr(Path, "rename", unexpected_rename)

    with pytest.raises(ValueError, match="appears active"):
        cdp_profile.cleanup_cdp_profile(str(profile))

    assert profile.is_dir()


@pytest.mark.skipif(not SYMLINK_SAFE_RMTREE, reason="guarded cleanup unavailable")
def test_cleanup_restores_profile_when_rename_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "profile"
    _initialize_legacy_profile(profile)
    original_rename = Path.rename
    interrupted = False

    def interrupt_first_rename(path: Path, target: Path) -> Path:
        nonlocal interrupted
        result = original_rename(path, target)
        if path == profile and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "rename", interrupt_first_rename)

    assert cdp_profile.main(["--directory", str(profile), "--cleanup"]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["profile"]["status"] == "not-established"
    assert payload["cleanup"]["status"] == "failed"
    assert profile.is_dir()
    assert not any(".xianyu-remove-" in child.name for child in tmp_path.iterdir())


def test_stopped_check_refuses_listening_debug_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    _initialize_legacy_profile(profile)
    (profile / "DevToolsActivePort").write_text(
        "54321\n/devtools/browser/synthetic\n",
        encoding="utf-8",
    )
    closed = False

    class Connection:
        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        cdp_profile.socket,
        "create_connection",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(ValueError, match="still running"):
        cdp_profile._require_profile_stopped(profile)

    assert closed


def test_stopped_check_accepts_refused_stale_debug_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    _initialize_legacy_profile(profile)
    (profile / "DevToolsActivePort").write_text(
        "54321\n/devtools/browser/synthetic\n",
        encoding="utf-8",
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(cdp_profile.socket, "create_connection", refuse)

    cdp_profile._require_profile_stopped(profile)


@pytest.mark.skipif(not SYMLINK_SAFE_RMTREE, reason="guarded cleanup unavailable")
def test_partial_cleanup_reports_unknown_and_failed_without_echoing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "private-profile-name"
    _initialize_legacy_profile(profile)
    (profile / "synthetic-data").write_text("safe fixture", encoding="utf-8")

    def fail_after_partial_remove(path: Path) -> None:
        (path / "synthetic-data").unlink()
        raise OSError("synthetic partial cleanup failure")

    setattr(fail_after_partial_remove, "avoids_symlink_attacks", True)
    monkeypatch.setattr(cdp_profile.shutil, "rmtree", fail_after_partial_remove)

    assert cdp_profile.main(["--directory", str(profile), "--cleanup"]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert "private-profile-name" not in output
    assert payload["profile"]["status"] == "not-established"
    assert payload["cleanup"]["status"] == "failed"
    assert profile.is_dir()
