#!/usr/bin/env python3
"""Deliver durable outbox events through explicitly configured HTTPS adapters."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .cli_contract import JsonArgumentParser, sigterm_cancellable
    from .task_manager import (
        TASK_COMMIT_STATUSES,
        TaskManager,
        TaskMutationProgress,
    )
else:
    from cli_contract import JsonArgumentParser, sigterm_cancellable
    from task_manager import TASK_COMMIT_STATUSES, TaskManager, TaskMutationProgress

DELIVERY_SCHEMA_VERSION = 1
PREVIEW_DIGEST_VERSION = "xianyu-delivery-preview-v1"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 100
MAX_WECOM_TEXT_BYTES = 2_048
_PREVIEW_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

ADAPTER_ENVIRONMENT = {
    "webhook": "XIANYU_WEBHOOK_URL",
    "bark": "XIANYU_BARK_URL",
    "wecom": "XIANYU_WECOM_WEBHOOK_URL",
}


class DeliveryConfigurationError(ValueError):
    """Delivery configuration is incomplete or unsafe."""


class DeliveryRequestError(RuntimeError):
    """A delivery attempt failed without exposing provider response content."""

    def __init__(self, message: str, *, external_send_status: str):
        super().__init__(message)
        self.external_send_status = external_send_status


class DeliveryResponseError(RuntimeError):
    """The provider returned a response that did not prove delivery success."""


@dataclass
class DeliveryProgress:
    """Conservative evidence for the current external-send boundary."""

    external_send_status: str = "not-attempted"

    def mark_attempt_started(self) -> None:
        self.external_send_status = "not-established"

    def mark_response_received(self) -> None:
        self.external_send_status = "completed"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _preview_digest(value: str) -> str:
    normalized = value.strip().lower()
    if _PREVIEW_SHA256.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(
            "expected preview SHA-256 must contain 64 hexadecimal characters"
        )
    return normalized


def _endpoint_from_environment(
    adapter: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    source = os.environ if environment is None else environment
    variable = ADAPTER_ENVIRONMENT[adapter]
    raw = source.get(variable, "")
    if not isinstance(raw, str) or not raw.strip():
        raise DeliveryConfigurationError(f"{variable} is required for {adapter}")
    endpoint = raw.strip()
    if (
        len(endpoint) > 8_192
        or not endpoint.isascii()
        or "\\" in endpoint
        or any(ord(character) < 32 or ord(character) == 127 for character in endpoint)
    ):
        raise DeliveryConfigurationError(f"{variable} is invalid")
    try:
        parsed = urlsplit(endpoint)
        parsed.port
    except ValueError as exc:
        raise DeliveryConfigurationError(f"{variable} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DeliveryConfigurationError(
            f"{variable} must be an HTTPS URL without credentials or fragment"
        )
    hostname = parsed.hostname
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.rstrip(".").split(".")
        if (
            not labels
            or len(hostname) > 253
            or any(_HOST_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise DeliveryConfigurationError(f"{variable} has an invalid hostname")
    return endpoint, variable


def _bounded_text(value: Any, maximum: int) -> str:
    return str(value)[:maximum] if value is not None else ""


def _notification_text(event: Mapping[str, Any]) -> tuple[str, str, str]:
    payload = event["payload"]
    item = payload["item"]
    keyword = _bounded_text(payload.get("keyword"), 100)
    title = _bounded_text(item.get("title"), 300)
    price = item.get("price")
    location = _bounded_text(item.get("location"), 100)
    url = str(item.get("url") or "")
    if len(url) > 2_000:
        raise DeliveryConfigurationError("notification item URL exceeds the limit")
    headline = _bounded_text(f"闲鱼上新：{keyword}", 200)
    lines = [title]
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        lines.append(f"价格：{price}")
    if location:
        lines.append(f"地点：{location}")
    return headline, "\n".join(lines), url


def _wecom_content(title: str, content: str, footer: str) -> str:
    suffix = f"\n{footer}"
    available = MAX_WECOM_TEXT_BYTES - len(suffix.encode("utf-8"))
    marker = "…"
    if available < len(marker.encode("utf-8")):
        raise DeliveryConfigurationError("notification item URL exceeds WeCom limit")
    prefix = f"{title}\n{content}".encode()
    if len(prefix) > available:
        prefix = prefix[: available - len(marker.encode("utf-8"))]
        summary = prefix.decode("utf-8", errors="ignore") + marker
    else:
        summary = prefix.decode("utf-8")
    return summary + suffix


def build_delivery_request(adapter: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Build one bounded provider body from an already validated outbox event."""

    key = str(event["idempotency_key"])
    if adapter == "webhook":
        body: dict[str, Any] = {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "idempotency_key": key,
            "event": copy.deepcopy(dict(event)),
        }
    else:
        title, content, url = _notification_text(event)
        footer = f"{url}\n事件：{key}"
        if adapter == "bark":
            body = {
                "title": title,
                "body": f"{content}\n{footer}",
                "url": url,
                "group": "xianyu-monitor",
            }
        elif adapter == "wecom":
            body = {
                "msgtype": "text",
                "text": {"content": _wecom_content(title, content, footer)},
            }
        else:
            raise DeliveryConfigurationError("unsupported delivery adapter")
    if len(_canonical_json_bytes(body)) > MAX_REQUEST_BYTES:
        raise DeliveryConfigurationError("delivery request exceeds the 256 KiB limit")
    return body


def build_delivery_preview(
    adapter: str,
    endpoint: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    requests = [
        {
            "idempotency_key": event["idempotency_key"],
            "body": build_delivery_request(adapter, event),
        }
        for event in events
    ]
    endpoint_sha256 = hashlib.sha256(endpoint.encode("ascii")).hexdigest()
    approved = {
        "version": PREVIEW_DIGEST_VERSION,
        "adapter": adapter,
        "endpoint_sha256": endpoint_sha256,
        "requests": requests,
    }
    return {
        "adapter": adapter,
        "endpoint": {"configured": True, "sha256": endpoint_sha256},
        "event_count": len(requests),
        "requests": requests,
        "approval": {
            "version": PREVIEW_DIGEST_VERSION,
            "preview_sha256": _sha256(approved),
            "binds": ["adapter", "endpoint_sha256", "requests"],
        },
    }


def _open_without_redirects(request: Request, *, timeout: int) -> Any:
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def _provider_confirms(adapter: str, raw_response: bytes) -> None:
    if adapter == "webhook" and not raw_response.strip():
        return
    try:
        payload = json.loads(raw_response.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        if adapter == "webhook":
            return
        raise DeliveryResponseError(
            f"{adapter} returned an invalid confirmation response"
        ) from exc
    if adapter == "webhook":
        return
    if not isinstance(payload, dict):
        raise DeliveryResponseError(
            f"{adapter} returned an invalid confirmation response"
        )
    code = payload.get("code" if adapter == "bark" else "errcode")
    confirmed = (
        isinstance(code, int)
        and not isinstance(code, bool)
        and code == (200 if adapter == "bark" else 0)
    )
    if not confirmed:
        raise DeliveryResponseError(f"{adapter} did not confirm delivery success")


def send_delivery(
    adapter: str,
    endpoint: str,
    event: Mapping[str, Any],
    *,
    timeout: int,
    opener: Callable[..., Any] = _open_without_redirects,
    progress: DeliveryProgress | None = None,
) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 120
    ):
        raise DeliveryConfigurationError("timeout must be between 1 and 120 seconds")
    request_progress = progress if progress is not None else DeliveryProgress()
    body = build_delivery_request(adapter, event)
    request = Request(  # noqa: S310 - endpoint validation requires HTTPS.
        endpoint,
        data=_canonical_json_bytes(body),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(event["idempotency_key"]),
            "User-Agent": "xianyu-monitor-delivery/1",
        },
        method="POST",
    )
    request_progress.mark_attempt_started()
    try:
        response = opener(request, timeout=timeout)
    except HTTPError as exc:
        request_progress.mark_response_received()
        exc.close()
        raise DeliveryRequestError(
            f"delivery failed with HTTP {exc.code}",
            external_send_status="completed",
        ) from exc
    except (HTTPException, OSError, URLError, TimeoutError) as exc:
        raise DeliveryRequestError(
            "delivery failed before a valid response",
            external_send_status="not-established",
        ) from exc
    request_progress.mark_response_received()
    try:
        with response:
            raw_response = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPException, OSError, URLError, TimeoutError) as exc:
        raise DeliveryRequestError(
            "delivery response failed after the provider received the request",
            external_send_status="completed",
        ) from exc
    if len(raw_response) > MAX_RESPONSE_BYTES:
        raise DeliveryResponseError("delivery response exceeds the 1 MiB safety limit")
    _provider_confirms(adapter, raw_response)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Preview or send durable outbox events through an HTTPS adapter"
    )
    parser.add_argument("--data-file", default="tasks.json", help="task JSON path")
    parser.add_argument("--adapter", choices=tuple(ADAPTER_ENVIRONMENT), required=True)
    parser.add_argument("--task-id")
    parser.add_argument(
        "--limit", type=int, default=20, help="events per batch (default: 20; max: 100)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="request timeout (default: 30; max: 120)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--send", action="store_true")
    parser.add_argument("--expected-preview-sha256", type=_preview_digest)
    return parser


def _failure_report(
    exc: BaseException,
    *,
    adapter: str,
    delivered: list[dict[str, Any]],
    current_key: str | None,
    send_progress: DeliveryProgress,
    mutation_progress: TaskMutationProgress | None,
) -> dict[str, Any]:
    send_status = getattr(exc, "external_send_status", None)
    if send_status not in {"not-attempted", "not-established", "completed"}:
        send_status = send_progress.external_send_status
    task_status = getattr(exc, "task_commit_status", None)
    if task_status not in TASK_COMMIT_STATUSES and mutation_progress is not None:
        task_status = mutation_progress.task_commit_status
    if task_status not in TASK_COMMIT_STATUSES:
        task_status = "not-attempted"
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "adapter": adapter,
        "current_idempotency_key": current_key,
        "delivered": delivered,
        "external_send": {"status": send_status},
        "task_commit_status": task_status,
        "possible_duplicate": send_status != "not-attempted"
        and task_status != "recorded",
    }


@sigterm_cancellable
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    delivered: list[dict[str, Any]] = []
    current_key: str | None = None
    send_progress = DeliveryProgress()
    mutation_progress: TaskMutationProgress | None = None
    try:
        if isinstance(args.limit, bool) or not 1 <= args.limit <= MAX_BATCH_EVENTS:
            raise DeliveryConfigurationError(  # noqa: TRY301
                "limit must be between 1 and 100"
            )
        if (
            isinstance(args.timeout, bool)
            or not isinstance(args.timeout, int)
            or not 1 <= args.timeout <= 120
        ):
            raise DeliveryConfigurationError(  # noqa: TRY301
                "timeout must be between 1 and 120 seconds"
            )
        endpoint, environment_name = _endpoint_from_environment(args.adapter)
        manager = TaskManager(args.data_file)
        events = manager.list_outbox(limit=args.limit, task_id=args.task_id)
        preview = build_delivery_preview(args.adapter, endpoint, events)
        if args.preview:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "mode": "preview",
                        "external_send": {"status": "not-attempted"},
                        "credential": {
                            "source": "environment",
                            "name": environment_name,
                            "value_exposed": False,
                        },
                        **preview,
                    },
                    ensure_ascii=True,
                    indent=2,
                    allow_nan=False,
                )
            )
            return 0
        expected = args.expected_preview_sha256
        if expected is None:
            raise DeliveryConfigurationError(  # noqa: TRY301
                "delivery --send requires --expected-preview-sha256 from preview"
            )
        if not hmac.compare_digest(expected, preview["approval"]["preview_sha256"]):
            raise DeliveryConfigurationError(  # noqa: TRY301
                "preview SHA-256 does not match the current endpoint and "
                "outbox requests"
            )

        for event in events:
            current_key = event["idempotency_key"]
            send_progress = DeliveryProgress()
            send_delivery(
                args.adapter,
                endpoint,
                event,
                timeout=args.timeout,
                progress=send_progress,
            )
            mutation_progress = TaskMutationProgress()
            acknowledged = manager.acknowledge_outbox(
                current_key,
                progress=mutation_progress,
            )
            if not acknowledged:
                error = RuntimeError(
                    "provider confirmed delivery but local acknowledgement was not "
                    "recorded"
                )
                setattr(error, "external_send_status", "completed")
                raise error
            delivered.append(
                {
                    "idempotency_key": current_key,
                    "external_send": {"status": "completed"},
                    "task_commit_status": mutation_progress.task_commit_status,
                }
            )
            current_key = None
            mutation_progress = None

        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "send",
                    "adapter": args.adapter,
                    "approval": preview["approval"],
                    "requested_count": len(events),
                    "delivered_count": len(delivered),
                    "delivered": delivered,
                    "external_send": {
                        "status": "completed" if events else "not-attempted"
                    },
                },
                ensure_ascii=True,
                indent=2,
                allow_nan=False,
            )
        )
        return 0  # noqa: TRY300 - success stays inside cancellation boundary.
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        report = _failure_report(
            exc,
            adapter=args.adapter,
            delivered=delivered,
            current_key=current_key,
            send_progress=send_progress,
            mutation_progress=mutation_progress,
        )
        report["error"] = "delivery cancelled"
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 130
    except (
        DeliveryConfigurationError,
        DeliveryRequestError,
        DeliveryResponseError,
        KeyError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                _failure_report(
                    exc,
                    adapter=args.adapter,
                    delivered=delivered,
                    current_key=current_key,
                    send_progress=send_progress,
                    mutation_progress=mutation_progress,
                ),
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
