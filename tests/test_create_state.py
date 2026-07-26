from __future__ import annotations

import argparse
import asyncio
import errno
import io
import json
import os
import select
import stat
import sys
import time
import warnings
from pathlib import Path

import create_state
import pytest
from create_state import create_storage_state, parse_cookie_string


def test_parse_cookie_preserves_equals_in_value() -> None:
    cookies = parse_cookie_string("cookie2=secret; token=a=b")
    assert [(cookie["name"], cookie["value"]) for cookie in cookies] == [
        ("cookie2", "secret"),
        ("token", "a=b"),
    ]


def test_create_state_uses_private_permissions(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    output = create_storage_state("cookie2=secret", str(private_dir / "state.json"))
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700


def test_create_state_does_not_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "state.json"
    create_storage_state("cookie2=first", str(output))
    with pytest.raises(FileExistsError):
        create_storage_state("cookie2=second", str(output))
    create_storage_state("cookie2=second", str(output), force=True)
    assert "second" in output.read_text(encoding="utf-8")


def test_create_state_preserves_file_created_during_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    original_dump = create_state.json.dump

    def create_racing_output(*args: object, **kwargs: object) -> None:
        original_dump(*args, **kwargs)
        output.write_text("race winner\n", encoding="utf-8")

    monkeypatch.setattr(create_state.json, "dump", create_racing_output)

    with pytest.raises(FileExistsError):
        create_storage_state("cookie2=must-not-win", str(output))

    assert output.read_text(encoding="utf-8") == "race winner\n"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


@pytest.mark.parametrize("force", [False, True])
def test_secure_write_reports_commit_when_interrupted_after_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    output = tmp_path / "state.json"
    if force:
        output.write_text('{"old": true}\n', encoding="utf-8")
        original_publish = create_state.os.replace

        def publish_then_interrupt(source: Path, target: Path) -> None:
            original_publish(source, target)
            raise KeyboardInterrupt

        monkeypatch.setattr(create_state.os, "replace", publish_then_interrupt)
    else:
        original_publish = create_state.os.link

        def publish_then_interrupt(source: Path, target: Path) -> None:
            original_publish(source, target)
            raise KeyboardInterrupt

        monkeypatch.setattr(create_state.os, "link", publish_then_interrupt)

    committed: list[Path] = []
    with pytest.raises(KeyboardInterrupt):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            force,
            on_commit=committed.append,
        )

    assert committed == [output]
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "cookies": [],
        "origins": [],
    }
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_secure_write_retries_idempotent_commit_callback_after_interruption(
    tmp_path: Path,
) -> None:
    output = tmp_path / "state.json"
    callback_calls = 0
    committed: list[Path] = []

    def interrupted_callback(path: Path) -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            raise KeyboardInterrupt
        committed.append(path)

    with pytest.raises(KeyboardInterrupt):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            False,
            on_commit=interrupted_callback,
        )

    assert callback_calls == 2
    assert committed == [output]
    assert output.is_file()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_secure_write_does_not_mistake_old_force_target_for_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    output.write_text('{"old": true}\n', encoding="utf-8")
    committed: list[Path] = []

    def discard_staged_file_then_interrupt(source: Path, _target: Path) -> None:
        Path(source).unlink()
        raise KeyboardInterrupt

    monkeypatch.setattr(
        create_state.os,
        "replace",
        discard_staged_file_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"new": True},
            True,
            on_commit=committed.append,
        )

    assert committed == []
    assert json.loads(output.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_secure_write_reports_unknown_state_when_commit_reconciliation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    original_link = create_state.os.link
    original_lstat = Path.lstat
    publish_attempted = False

    def publish_then_interrupt(source: Path, target: Path) -> None:
        nonlocal publish_attempted
        original_link(source, target)
        publish_attempted = True
        raise KeyboardInterrupt

    def fail_output_reconciliation(path: Path) -> os.stat_result:
        if publish_attempted and path == output:
            raise PermissionError("simulated lstat denial")
        return original_lstat(path)

    monkeypatch.setattr(create_state.os, "link", publish_then_interrupt)
    monkeypatch.setattr(Path, "lstat", fail_output_reconciliation)
    progress = create_state.CredentialWriteProgress()

    with pytest.raises(KeyboardInterrupt) as captured:
        create_state.create_storage_state(
            "cookie2=secret",
            str(output),
            progress=progress,
        )

    assert output.is_file()
    assert captured.value.credential_state_status == "not-established"
    assert progress.state_status == "not-established"
    assert progress.output is None
    assert progress.cleanup_failures == [
        "failed to determine credential publish status"
    ]
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_secure_write_closes_descriptor_and_removes_temp_after_fstat_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_descriptors: list[int] = []
    original_fstat = create_state.os.fstat

    def interrupt_fstat(descriptor: int) -> os.stat_result:
        captured_descriptors.append(descriptor)
        raise KeyboardInterrupt

    monkeypatch.setattr(create_state.os, "fstat", interrupt_fstat)

    with pytest.raises(KeyboardInterrupt):
        create_state._secure_write_json(  # noqa: SLF001
            str(tmp_path / "state.json"),
            {"cookies": [], "origins": []},
            False,
        )

    assert len(captured_descriptors) == 1
    with pytest.raises(OSError) as captured:
        original_fstat(captured_descriptors[0])
    assert captured.value.errno == errno.EBADF
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_secure_write_stages_privately_with_restrictive_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_modes: list[tuple[int, int]] = []
    original_link = create_state.os.link

    def inspect_staging_permissions(source: Path, target: Path) -> None:
        observed_modes.append(
            (
                stat.S_IMODE(Path(source).stat().st_mode),
                stat.S_IMODE(Path(source).parent.stat().st_mode),
            )
        )
        original_link(source, target)

    monkeypatch.setattr(create_state.os, "link", inspect_staging_permissions)

    output = create_state._secure_write_json(  # noqa: SLF001
        str(tmp_path / "state.json"),
        {"cookies": [], "origins": []},
        False,
    )

    assert len(observed_modes) == 1
    if os.name != "nt":
        assert observed_modes == [(0o600, 0o700)]
    assert output.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_credential_stage_directory_creation_interruption_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    original_mkdir = Path.mkdir

    def mkdir_then_interrupt(path: Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.name.startswith(f".{output.name}."):
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "mkdir", mkdir_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            False,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_credential_stage_open_interruption_leaves_no_file_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    original_open = Path.open

    def open_then_interrupt(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        stream = original_open(path, *args, **kwargs)
        if path.name == "payload" and path.parent.name.startswith(f".{output.name}."):
            if os.name == "nt":
                stream.close()
            raise asyncio.CancelledError
        return stream

    monkeypatch.setattr(Path, "open", open_then_interrupt)

    with pytest.raises(asyncio.CancelledError):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            False,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_action_cancellation_survives_ordinary_stream_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    output = tmp_path / "state.json"
    original_close = create_state._close_credential_stage_stream

    def close_then_fail(stage: create_state._CredentialStage) -> None:
        original_close(stage)
        raise OSError("simulated stream close failure")

    def cancel_dump(*_args: object, **_kwargs: object) -> None:
        raise interruption()

    monkeypatch.setattr(create_state, "_read_cookie_input", lambda _args: "cookie2=x")
    monkeypatch.setattr(
        create_state,
        "_close_credential_stage_stream",
        close_then_fail,
    )
    monkeypatch.setattr(create_state.json, "dump", cancel_dump)

    assert create_state.main(["--cookie-stdin", "--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_type"] == error_type
    assert payload["state"]["status"] == "not-saved"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the private credential staging stream"],
    }
    assert not output.exists()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_normal_staging_cleanup_preserves_concurrent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    replacement: Path | None = None
    original_link = create_state.os.link

    def publish_then_replace_staging(source: Path, target: Path) -> None:
        nonlocal replacement
        original_link(source, target)
        replacement = Path(source)
        replacement.unlink()
        replacement.write_text("foreign replacement\n", encoding="utf-8")

    monkeypatch.setattr(create_state.os, "link", publish_then_replace_staging)

    with pytest.raises(OSError) as caught:
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            False,
        )

    assert replacement is not None
    assert replacement.read_text(encoding="utf-8") == "foreign replacement\n"
    assert caught.value.credential_state_status == "candidate-saved"
    assert getattr(caught.value, "cleanup_failures", []) == [
        "failed to remove the private credential staging directory"
    ]
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "cookies": [],
        "origins": [],
    }


@pytest.mark.skipif(os.name == "nt", reason="replacing an open file is POSIX-only")
def test_cancelled_staging_cleanup_preserves_concurrent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    temporary: Path | None = None
    original_new_stage = create_state._new_credential_stage

    def capture_stage(output_path: Path) -> create_state._CredentialStage:
        nonlocal temporary
        stage = original_new_stage(output_path)
        temporary = stage.path
        return stage

    def replace_staging_then_cancel(*_args: object, **_kwargs: object) -> None:
        assert temporary is not None
        temporary.unlink()
        temporary.write_text("foreign replacement\n", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(create_state, "_new_credential_stage", capture_stage)
    monkeypatch.setattr(create_state.json, "dump", replace_staging_then_cancel)

    with pytest.raises(KeyboardInterrupt) as caught:
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            False,
        )

    assert temporary is not None
    assert temporary.read_text(encoding="utf-8") == "foreign replacement\n"
    assert getattr(caught.value, "cleanup_failures", []) == [
        "failed to remove the private credential staging directory"
    ]
    assert not output.exists()


@pytest.mark.parametrize(
    "interruption_factory",
    [KeyboardInterrupt, asyncio.CancelledError],
    ids=["keyboard-interrupt", "asyncio-cancelled"],
)
def test_staging_verification_failure_preserves_primary_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_factory: type[BaseException],
) -> None:
    output = tmp_path / "state.json"
    stage: create_state._CredentialStage | None = None
    cleanup_phase = False
    interruption = interruption_factory()
    original_new_stage = create_state._new_credential_stage
    original_lstat = Path.lstat

    def capture_stage(output_path: Path) -> create_state._CredentialStage:
        nonlocal stage
        stage = original_new_stage(output_path)
        return stage

    def interrupt_dump(*_args: object, **_kwargs: object) -> None:
        nonlocal cleanup_phase
        cleanup_phase = True
        raise interruption

    def reject_stage_verification(path: Path) -> os.stat_result:
        if cleanup_phase and stage is not None and path == stage.directory:
            raise PermissionError("injected staging-directory lstat denial")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(create_state, "_new_credential_stage", capture_stage)
        patch.setattr(create_state.json, "dump", interrupt_dump)
        patch.setattr(Path, "lstat", reject_stage_verification)

        with pytest.raises(interruption_factory) as caught:
            create_state._secure_write_json(  # noqa: SLF001
                str(output),
                {"cookies": [], "origins": []},
                False,
            )

    assert caught.value is interruption
    assert caught.value.credential_state_status == "not-saved"
    assert getattr(caught.value, "cleanup_failures", []) == [
        "failed to verify the private credential staging directory"
    ]
    assert stage is not None
    assert stage.path.is_file()
    assert stage.directory.is_dir()
    assert not output.exists()

    stage.path.unlink()
    stage.directory.rmdir()


@pytest.mark.skipif(os.name == "nt", reason="symlink setup is not portable on Windows")
def test_publish_verification_does_not_chmod_replacement_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "state.json"
    output.write_text('{"old": true}\n', encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("must retain permissions\n", encoding="utf-8")
    victim.chmod(0o644)
    original_replace = create_state.os.replace

    def publish_then_replace_output(source: Path, target: Path) -> None:
        original_replace(source, target)
        Path(target).unlink()
        Path(target).symlink_to(victim)

    monkeypatch.setattr(create_state.os, "replace", publish_then_replace_output)

    with pytest.raises(OSError, match="changed before commit"):
        create_state._secure_write_json(  # noqa: SLF001
            str(output),
            {"cookies": [], "origins": []},
            True,
        )

    assert output.is_symlink()
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert victim.read_text(encoding="utf-8") == "must retain permissions\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink setup is not portable on Windows")
@pytest.mark.parametrize("target_exists", [False, True])
def test_create_state_rejects_final_symlink(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    target = tmp_path / "target.json"
    if target_exists:
        target.write_text("target winner\n", encoding="utf-8")
    output = tmp_path / "state.json"
    output.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        create_storage_state("cookie2=must-not-follow", str(output), force=True)

    assert output.is_symlink()
    if target_exists:
        assert target.read_text(encoding="utf-8") == "target winner\n"
    else:
        assert not target.exists()


def _cookie_args() -> argparse.Namespace:
    return argparse.Namespace(cookie_stdin=True, cookie_file=None, cookie=None)


def test_cookie_stdin_reads_non_tty_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("cookie2=PIPE_SENTINEL\n"))

    assert create_state._read_cookie_input(_cookie_args()) == ("cookie2=PIPE_SENTINEL")


def test_cookie_stdin_uses_hidden_prompt_for_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

        @staticmethod
        def read() -> str:
            raise AssertionError("TTY input must not use an echoed bulk read")

    prompts: list[str] = []

    def fake_getpass(prompt: str, *, stream: object) -> str:
        prompts.append(prompt)
        assert stream is sys.stderr
        return "cookie2=TTY_SENTINEL"

    monkeypatch.setattr(sys, "stdin", TtyInput())
    monkeypatch.setattr(create_state.getpass, "getpass", fake_getpass)

    assert create_state._read_cookie_input(_cookie_args()) == ("cookie2=TTY_SENTINEL")
    assert prompts == ["Cookie header (input hidden): "]


def test_cookie_stdin_fails_closed_when_echo_cannot_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    def unsafe_getpass(_prompt: str, *, stream: object) -> str:
        warnings.warn(
            "terminal control unavailable", create_state.getpass.GetPassWarning
        )
        return "cookie2=MUST_NOT_BE_USED"

    monkeypatch.setattr(sys, "stdin", TtyInput())
    monkeypatch.setattr(create_state.getpass, "getpass", unsafe_getpass)

    with pytest.raises(ValueError, match="unable to disable terminal echo") as exc:
        create_state._read_cookie_input(_cookie_args())

    assert "MUST_NOT_BE_USED" not in str(exc.value)


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_main_cancellation_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    monkeypatch.setattr(
        create_state,
        "_read_cookie_input",
        lambda _args: "cookie2=secret",
    )

    def cancel(
        _cookie: str,
        _output: str,
        *,
        force: bool,
        progress: create_state.CredentialWriteProgress,
    ) -> Path:
        assert force is False
        raise interruption

    monkeypatch.setattr(create_state, "create_storage_state", cancel)

    assert (
        create_state.main(["--cookie-stdin", "--output", str(tmp_path / "state.json")])
        == 130
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_type"] == error_type
    assert payload["state"]["status"] == "not-saved"
    assert payload["authentication"]["status"] == "not-established"
    assert payload["search_capability"]["status"] == "not-tested"


def test_main_cancellation_after_commit_reports_candidate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "state.json"
    monkeypatch.setattr(
        create_state,
        "_read_cookie_input",
        lambda _args: "cookie2=secret",
    )

    def commit_then_cancel(
        _cookie: str,
        _output: str,
        *,
        force: bool,
        progress: create_state.CredentialWriteProgress,
    ) -> Path:
        assert force is False
        output.write_text("{}\n", encoding="utf-8")
        progress.mark_committed(output)
        raise KeyboardInterrupt

    monkeypatch.setattr(create_state, "create_storage_state", commit_then_cancel)

    assert create_state.main(["--cookie-stdin", "--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"] == {
        "status": "candidate-saved",
        "output": str(output),
    }
    assert payload["authentication"]["status"] == "not-established"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_main_cancellation_during_success_json_reports_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    output = tmp_path / "state.json"
    original_dumps = create_state.json.dumps
    success_cancelled = False

    def cancel_success_json(
        payload: object,
        *args: object,
        **kwargs: object,
    ) -> str:
        nonlocal success_cancelled
        if (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and not success_cancelled
        ):
            success_cancelled = True
            raise interruption()
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(create_state, "_read_cookie_input", lambda _args: "cookie2=x")
    monkeypatch.setattr(create_state.json, "dumps", cancel_success_json)

    assert create_state.main(["--cookie-stdin", "--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error_type"] == error_type
    assert payload["state"] == {
        "status": "candidate-saved",
        "output": str(output),
    }
    assert output.is_file()


@pytest.mark.skipif(os.name == "nt", reason="PTY integration is POSIX-only")
def test_cookie_stdin_does_not_echo_secret_in_real_pty(tmp_path: Path) -> None:
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")
    output = tmp_path / "state.json"
    script = Path(create_state.__file__).resolve()
    cookie_header = "cookie2=PTY_SENTINEL_7D31"
    argv = [
        sys.executable,
        str(script),
        "--cookie-stdin",
        "--output",
        str(output),
    ]

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, argv)  # noqa: S606

    transcript = bytearray()
    status: int | None = None
    echo_restored = False
    try:
        deadline = time.monotonic() + 10
        while b"input hidden" not in transcript:
            if time.monotonic() >= deadline:
                pytest.fail("timed out waiting for hidden Cookie prompt")
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                transcript.extend(os.read(master_fd, 4096))

        os.write(master_fd, f"{cookie_header}\n".encode())

        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    transcript.extend(os.read(master_fd, 4096))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                break
        if status is None:
            waited_pid, waited_status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                pytest.fail("timed out waiting for create_state.py to exit")
            status = waited_status
        echo_restored = bool(termios.tcgetattr(master_fd)[3] & termios.ECHO)
    finally:
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(master_fd)

    assert os.waitstatus_to_exitcode(status) == 0
    assert cookie_header.encode() not in transcript
    assert echo_restored
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["cookies"][0]["value"] == "PTY_SENTINEL_7D31"
