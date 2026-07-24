from __future__ import annotations

from login_state import _is_authenticated


def test_login_detection_requires_nonempty_cookie2() -> None:
    assert _is_authenticated([{"name": "cookie2", "value": "present"}]) is True
    assert _is_authenticated([{"name": "cookie2", "value": ""}]) is False
    assert _is_authenticated([{"name": "other", "value": "present"}]) is False
