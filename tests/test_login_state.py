from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import login_state
import pytest


class InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _function_line(function: Any, marker: str) -> int:
    lines, first_line = inspect.getsourcelines(function)
    return first_line + next(
        index for index, line in enumerate(lines) if line.strip() == marker
    )


def read_immediately(
    stream: io.StringIO,
    _timeout: float,
    browser_closed: Any,
) -> str:
    if browser_closed is not None and browser_closed():
        raise ValueError("browser closed before interactive confirmation")
    return stream.readline()


def test_confirmation_requires_real_tty() -> None:
    with pytest.raises(ValueError, match="interactive terminal required"):
        login_state._read_explicit_confirmation(
            io.StringIO("SAVE-1234\n"),
            io.StringIO(),
            "SAVE-1234",
            1,
        )


@pytest.mark.parametrize("text", ["", "SAVE-WRONG\n", "错误\n"])
def test_eof_or_wrong_confirmation_fails_closed(text: str) -> None:
    with pytest.raises(ValueError, match="confirmation"):
        login_state._read_explicit_confirmation(
            InteractiveInput(text),
            io.StringIO(),
            "SAVE-1234",
            1,
            line_reader=read_immediately,
        )


def test_exact_confirmation_is_accepted_without_echoing_secret() -> None:
    errors = io.StringIO()
    login_state._read_explicit_confirmation(
        InteractiveInput("SAVE-1234\n"),
        errors,
        "SAVE-1234",
        1,
        line_reader=read_immediately,
    )

    assert "SAVE-1234" in errors.getvalue()


def test_closed_browser_cannot_be_confirmed() -> None:
    with pytest.raises(ValueError, match="browser closed"):
        login_state._read_explicit_confirmation(
            InteractiveInput("SAVE-1234\n"),
            io.StringIO(),
            "SAVE-1234",
            1,
            browser_closed=lambda: True,
            line_reader=read_immediately,
        )


def test_browser_confirmation_is_local_only_and_records_its_channel() -> None:
    events: list[Any] = []

    class Page:
        url = "about:blank"

        def __init__(self) -> None:
            self.main_frame = object()
            self.binding: Any = None
            self.wait_count = 0

        def expose_binding(self, name: str, callback: Any) -> None:
            events.append(("binding", name))
            self.binding = callback

        def set_content(self, document: str, **kwargs: Any) -> None:
            events.append(("document", document, kwargs))

        def bring_to_front(self) -> None:
            events.append("front")

        def is_closed(self) -> bool:
            return False

        def wait_for_timeout(self, timeout: int) -> None:
            events.append(("wait", timeout))
            self.wait_count += 1
            source = {"page": self, "frame": self.main_frame}
            submitted = "SAVE-WRONG" if self.wait_count == 1 else "SAVE-SYNTHETIC"
            accepted = self.binding(source, submitted)
            events.append(("binding-result", accepted))

        def close(self) -> None:
            events.append("close")

    class Context:
        def new_page(self) -> Page:
            return Page()

    errors = io.StringIO()
    progress = login_state.CaptureProgress()
    login_state._read_browser_confirmation(
        Context(),
        errors,
        "SAVE-SYNTHETIC",
        5,
        progress,
    )

    document = next(event[1] for event in events if event[0] == "document")
    assert "SAVE-SYNTHETIC" in document
    assert "default-src 'none'" in document
    assert "SAVE-SYNTHETIC" not in errors.getvalue()
    assert json.loads(errors.getvalue())["status"] == "browser-confirmation-ready"
    assert ("binding-result", False) in events
    assert ("binding-result", True) in events
    assert progress.confirmation_received is True
    assert progress.confirmation_channel == "browser"
    assert events[-1] == "close"


@pytest.mark.skipif(os.name == "nt", reason="POSIX selector/pipe coverage")
def test_terminal_timeout_leaves_no_confirmation_reader_thread() -> None:
    read_descriptor, write_descriptor = os.pipe()
    before = {thread.ident for thread in threading.enumerate()}
    try:
        with os.fdopen(read_descriptor, encoding="utf-8") as stream:
            with pytest.raises(TimeoutError, match="interactive confirmation"):
                login_state._wait_for_terminal_line(stream, 0.01, None)
    finally:
        os.close(write_descriptor)

    assert {thread.ident for thread in threading.enumerate()} == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY coverage")
def test_partial_raw_tty_input_still_honors_timeout_and_restores_blocking() -> None:
    import pty
    import tty

    master_descriptor, slave_descriptor = pty.openpty()
    tty.setcbreak(slave_descriptor)
    outcome: list[BaseException | str] = []

    def read_partial_line() -> None:
        with os.fdopen(
            os.dup(slave_descriptor),
            encoding="utf-8",
            closefd=True,
        ) as stream:
            try:
                outcome.append(login_state._wait_for_terminal_line(stream, 0.05, None))
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

    reader = threading.Thread(target=read_partial_line, daemon=True)
    try:
        reader.start()
        os.write(master_descriptor, b"S")
        reader.join(timeout=0.75)
        completed_before_cleanup = not reader.is_alive()
        blocking_was_restored = os.get_blocking(slave_descriptor)
    finally:
        os.close(master_descriptor)
        os.close(slave_descriptor)
        reader.join(timeout=0.75)

    assert completed_before_cleanup
    assert blocking_was_restored is True
    assert len(outcome) == 1
    assert isinstance(outcome[0], TimeoutError)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY coverage")
def test_terminal_ctrl_c_is_read_locally_and_terminal_mode_is_restored() -> None:
    import pty
    import termios

    master_descriptor, slave_descriptor = pty.openpty()
    original_attributes = termios.tcgetattr(slave_descriptor)
    outcome: list[BaseException | str] = []

    def read_line() -> None:
        with os.fdopen(
            os.dup(slave_descriptor),
            encoding="utf-8",
            closefd=True,
        ) as stream:
            try:
                outcome.append(login_state._wait_for_terminal_line(stream, 1, None))
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

    reader = threading.Thread(target=read_line, daemon=True)
    try:
        reader.start()
        deadline = time.monotonic() + 0.5
        while (
            termios.tcgetattr(slave_descriptor)[3] & termios.ISIG
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert not termios.tcgetattr(slave_descriptor)[3] & termios.ISIG
        os.write(master_descriptor, b"\x03")
        reader.join(timeout=0.75)
        restored_attributes = termios.tcgetattr(slave_descriptor)
    finally:
        os.close(master_descriptor)
        os.close(slave_descriptor)
        reader.join(timeout=0.75)

    assert not reader.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], KeyboardInterrupt)
    pending_input_flag = getattr(termios, "PENDIN", 0)
    original_comparable = list(original_attributes)
    restored_comparable = list(restored_attributes)
    original_comparable[3] &= ~pending_input_flag
    restored_comparable[3] &= ~pending_input_flag
    assert restored_comparable == original_comparable


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY coverage")
def test_terminal_mode_restores_even_when_blocking_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pty
    import termios

    master_descriptor, slave_descriptor = pty.openpty()
    original_attributes = termios.tcgetattr(slave_descriptor)
    original_set_blocking = login_state.os.set_blocking
    calls = 0

    def fail_restore(descriptor: int, blocking: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated blocking restore failure")
        original_set_blocking(descriptor, blocking)

    monkeypatch.setattr(login_state.os, "set_blocking", fail_restore)
    try:
        os.write(master_descriptor, b"SAVE-1234\n")
        with os.fdopen(
            os.dup(slave_descriptor),
            encoding="utf-8",
            closefd=True,
        ) as stream:
            with pytest.raises(ValueError, match="restore") as captured:
                login_state._wait_for_terminal_line(stream, 1, None)
        restored_attributes = termios.tcgetattr(slave_descriptor)
    finally:
        os.close(master_descriptor)
        os.close(slave_descriptor)

    pending_input_flag = getattr(termios, "PENDIN", 0)
    assert (
        restored_attributes[3] & ~pending_input_flag
        == original_attributes[3] & ~pending_input_flag
    )
    assert captured.value.cleanup_failures == [
        "failed to restore the interactive terminal state"
    ]


def test_windows_console_reader_handles_editing_without_a_thread() -> None:
    class Console:
        def __init__(self) -> None:
            self.characters = iter(["S", "A", "X", "\b", "V", "E", "\r"])

        def kbhit(self) -> bool:
            return True

        def getwche(self) -> str:
            return next(self.characters)

        def getwch(self) -> str:
            return next(self.characters)

    assert (
        login_state._wait_for_windows_console_line(
            1,
            None,
            console=Console(),
        )
        == "SAVE"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://passport.goofish.com/mini_login.htm",
        "https://www.goofish.com/login",
        "https://www.goofish.com/punish",
        "https://www.goofish.com/?challenge=1",
        "https://www.goofish.com/#captcha",
        "http://www.goofish.com/",
        "https://www.goofish.com:8443/",
        "https://example.com/",
    ],
)
def test_confirmation_rejects_login_challenge_and_other_hosts(url: str) -> None:
    with pytest.raises(ValueError, match="not a normal Xianyu page"):
        login_state._validate_confirmation_page(url)


def test_confirmation_page_error_does_not_leak_query() -> None:
    with pytest.raises(ValueError) as captured:
        login_state._validate_confirmation_page(
            "https://passport.goofish.com/login?token=QUERY_SENTINEL"
        )

    assert "QUERY_SENTINEL" not in str(captured.value)


def test_nav_display_name_requires_success_and_nonempty_value() -> None:
    payload = {
        "ret": ["SUCCESS::ok"],
        "data": {"module": {"base": {"displayName": "VISIBLE_SENTINEL"}}},
    }
    assert login_state._has_nav_display_name(payload)
    assert not login_state._has_nav_display_name(
        {
            "ret": ["FAIL_SYS::rejected"],
            "data": {"module": {"base": {"displayName": "VISIBLE_SENTINEL"}}},
        }
    )
    assert not login_state._has_nav_display_name(
        {"ret": ["SUCCESS::ok"], "data": {"module": {"base": {"displayName": ""}}}}
    )
    assert not login_state._has_nav_display_name(
        {
            "ret": ["SUCCESS::ok", "FAIL_SYS::rejected"],
            "data": {"module": {"base": {"displayName": "VISIBLE_SENTINEL"}}},
        }
    )
    assert not login_state._has_nav_display_name(
        {
            "ret": ["SUCCESS::ok"],
            "data": {"module": {"base": {"displayName": "请登录"}}},
        }
    )
    assert not login_state._has_nav_display_name(
        {
            "ret": ["SUCCESS_BOGUS"],
            "data": {"module": {"base": {"displayName": "VISIBLE_SENTINEL"}}},
        }
    )


def test_browser_confirmation_explains_scan_is_not_login_completion() -> None:
    document = login_state._browser_confirmation_document(
        "SAVE-1234",
        "__confirm",
    )

    assert "扫码后还要在手机闲鱼中确认登录" in document
    assert "二维码消失不代表登录已经完成" in document
    assert "专用 Chrome 会自动关闭" in document
    assert "SAVE-1234" in document


def test_login_parser_allows_thirty_minutes_by_default() -> None:
    args = login_state.build_parser().parse_args(
        ["--output", str(Path.cwd() / "state.json")]
    )

    assert args.timeout == 1_800


def test_storage_state_filter_removes_non_goofish_cookies_and_origins() -> None:
    state = {
        "cookies": [
            {
                "name": "site",
                "value": "SITE_SENTINEL",
                "domain": ".goofish.com",
                "path": "/",
            },
            {
                "name": "third-party",
                "value": "THIRD_PARTY_SENTINEL",
                "domain": ".taobao.com",
                "path": "/",
            },
        ],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [{"name": "site", "value": "SITE_ORIGIN_SENTINEL"}],
            },
            {
                "origin": "https://login.taobao.com",
                "localStorage": [
                    {"name": "third-party", "value": "THIRD_PARTY_ORIGIN_SENTINEL"}
                ],
            },
        ],
    }

    assert login_state._filter_goofish_storage_state(state) == {
        "cookies": [
            {
                "name": "site",
                "value": "SITE_SENTINEL",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [{"name": "site", "value": "SITE_ORIGIN_SENTINEL"}],
            }
        ],
    }


def test_nav_response_url_requires_exact_https_goofish_api_path() -> None:
    assert login_state._is_nav_response_url(
        "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/"
    )
    assert not login_state._is_nav_response_url(
        "https://example.com/?api=mtop.idle.web.user.page.nav"
    )
    assert not login_state._is_nav_response_url(
        "http://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/"
    )
    assert not login_state._is_nav_response_url(
        "https://evil.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/"
    )
    assert not login_state._is_nav_response_url(
        "https://h5api.m.goofish.com:8443/h5/mtop.idle.web.user.page.nav/1.0/"
    )


class FakeResponse:
    url = (
        "https://h5api.m.goofish.com/h5/"
        "mtop.idle.web.user.page.nav/1.0/?data=SECRET_QUERY"
    )

    def json(self) -> dict[str, Any]:
        return {
            "ret": ["SUCCESS::ok"],
            "data": {"module": {"base": {"displayName": "IDENTITY_SENTINEL"}}},
        }


class FakePage:
    def __init__(self, events: list[str]):
        self.url = "https://www.goofish.com/"
        self.events = events
        self.response_callback: Any = None

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.events.append("observer-installed")
        self.response_callback = callback

    def goto(self, *_args: Any, **_kwargs: Any) -> None:
        self.events.append("goto")
        if self.response_callback is not None:
            self.response_callback(FakeResponse())

    def wait_for_timeout(self, _timeout: int) -> None:
        self.events.append("wait")

    def is_closed(self) -> bool:
        return False

    def close(self) -> None:
        self.events.append("validation-close")


class FakeContext:
    def __init__(self, events: list[str], state: dict[str, Any]):
        self.events = events
        self.state = state

    def new_page(self) -> FakePage:
        return FakePage(self.events)

    def storage_state(self, *, indexed_db: bool = False) -> dict[str, Any]:
        assert indexed_db is True
        self.events.append("storage-state")
        return self.state

    def cookies(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("Cookie polling must never be used as login proof")


class FakeBrowser:
    def __init__(
        self,
        context: FakeContext,
        events: list[str],
        *,
        close_error: bool = False,
    ):
        self.context = context
        self.events = events
        self.close_error = close_error

    def new_context(self, **_kwargs: Any) -> FakeContext:
        return self.context

    def close(self) -> None:
        self.events.append("browser-close")
        if self.close_error:
            raise RuntimeError("browser close failed")


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self.browser = browser

    def launch(self, **_kwargs: Any) -> FakeBrowser:
        return self.browser


class FakePlaywright:
    def __init__(
        self,
        browser: FakeBrowser,
        events: list[str],
        *,
        stop_error: bool = False,
    ):
        self.chromium = FakeChromium(browser)
        self.events = events
        self.stop_error = stop_error

    def stop(self) -> None:
        self.events.append("playwright-stop")
        if self.stop_error:
            raise RuntimeError("runtime stop failed")


class FakePlaywrightManager:
    def __init__(
        self,
        browser: FakeBrowser,
        events: list[str],
        *,
        stop_error: bool = False,
    ):
        self.playwright = FakePlaywright(
            browser,
            events,
            stop_error=stop_error,
        )

    def start(self) -> FakePlaywright:
        return self.playwright


def _install_fake_browser(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    state: dict[str, Any],
    *,
    browser_close_error: bool = False,
    runtime_stop_error: bool = False,
) -> None:
    context = FakeContext(events, state)
    browser = FakeBrowser(context, events, close_error=browser_close_error)
    monkeypatch.setattr(
        login_state,
        "sync_playwright",
        lambda: FakePlaywrightManager(
            browser,
            events,
            stop_error=runtime_stop_error,
        ),
    )


def test_cdp_capture_is_rejected_before_browser_or_output_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_playwright() -> None:
        pytest.fail("disabled CDP must fail before Playwright starts")

    monkeypatch.setattr(login_state, "sync_playwright", unexpected_playwright)

    with pytest.raises(ValueError, match="raw TCP CDP is disabled"):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            cdp_user_data_dir=str(tmp_path / "missing-profile"),
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
        )

    assert not (tmp_path / "state.json").exists()


def test_browser_confirmation_allows_non_tty_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "SYNTHETIC",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)

    def confirm_in_browser(
        _context: Any,
        _errors: Any,
        _token: str,
        _timeout: float,
        progress: login_state.CaptureProgress,
    ) -> None:
        events.append("browser-confirmed")
        progress.confirmation_received = True
        progress.confirmation_channel = "browser"

    monkeypatch.setattr(
        login_state,
        "_read_browser_confirmation",
        confirm_in_browser,
    )

    output = login_state.capture_login_state(
        str(tmp_path / "state.json"),
        browser_channel=None,
        timeout_seconds=5,
        force=False,
        confirm_in_browser=True,
        input_stream=io.StringIO(),
    )

    assert output == tmp_path / "state.json"
    assert "browser-confirmed" in events


def test_state_is_saved_only_after_confirmation_and_nav_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "COOKIE_SENTINEL",
                "domain": ".goofish.com",
                "path": "/",
            },
            {
                "name": "sso",
                "value": "THIRD_PARTY_SENTINEL",
                "domain": ".taobao.com",
                "path": "/",
            },
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )

    output = login_state.capture_login_state(
        str(tmp_path / "state.json"),
        browser_channel=None,
        timeout_seconds=5,
        force=False,
        input_stream=InteractiveInput(),
        token_factory=lambda: "SAVE-1234",
    )

    assert output == tmp_path / "state.json"
    assert events == [
        "goto",
        "user-confirmed",
        "observer-installed",
        "goto",
        "validation-close",
        "storage-state",
        "browser-close",
        "playwright-stop",
    ]
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted == {
        "cookies": [
            {
                "name": "cookie2",
                "value": "COOKIE_SENTINEL",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    assert "THIRD_PARTY_SENTINEL" not in output.read_text(encoding="utf-8")


def test_outdated_playwright_fails_with_dependency_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class LegacyContext(FakeContext):
        def storage_state(self) -> dict[str, Any]:  # type: ignore[override]
            raise AssertionError("the old signature must fail before this body")

    context = LegacyContext(events, {"cookies": [], "origins": []})
    browser = FakeBrowser(context, events)
    monkeypatch.setattr(
        login_state,
        "sync_playwright",
        lambda: FakePlaywrightManager(browser, events),
    )
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )

    with pytest.raises(login_state.DependencyError, match="1.51 or newer"):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
        )

    assert not (tmp_path / "state.json").exists()
    assert events[-2:] == ["browser-close", "playwright-stop"]


def test_failed_confirmation_never_reads_or_writes_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "anonymous-cookie",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)

    def reject(*_args: Any, **_kwargs: Any) -> None:
        events.append("confirmation-rejected")
        raise ValueError("confirmation did not match")

    monkeypatch.setattr(login_state, "_read_explicit_confirmation", reject)

    with pytest.raises(ValueError, match="confirmation did not match"):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
        )

    assert "storage-state" not in events
    assert not (tmp_path / "state.json").exists()
    assert events[-2:] == ["browser-close", "playwright-stop"]


def test_confirmation_without_session_material_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_fake_browser(monkeypatch, events, {"cookies": [], "origins": []})
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )

    with pytest.raises(ValueError, match="no retained Goofish storage material"):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
        )

    assert not (tmp_path / "state.json").exists()


def test_missing_nav_display_name_still_saves_explicit_candidate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "anonymous-cookie",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )

    def miss_marker(*_args: Any, **_kwargs: Any) -> bool:
        events.append("marker-not-observed")
        return False

    monkeypatch.setattr(login_state, "_check_nav_display_name", miss_marker)
    progress = login_state.CaptureProgress()

    output = login_state.capture_login_state(
        str(tmp_path / "state.json"),
        browser_channel=None,
        timeout_seconds=5,
        force=False,
        input_stream=InteractiveInput(),
        progress=progress,
    )

    assert output == tmp_path / "state.json"
    assert "storage-state" in events
    assert progress.nav_display_name_checked is True
    assert progress.nav_display_name_present is False
    assert progress.state_saved is True


@pytest.mark.parametrize(
    "probe_error",
    [
        TimeoutError("optional navigation probe timed out"),
        login_state.PlaywrightError("optional navigation probe failed"),
        ValueError("optional navigation response changed"),
    ],
)
def test_optional_nav_probe_error_still_saves_candidate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: Exception,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "candidate",
                "value": "SYNTHETIC",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )

    def fail_probe(*_args: Any, **_kwargs: Any) -> bool:
        raise probe_error

    monkeypatch.setattr(login_state, "_check_nav_display_name", fail_probe)
    progress = login_state.CaptureProgress()

    output = login_state.capture_login_state(
        str(tmp_path / "state.json"),
        browser_channel=None,
        timeout_seconds=5,
        force=False,
        input_stream=InteractiveInput(),
        progress=progress,
    )

    assert output == tmp_path / "state.json"
    assert progress.nav_display_name_checked is True
    assert progress.nav_display_name_present is False
    assert progress.state_saved is True
    assert events[-3:] == ["storage-state", "browser-close", "playwright-stop"]


def test_optional_nav_page_creation_error_still_saves_candidate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "candidate",
                "value": "SYNTHETIC",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }

    class ValidationPageFailingContext(FakeContext):
        page_calls = 0

        def new_page(self) -> FakePage:
            self.page_calls += 1
            if self.page_calls == 2:
                raise login_state.PlaywrightError(
                    "optional validation page could not be created"
                )
            return super().new_page()

    context = ValidationPageFailingContext(events, state)
    browser = FakeBrowser(context, events)
    monkeypatch.setattr(
        login_state,
        "sync_playwright",
        lambda: FakePlaywrightManager(browser, events),
    )
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("user-confirmed"),
    )
    progress = login_state.CaptureProgress()

    output = login_state.capture_login_state(
        str(tmp_path / "state.json"),
        browser_channel=None,
        timeout_seconds=5,
        force=False,
        input_stream=InteractiveInput(),
        progress=progress,
    )

    assert output == tmp_path / "state.json"
    assert progress.nav_display_name_checked is True
    assert progress.nav_display_name_present is False
    assert progress.state_saved is True
    assert events[-3:] == ["storage-state", "browser-close", "playwright-stop"]


def test_close_failure_reports_candidate_file_as_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(
        monkeypatch,
        events,
        state,
        browser_close_error=True,
    )
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("interactive-token-received"),
    )
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())
    output = tmp_path / "state.json"

    assert login_state.main(["--output", str(output)]) == 2
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert output.is_file()
    assert payload["error"] == "failed to close the dedicated browser"
    assert payload["state"] == {"status": "candidate-saved"}
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["confirmation"]["actor"] == "not-machine-verified"
    assert payload["confirmation"]["channel"] == "terminal"
    assert payload["session"]["nav_display_name"] == "present"
    assert payload["authentication"]["status"] == "not-established"
    assert payload["identity"]["status"] == "not-machine-verified"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to close the dedicated browser"],
    }


def test_cleanup_failures_never_mask_primary_confirmation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(
        monkeypatch,
        events,
        state,
        browser_close_error=True,
        runtime_stop_error=True,
    )

    def reject(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("confirmation did not match")

    monkeypatch.setattr(login_state, "_read_explicit_confirmation", reject)
    progress = login_state.CaptureProgress()

    with pytest.raises(ValueError, match="confirmation did not match") as captured:
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
            progress=progress,
        )

    assert type(captured.value) is ValueError
    assert progress.cleanup_failures == [
        "failed to close the dedicated browser",
        "failed to stop the dedicated browser runtime",
    ]
    assert not (tmp_path / "state.json").exists()


def test_cleanup_interruption_is_recorded_before_propagating() -> None:
    progress = login_state.CaptureProgress()

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        login_state._cleanup_preserving_primary_error(  # noqa: SLF001
            interrupt,
            "failed to close the dedicated browser",
            progress,
        )

    assert progress.cleanup_failures == [
        "failed to close the dedicated browser",
    ]


def test_interrupted_playwright_start_attempts_manager_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class InterruptedManager:
        def start(self) -> None:
            events.append("start")
            raise KeyboardInterrupt

        def __exit__(self, *_args: object) -> None:
            events.append("manager-exit")

    monkeypatch.setattr(login_state, "sync_playwright", InterruptedManager)
    progress = login_state.CaptureProgress()

    with pytest.raises(KeyboardInterrupt):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
            progress=progress,
        )

    assert events == ["start", "manager-exit"]
    assert progress.cleanup_failures == []
    assert not (tmp_path / "state.json").exists()


def test_interrupted_playwright_start_reports_failed_manager_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedManager:
        def start(self) -> None:
            raise KeyboardInterrupt

        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("simulated manager cleanup failure")

    monkeypatch.setattr(login_state, "sync_playwright", InterruptedManager)
    progress = login_state.CaptureProgress()

    with pytest.raises(KeyboardInterrupt):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
            progress=progress,
        )

    assert progress.cleanup_failures == [
        "failed to stop the partially started browser runtime"
    ]
    assert not (tmp_path / "state.json").exists()


def test_start_error_does_not_mask_cleanup_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_interruption = KeyboardInterrupt()

    class BrokenManager:
        def start(self) -> None:
            raise RuntimeError("simulated start failure")

        def __exit__(self, *_args: object) -> None:
            raise cleanup_interruption

    monkeypatch.setattr(login_state, "sync_playwright", BrokenManager)
    progress = login_state.CaptureProgress()

    with pytest.raises(KeyboardInterrupt) as captured:
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=InteractiveInput(),
            progress=progress,
        )

    assert captured.value is cleanup_interruption
    assert isinstance(captured.value.__context__, RuntimeError)
    assert progress.cleanup_failures == [
        "failed to stop the partially started browser runtime"
    ]
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize(
    ("boundary", "expected_events"),
    [
        (
            'launch_kwargs: dict[str, Any] = {"headless": False}',
            ["manager-start", "playwright-stop"],
        ),
        (
            "if browser is not None:",
            [
                "manager-start",
                "browser-launch",
                "browser-close",
                "playwright-stop",
            ],
        ),
    ],
)
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_acquisition_boundary_interruption_closes_every_acquired_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_events: list[str],
    interruption: type[BaseException],
) -> None:
    events: list[str] = []

    class Browser:
        def close(self) -> None:
            events.append("browser-close")

    class Chromium:
        def launch(self, **_kwargs: Any) -> Browser:
            events.append("browser-launch")
            return Browser()

    class Playwright:
        chromium = Chromium()

        def stop(self) -> None:
            events.append("playwright-stop")

    class Manager:
        def start(self) -> Playwright:
            events.append("manager-start")
            return Playwright()

    monkeypatch.setattr(login_state, "sync_playwright", Manager)
    target_line = _function_line(login_state.capture_login_state, boundary)
    previous_trace = sys.gettrace()
    triggered = False

    def interrupt_at_boundary(
        frame: Any,
        event: str,
        _argument: Any,
    ) -> Any:
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is login_state.capture_login_state.__code__
            and frame.f_lineno == target_line
        ):
            triggered = True
            sys.settrace(None)
            raise interruption
        return interrupt_at_boundary

    sys.settrace(interrupt_at_boundary)
    try:
        with pytest.raises(interruption):
            login_state.capture_login_state(
                str(tmp_path / "state.json"),
                browser_channel=None,
                timeout_seconds=5,
                force=False,
                input_stream=InteractiveInput(),
            )
    finally:
        sys.settrace(previous_trace)

    assert triggered is True
    assert events == expected_events
    assert not (tmp_path / "state.json").exists()


def test_non_tty_capture_fails_before_browser_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def browser_must_not_start() -> None:
        raise AssertionError("browser must not start for non-interactive input")

    monkeypatch.setattr(login_state, "sync_playwright", browser_must_not_start)

    with pytest.raises(ValueError, match="interactive terminal required"):
        login_state.capture_login_state(
            str(tmp_path / "state.json"),
            browser_channel=None,
            timeout_seconds=5,
            force=False,
            input_stream=io.StringIO("SAVE-1234\n"),
        )

    assert not (tmp_path / "state.json").exists()


def test_success_output_separates_state_identity_and_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "state.json"

    def succeed(*_args: Any, **kwargs: Any) -> Path:
        progress = kwargs["progress"]
        progress.confirmation_received = True
        progress.nav_display_name_present = True
        progress.saved_output = output
        progress.state_saved = True
        return output

    monkeypatch.setattr(login_state, "capture_login_state", succeed)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(output)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    opening = json.loads(captured.err)

    assert opening["status"] == "browser-opening"
    assert payload["state"]["status"] == "candidate-saved"
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["confirmation"]["actor"] == "not-machine-verified"
    assert payload["confirmation"]["channel"] == "terminal"
    assert payload["session"]["nav_display_name"] == "present"
    assert payload["authentication"] == {"status": "not-established"}
    assert payload["identity"] == {"status": "not-machine-verified"}
    assert payload["search_capability"]["status"] == "not-tested"
    assert "authenticated" not in captured.out.lower()
    assert "IDENTITY_SENTINEL" not in captured.out


def test_success_output_allows_optional_nav_signal_to_be_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "state.json"

    def succeed(*_args: Any, **kwargs: Any) -> Path:
        progress = kwargs["progress"]
        progress.confirmation_received = True
        progress.nav_display_name_checked = True
        progress.saved_output = output
        progress.state_saved = True
        return output

    monkeypatch.setattr(login_state, "capture_login_state", succeed)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(output)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"] == {"status": "candidate-saved"}
    assert payload["session"] == {"nav_display_name": "not-observed"}
    assert payload["authentication"] == {"status": "not-established"}
    assert payload["identity"] == {"status": "not-established"}
    assert payload["search_capability"] == {"status": "not-tested"}


def test_main_non_tty_reports_no_saved_state_without_starting_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def capture_must_not_start(*_args: Any, **_kwargs: Any) -> Path:
        raise AssertionError("capture must not start for non-interactive input")

    monkeypatch.setattr(login_state, "capture_login_state", capture_must_not_start)
    monkeypatch.setattr(login_state.sys, "stdin", io.StringIO())

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"
    assert payload["confirmation"]["actor"] == "not-established"
    assert payload["confirmation"]["channel"] == "not-established"
    assert payload["handoff"]["required"] is True
    assert payload["handoff"]["environment"] == "normal-user-terminal"
    template = payload["handoff"]["argv_template"]
    assert template[0] == sys.executable
    assert template[3] == "<PRIVATE_STATE_PATH>"
    assert str(tmp_path) not in json.dumps(template)
    assert template[-2:] == [
        "--timeout",
        "1800",
    ]
    assert payload["authentication"]["status"] == "not-established"
    assert payload["identity"]["status"] == "not-established"
    assert payload["search_capability"]["status"] == "not-tested"


def test_main_rejects_cdp_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_capture(*_args: Any, **_kwargs: Any) -> Path:
        pytest.fail("disabled CDP must fail during argument parsing")

    monkeypatch.setattr(login_state, "capture_login_state", unexpected_capture)

    with pytest.raises(SystemExit) as captured:
        login_state.main(
            [
                "--output",
                str(tmp_path / "state.json"),
                "--cdp-user-data-dir",
                str(tmp_path / "profile"),
                "--confirm-in-browser",
            ]
        )

    assert captured.value.code == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert "raw TCP CDP is disabled" in payload["error"]
    assert str(tmp_path / "profile") not in output.out
    assert output.err == ""


def test_main_preserves_missing_playwright_install_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(login_state, "sync_playwright", None)
    monkeypatch.setattr(login_state, "PlaywrightError", Exception)

    assert (
        login_state.main(
            [
                "--output",
                str(tmp_path / "state.json"),
                "--confirm-in-browser",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)

    assert "playwright is not installed" in payload["error"]
    assert "pip install -r requirements.txt" in payload["error"]
    assert payload["state"]["status"] == "not-saved"


def test_main_with_missing_stdin_fails_as_structured_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(login_state.sys, "stdin", None)

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"


def test_main_with_broken_stdin_fails_as_structured_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenInput:
        def isatty(self) -> bool:
            raise OSError("terminal unavailable")

    monkeypatch.setattr(login_state.sys, "stdin", BrokenInput())

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_tty_probe_cancellation_is_structured_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    def cancel_tty_probe(_stream: Any) -> bool:
        raise interruption

    monkeypatch.setattr(login_state, "_is_interactive_stream", cancel_tty_probe)

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 130
    output_lines = capsys.readouterr().out.splitlines()

    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["error"] == "capture cancelled"
    assert payload["error_type"] == error_type
    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_browser_opening_serialization_cancellation_is_structured_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    original_dumps = login_state.json.dumps

    def cancel_opening_payload(payload: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(payload, dict) and payload.get("status") == "browser-opening":
            raise interruption
        return original_dumps(payload, *args, **kwargs)

    def capture_must_not_start(*_args: Any, **_kwargs: Any) -> Path:
        raise AssertionError("capture must not start before opening output completes")

    monkeypatch.setattr(login_state.json, "dumps", cancel_opening_payload)
    monkeypatch.setattr(login_state, "capture_login_state", capture_must_not_start)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 130
    output_lines = capsys.readouterr().out.splitlines()

    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["error"] == "capture cancelled"
    assert payload["error_type"] == error_type
    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_cancellation_between_opening_output_and_capture_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    def capture_must_not_start(*_args: Any, **_kwargs: Any) -> Path:
        raise AssertionError("trace interruption must run before capture")

    monkeypatch.setattr(login_state, "capture_login_state", capture_must_not_start)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())
    main_implementation = inspect.unwrap(login_state.main)
    target_line = _function_line(main_implementation, "capture_login_state(")
    previous_trace = sys.gettrace()
    triggered = False

    def interrupt_at_capture_boundary(
        frame: Any,
        event: str,
        _argument: Any,
    ) -> Any:
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is main_implementation.__code__
            and frame.f_lineno == target_line
        ):
            triggered = True
            sys.settrace(None)
            raise interruption
        return interrupt_at_capture_boundary

    sys.settrace(interrupt_at_capture_boundary)
    try:
        assert login_state.main(["--output", str(tmp_path / "state.json")]) == 130
    finally:
        sys.settrace(previous_trace)
    captured = capsys.readouterr()
    output_lines = captured.out.splitlines()

    assert triggered is True
    assert len(output_lines) == 1
    opening = json.loads(captured.err)
    cancellation = json.loads(output_lines[0])
    assert opening["status"] == "browser-opening"
    assert cancellation["error"] == "capture cancelled"
    assert cancellation["error_type"] == error_type
    assert cancellation["state"]["status"] == "not-saved"
    assert cancellation["confirmation"]["status"] == "not-received"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_main_cancellation_is_structured_and_never_claims_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    def cancel(*_args: Any, **_kwargs: Any) -> Path:
        raise interruption

    monkeypatch.setattr(login_state, "capture_login_state", cancel)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 130
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["error"] == "capture cancelled"
    assert payload["error_type"] == error_type
    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "not-received"
    assert payload["identity"]["status"] == "not-established"


def test_cancellation_after_persistence_reports_candidate_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "state.json"

    def cancel_after_save(*_args: Any, **kwargs: Any) -> Path:
        progress = kwargs["progress"]
        progress.confirmation_received = True
        progress.nav_display_name_present = True
        progress.saved_output = output
        progress.state_saved = True
        output.write_text("{}", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(login_state, "capture_login_state", cancel_after_save)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["error"] == "capture cancelled"
    assert payload["state"] == {"status": "candidate-saved"}
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["confirmation"]["actor"] == "not-machine-verified"
    assert payload["session"]["nav_display_name"] == "present"
    assert payload["authentication"]["status"] == "not-established"
    assert payload["identity"]["status"] == "not-machine-verified"


@pytest.mark.parametrize(
    ("interruption", "error_type"),
    [
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (asyncio.CancelledError, "CancelledError"),
    ],
)
def test_success_serialization_cancellation_reports_saved_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
    error_type: str,
) -> None:
    output = tmp_path / "state.json"

    def succeed(*_args: Any, **kwargs: Any) -> Path:
        progress = kwargs["progress"]
        progress.confirmation_received = True
        progress.nav_display_name_present = True
        progress.saved_output = output
        progress.state_saved = True
        return output

    original_dumps = login_state.json.dumps

    def interrupt_success_payload(payload: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(payload, dict) and payload.get("ok") is True:
            raise interruption
        return original_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(login_state, "capture_login_state", succeed)
    monkeypatch.setattr(login_state.json, "dumps", interrupt_success_payload)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["error"] == "capture cancelled"
    assert payload["error_type"] == error_type
    assert payload["state"] == {"status": "candidate-saved"}
    assert payload["search_capability"]["status"] == "not-tested"


def test_writer_cancellation_propagates_cleanup_evidence_to_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("interactive-token-received"),
    )

    def interrupt_writer(*_args: Any, **_kwargs: Any) -> Path:
        error = KeyboardInterrupt()
        error.cleanup_failures = ["failed to remove the temporary credential file"]
        raise error

    monkeypatch.setattr(login_state, "_secure_write_json", interrupt_writer)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())
    output = tmp_path / "state.json"

    assert login_state.main(["--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["error"] == "capture cancelled"
    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["session"]["nav_display_name"] == "present"
    assert payload["authentication"]["status"] == "not-established"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to remove the temporary credential file"],
    }
    assert events[-2:] == ["browser-close", "playwright-stop"]


def test_writer_reconciliation_failure_reports_unknown_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    state = {
        "cookies": [
            {
                "name": "cookie2",
                "value": "candidate",
                "domain": ".goofish.com",
                "path": "/",
            }
        ],
        "origins": [],
    }
    _install_fake_browser(monkeypatch, events, state)
    monkeypatch.setattr(
        login_state,
        "_read_explicit_confirmation",
        lambda *_args, **_kwargs: events.append("interactive-token-received"),
    )

    def fail_reconciliation(*_args: Any, **_kwargs: Any) -> Path:
        error = KeyboardInterrupt()
        error.credential_state_status = "not-established"
        error.cleanup_failures = ["failed to determine credential publish status"]
        raise error

    monkeypatch.setattr(login_state, "_secure_write_json", fail_reconciliation)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())
    output = tmp_path / "state.json"

    assert login_state.main(["--output", str(output)]) == 130
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["state"] == {"status": "not-established"}
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["session"]["nav_display_name"] == "present"
    assert payload["authentication"]["status"] == "not-established"
    assert payload["cleanup"] == {
        "status": "failed",
        "errors": ["failed to determine credential publish status"],
    }


def test_failure_after_token_keeps_confirmation_evidence_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_after_token(*_args: Any, **kwargs: Any) -> Path:
        kwargs["progress"].confirmation_received = True
        raise ValueError("navigation display name was not observed")

    monkeypatch.setattr(login_state, "capture_login_state", fail_after_token)
    monkeypatch.setattr(login_state.sys, "stdin", InteractiveInput())

    assert login_state.main(["--output", str(tmp_path / "state.json")]) == 2
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert payload["state"]["status"] == "not-saved"
    assert payload["confirmation"]["status"] == "interactive-token-received"
    assert payload["session"]["nav_display_name"] == "not-established"
    assert payload["identity"]["status"] == "not-established"
