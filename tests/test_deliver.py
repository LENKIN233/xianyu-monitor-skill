from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import deliver
import pytest
from task_manager import TaskManager


def _seed_outbox(path: Path) -> dict[str, Any]:
    manager = TaskManager(path, allow_missing=True)
    task = manager.create_task("MacBook Air", criteria="16GB")
    manager.record_run(
        task["id"],
        [
            {
                "id": "item 1",
                "title": "MacBook Air M2 16GB",
                "price": 4999,
                "location": "上海",
                "seller": "must-not-deliver",
                "image": "https://private.example/image.jpg",
            }
        ],
    )
    return manager.list_outbox()[0]


def _preview_digest(path: Path, endpoint: str, adapter: str = "webhook") -> str:
    events = TaskManager(path).list_outbox(limit=20)
    return deliver.build_delivery_preview(adapter, endpoint, events)["approval"][
        "preview_sha256"
    ]


def test_preview_performs_no_network_and_redacts_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    _seed_outbox(task_file)
    endpoint = "https://hooks.example/secret-token?key=private"
    monkeypatch.setenv("XIANYU_WEBHOOK_URL", endpoint)

    def reject_send(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("preview must not send")

    monkeypatch.setattr(deliver, "send_delivery", reject_send)
    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "webhook",
                "--preview",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert endpoint not in output
    assert "private" not in output
    assert report["endpoint"]["configured"] is True
    assert report["event_count"] == 1
    body = report["requests"][0]["body"]
    assert body["idempotency_key"] == report["requests"][0]["idempotency_key"]
    assert "must-not-deliver" not in json.dumps(body)


def test_digest_mismatch_fails_before_network_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    event = _seed_outbox(task_file)
    monkeypatch.setenv("XIANYU_WEBHOOK_URL", "https://hooks.example/secret")
    monkeypatch.setattr(
        deliver,
        "send_delivery",
        lambda *_args, **_kwargs: pytest.fail("must not send"),
    )

    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "webhook",
                "--send",
                "--expected-preview-sha256",
                "0" * 64,
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)

    assert report["external_send"] == {"status": "not-attempted"}
    assert (
        TaskManager(task_file).list_outbox()[0]["idempotency_key"]
        == event["idempotency_key"]
    )


def test_confirmed_delivery_acknowledges_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    event = _seed_outbox(task_file)
    endpoint = "https://hooks.example/secret"
    monkeypatch.setenv("XIANYU_WEBHOOK_URL", endpoint)
    observed: list[str] = []

    def confirm(
        _adapter: str,
        _endpoint: str,
        current: dict[str, Any],
        *,
        timeout: int,
        progress: deliver.DeliveryProgress,
    ) -> None:
        assert timeout == 30
        progress.mark_attempt_started()
        progress.mark_response_received()
        observed.append(current["idempotency_key"])

    monkeypatch.setattr(deliver, "send_delivery", confirm)
    digest = _preview_digest(task_file, endpoint)
    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "webhook",
                "--send",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert observed == [event["idempotency_key"]]
    assert report["delivered_count"] == 1
    assert report["delivered"][0]["task_commit_status"] == "recorded"
    assert TaskManager(task_file).list_outbox() == []


@pytest.mark.parametrize(
    ("adapter", "response", "accepted"),
    [
        ("bark", {"code": 200}, True),
        ("bark", {"code": 400}, False),
        ("bark", {"code": 200.0}, False),
        ("bark", {"code": "200"}, False),
        ("wecom", {"errcode": 0}, True),
        ("wecom", {"errcode": 93000}, False),
        ("wecom", {"errcode": False}, False),
        ("wecom", {"errcode": 0.0}, False),
        ("wecom", {"errcode": "0"}, False),
        ("wecom", {}, False),
    ],
)
def test_provider_confirmation_contracts(
    adapter: str,
    response: dict[str, Any],
    accepted: bool,
) -> None:
    encoded = json.dumps(response).encode()
    if accepted:
        deliver._provider_confirms(adapter, encoded)
    else:
        with pytest.raises(deliver.DeliveryResponseError, match="did not confirm"):
            deliver._provider_confirms(adapter, encoded)


def test_wecom_uses_plain_text_for_untrusted_listing_content() -> None:
    event = {
        "idempotency_key": "a" * 64,
        "payload": {
            "task_id": "task_1",
            "keyword": "keyword",
            "criteria": "",
            "item": {
                "id": "1",
                "title": "[untrusted](https://evil.example)",
                "url": "https://www.goofish.com/item?id=1",
            },
        },
    }

    body = deliver.build_delivery_request("wecom", event)

    assert body["msgtype"] == "text"
    assert set(body) == {"msgtype", "text"}
    assert "https://www.goofish.com/item?id=1" in body["text"]["content"]


def test_wecom_bounds_utf8_bytes_and_preserves_link_and_event_key() -> None:
    url = "https://www.goofish.com/item?id=1"
    key = "a" * 64
    event = {
        "idempotency_key": key,
        "payload": {
            "keyword": "🧭" * 100,
            "item": {
                "title": "📱" * 300,
                "price": 4999,
                "location": "📍" * 100,
                "url": url,
            },
        },
    }

    content = deliver.build_delivery_request("wecom", event)["text"]["content"]

    assert len(content.encode("utf-8")) <= 2048
    assert "…" in content
    assert "\ufffd" not in content
    assert content.endswith(f"\n{url}\n事件：{key}")


@pytest.mark.parametrize("adapter", ["bark", "wecom"])
def test_notification_rejects_oversized_link_instead_of_truncating_it(
    adapter: str,
) -> None:
    event = {
        "idempotency_key": "a" * 64,
        "payload": {"item": {"url": "https://www.goofish.com/item?id=" + "1" * 2100}},
    }

    with pytest.raises(deliver.DeliveryConfigurationError, match="URL"):
        deliver.build_delivery_request(adapter, event)


def test_send_uses_idempotency_header_and_never_exposes_response() -> None:
    event = {
        "idempotency_key": "a" * 64,
        "payload": {
            "task_id": "task_1",
            "keyword": "iPhone",
            "criteria": "",
            "item": {
                "id": "1",
                "title": "title",
                "url": "https://www.goofish.com/item?id=1",
            },
        },
    }
    observed: dict[str, Any] = {}

    def reject(request: Any, *, timeout: int) -> Any:
        observed["key"] = request.get_header("Idempotency-key")
        observed["timeout"] = timeout
        raise HTTPError(
            request.full_url,
            500,
            "bad",
            {},
            io.BytesIO(b"private-provider-response"),
        )

    with pytest.raises(deliver.DeliveryRequestError, match="HTTP 500") as captured:
        deliver.send_delivery(
            "webhook",
            "https://hooks.example/secret",
            event,
            timeout=10,
            opener=reject,
        )

    assert observed == {"key": "a" * 64, "timeout": 10}
    assert "private-provider-response" not in str(captured.value)


def test_unconfirmed_provider_response_does_not_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    event = _seed_outbox(task_file)
    endpoint = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
    monkeypatch.setenv("XIANYU_WECOM_WEBHOOK_URL", endpoint)

    def unconfirmed(
        *_args: Any, progress: deliver.DeliveryProgress, **_kwargs: Any
    ) -> None:
        progress.mark_attempt_started()
        progress.mark_response_received()
        raise deliver.DeliveryResponseError("wecom did not confirm delivery success")

    monkeypatch.setattr(deliver, "send_delivery", unconfirmed)
    digest = _preview_digest(task_file, endpoint, "wecom")
    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "wecom",
                "--send",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)

    assert report["external_send"] == {"status": "completed"}
    assert report["possible_duplicate"] is True
    assert (
        TaskManager(task_file).list_outbox()[0]["idempotency_key"]
        == event["idempotency_key"]
    )


def test_endpoint_must_be_https_and_is_never_echoed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    _seed_outbox(task_file)
    endpoint = "http://private.example/secret"
    monkeypatch.setenv("XIANYU_BARK_URL", endpoint)

    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "bark",
                "--preview",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert endpoint not in output
    assert "secret" not in output
    assert json.loads(output)["external_send"] == {"status": "not-attempted"}


def test_endpoint_rejects_invalid_hostname_before_send() -> None:
    with pytest.raises(deliver.DeliveryConfigurationError, match="hostname"):
        deliver._endpoint_from_environment(
            "webhook",
            environment={"XIANYU_WEBHOOK_URL": "https://not valid.example/secret"},
        )


def test_webhook_request_body_has_a_hard_size_limit() -> None:
    event = {
        "idempotency_key": "a" * 64,
        "payload": {
            "task_id": "task_1",
            "keyword": "x",
            "criteria": "",
            "item": {"id": "1", "title": "x" * deliver.MAX_REQUEST_BYTES},
        },
    }

    with pytest.raises(deliver.DeliveryConfigurationError, match="256 KiB"):
        deliver.build_delivery_request("webhook", event)


def test_redirect_handler_never_follows_provider_redirect() -> None:
    handler = deliver._NoRedirectHandler()
    request = Request(
        "https://approved.example/secret",
        headers={"Idempotency-Key": "a" * 64},
    )

    assert (
        handler.redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            {},
            "https://unapproved.example/collect",
        )
        is None
    )


def test_confirmed_send_with_missing_ack_reports_duplicate_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "tasks.json"
    event = _seed_outbox(task_file)
    endpoint = "https://hooks.example/secret"
    monkeypatch.setenv("XIANYU_WEBHOOK_URL", endpoint)

    def confirm(
        *_args: Any,
        progress: deliver.DeliveryProgress,
        **_kwargs: Any,
    ) -> None:
        progress.mark_attempt_started()
        progress.mark_response_received()

    monkeypatch.setattr(deliver, "send_delivery", confirm)
    monkeypatch.setattr(
        deliver.TaskManager,
        "acknowledge_outbox",
        lambda *_args, **_kwargs: False,
    )

    assert (
        deliver.main(
            [
                "--data-file",
                str(task_file),
                "--adapter",
                "webhook",
                "--send",
                "--expected-preview-sha256",
                _preview_digest(task_file, endpoint),
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)

    assert report["current_idempotency_key"] == event["idempotency_key"]
    assert report["external_send"] == {"status": "completed"}
    assert report["task_commit_status"] == "not-recorded"
    assert report["possible_duplicate"] is True
    assert (
        TaskManager(task_file).list_outbox()[0]["idempotency_key"]
        == event["idempotency_key"]
    )


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("prior_delivery", [False, True])
def test_ambiguous_send_retains_event_and_reports_duplicate_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cancelled: bool,
    prior_delivery: bool,
) -> None:
    task_file = tmp_path / "tasks.json"
    event = _seed_outbox(task_file)
    if prior_delivery:
        manager = TaskManager(task_file)
        manager.record_run(event["task_id"], [{"id": "item 2", "title": "second"}])
    events = TaskManager(task_file).list_outbox()
    endpoint = "https://hooks.example/secret"
    monkeypatch.setenv("XIANYU_WEBHOOK_URL", endpoint)
    attempts = 0

    class AmbiguousTransport:
        def open(self, _request: Request, *, timeout: int) -> io.BytesIO:
            nonlocal attempts
            attempts += 1
            assert timeout == 30
            if prior_delivery and attempts == 1:
                return io.BytesIO(b"")
            if cancelled:
                raise KeyboardInterrupt
            raise TimeoutError("response lost after the request may have arrived")

    monkeypatch.setattr(deliver, "build_opener", lambda *_args: AmbiguousTransport())
    result = deliver.main(
        [
            "--data-file",
            str(task_file),
            "--adapter",
            "webhook",
            "--send",
            "--expected-preview-sha256",
            _preview_digest(task_file, endpoint),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert result == (130 if cancelled else 2)
    assert attempts == len(events)
    assert report["external_send"] == {"status": "not-established"}
    assert report["possible_duplicate"] is True
    assert report["task_commit_status"] == "not-attempted"
    assert report["current_idempotency_key"] == events[-1]["idempotency_key"]
    assert len(report["delivered"]) == int(prior_delivery)
    assert TaskManager(task_file).list_outbox() == [events[-1]]
