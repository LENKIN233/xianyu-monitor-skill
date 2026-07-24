from __future__ import annotations

import argparse
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
