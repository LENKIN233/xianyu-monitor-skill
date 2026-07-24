from __future__ import annotations

import stat
from pathlib import Path

import pytest
from create_state import create_storage_state, parse_cookie_string


def test_parse_cookie_preserves_equals_in_value() -> None:
    cookies = parse_cookie_string("cookie2=secret; token=a=b")
    assert [(cookie["name"], cookie["value"]) for cookie in cookies] == [
        ("cookie2", "secret"),
        ("token", "a=b"),
    ]


def test_create_state_uses_private_permissions(tmp_path: Path) -> None:
    output = create_storage_state("cookie2=secret", str(tmp_path / "state.json"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_create_state_does_not_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "state.json"
    create_storage_state("cookie2=first", str(output))
    with pytest.raises(FileExistsError):
        create_storage_state("cookie2=second", str(output))
    create_storage_state("cookie2=second", str(output), force=True)
    assert "second" in output.read_text(encoding="utf-8")
