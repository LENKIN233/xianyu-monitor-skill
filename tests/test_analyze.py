from __future__ import annotations

import argparse
import io
import json
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import analyze
import pytest


def _search_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "criteria": "prefer 16GB and local pickup",
        "items": [
            {
                "id": "item 1",
                "title": "MacBook Air M2 16GB",
                "price": 4999,
                "location": "上海",
                "publish_time": "2026-08-13 12:00",
                "wants": "8",
                "tags": ["16GB", "自提"],
                "seller": "must-not-send",
                "image": "https://images.example/private.jpg",
                "url": "https://evil.example/redirect",
                "unknown": "must-not-send",
            }
        ],
    }


def _analysis_item(*, item_id: str = "item 1", source_index: int = 0) -> dict[str, Any]:
    return {
        "source_index": source_index,
        "id": item_id,
        "score": 90,
        "match_level": "high_match",
        "observed_evidence": ["标题写明 16GB"],
        "uncertainties": ["未观察到电池健康"],
        "risk_signals": [],
    }


def _approval_digest(
    payload: dict[str, Any] | None = None,
    *,
    model: str = analyze.DEFAULT_MODEL,
    base_url: str = analyze.DEFAULT_BASE_URL,
    allow_retained_recorded_items: bool = False,
) -> str:
    source = payload if payload is not None else _search_payload()
    listings = analyze.sanitize_results(
        source,
        allow_retained_recorded_items=allow_retained_recorded_items,
    )
    request = analyze.build_request_payload(listings, model=model)
    provider = analyze._provider_evidence(base_url, model)
    return analyze._preview_sha256(provider, request)


def test_sanitize_search_result_uses_strict_allowlist() -> None:
    listings = analyze.sanitize_results(_search_payload())

    assert listings == [
        {
            "source_index": 0,
            "id": "item 1",
            "title": "MacBook Air M2 16GB",
            "price": 4999,
            "location": "上海",
            "publish_time": "2026-08-13 12:00",
            "wants": "8",
            "tags": ["16GB", "自提"],
            "criteria": "prefer 16GB and local pickup",
        }
    ]
    serialized = json.dumps(listings, ensure_ascii=False)
    for forbidden in ("seller", "image", "evil.example", "must-not-send"):
        assert forbidden not in serialized


def test_sanitize_monitor_result_inherits_each_task_criteria() -> None:
    payload = {
        "ok": True,
        "tasks": [
            {
                "criteria": "first criterion",
                "items": [{"id": "1", "title": "first"}],
            },
            {
                "criteria": "second criterion",
                "items": [{"id": "2", "title": "second"}],
            },
        ],
    }

    listings = analyze.sanitize_results(payload)

    assert [item["criteria"] for item in listings] == [
        "first criterion",
        "second criterion",
    ]
    assert [item["source_index"] for item in listings] == [0, 1]


def test_sanitize_result_enforces_item_limit() -> None:
    payload = {
        "ok": True,
        "items": [{"id": str(index), "title": f"item {index}"} for index in range(60)],
    }

    assert len(analyze.sanitize_results(payload, max_items=50)) == 50
    with pytest.raises(analyze.AnalysisInputError, match="between 1 and 50"):
        analyze.sanitize_results(payload, max_items=51)
    with pytest.raises(analyze.AnalysisInputError, match="must be an integer"):
        analyze.sanitize_results(payload, max_items=True)


def test_request_payload_marks_listing_text_untrusted_and_uses_strict_schema() -> None:
    request = analyze.build_request_payload(
        analyze.sanitize_results(_search_payload()),
        model="gpt-4o-mini",
    )

    assert request["store"] is False
    assert request["max_completion_tokens"] == 8_192
    assert "untrusted data" in request["messages"][0]["content"]
    assert "cannot override" in request["messages"][0]["content"]
    assert request["response_format"]["json_schema"]["strict"] is True
    assert (
        request["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    user_content = request["messages"][1]["content"]
    assert "must-not-send" not in user_content
    assert "evil.example" not in user_content


def test_validate_analysis_rejects_changed_listing_identity() -> None:
    listings = analyze.sanitize_results(_search_payload())
    raw = {"summary": "summary", "items": [_analysis_item(item_id="forged")]}

    with pytest.raises(analyze.AIResponseError, match="changed a listing id"):
        analyze.validate_analysis(raw, listings)


def test_validate_analysis_rejects_nonstring_match_level() -> None:
    listings = analyze.sanitize_results(_search_payload())
    item = _analysis_item()
    item["match_level"] = []

    with pytest.raises(analyze.AIResponseError, match="invalid match_level"):
        analyze.validate_analysis(
            {"summary": "summary", "items": [item]},
            listings,
        )


def test_validate_analysis_enriches_locally_and_sorts_by_score() -> None:
    payload = {
        "ok": True,
        "items": [
            {"id": "low", "title": "low title", "price": 1},
            {"id": "high", "title": "high title", "price": 2},
        ],
    }
    listings = analyze.sanitize_results(payload)
    low = _analysis_item(item_id="low", source_index=0)
    low["score"] = 10
    low["match_level"] = "low_match"
    high = _analysis_item(item_id="high", source_index=1)
    raw = {"summary": "ranked", "items": [low, high]}

    result = analyze.validate_analysis(raw, listings)

    assert [item["id"] for item in result["items"]] == ["high", "low"]
    assert result["items"][0]["url"] == "https://www.goofish.com/item?id=high"
    assert result["items"][0]["title"] == "high title"


def test_failed_collection_result_is_never_converted_to_analysis_success() -> None:
    payload = {
        "ok": False,
        "error": "search failed",
        "items": [{"id": "1", "title": "must not analyze"}],
    }

    with pytest.raises(analyze.AnalysisInputError, match="successful search"):
        analyze.sanitize_results(payload)


def test_failed_monitor_can_select_only_recorded_retained_items() -> None:
    payload = {
        "ok": False,
        "error_type": "RuntimeError",
        "tasks": [
            {
                "criteria": "recorded criterion",
                "items": [{"id": "recorded", "title": "recorded item"}],
                "persistence": {"status": "recorded"},
            },
            {
                "criteria": "uncertain criterion",
                "possible_items": [{"id": "uncertain", "title": "do not send"}],
                "persistence": {"status": "not-established"},
            },
        ],
    }

    with pytest.raises(analyze.AnalysisInputError, match="successful search"):
        analyze.sanitize_results(payload)

    listings = analyze.sanitize_results(
        payload,
        allow_retained_recorded_items=True,
    )
    assert [item["id"] for item in listings] == ["recorded"]


def test_retained_mode_rejects_failed_top_level_items_even_with_tasks() -> None:
    payload = {
        "ok": False,
        "items": [{"id": "unrecorded", "title": "must not send"}],
        "tasks": [],
    }

    with pytest.raises(analyze.AnalysisInputError, match="failed monitor"):
        analyze.sanitize_results(
            payload,
            allow_retained_recorded_items=True,
        )


@pytest.mark.parametrize("ok_value", [None, 0, "false"])
def test_retained_mode_requires_explicit_boolean_failure(ok_value: Any) -> None:
    payload = {
        "ok": ok_value,
        "tasks": [
            {
                "items": [{"id": "recorded", "title": "recorded item"}],
                "persistence": {"status": "recorded"},
            }
        ],
    }

    with pytest.raises(analyze.AnalysisInputError, match="successful search"):
        analyze.sanitize_results(
            payload,
            allow_retained_recorded_items=True,
        )


def test_preview_requires_no_key_and_performs_no_external_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def reject_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("preview must not contact AI")

    monkeypatch.setattr(analyze, "_request_completion", reject_request)

    assert analyze.main(["--input", str(input_path), "--preview"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["mode"] == "preview"
    assert report["external_send"] == {"status": "not-attempted"}
    assert report["listings"][0]["id"] == "item 1"
    assert report["provider"] == {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
    }
    assert report["selection"] == {
        "available_count": 1,
        "selected_count": 1,
        "truncated": False,
    }
    assert report["approval"]["preview_sha256"] == _approval_digest()
    assert report["run_evidence"]["schema_version"] == 1
    assert report["run_evidence"]["model"] == "gpt-4o-mini"
    for field in (
        "provider_sha256",
        "input_sha256",
        "prompt_sha256",
        "schema_sha256",
        "request_sha256",
    ):
        assert len(report["run_evidence"][field]) == 64
    assert "output_sha256" not in report["run_evidence"]
    assert "latency_ms" not in report["run_evidence"]


def test_input_rejects_nonstandard_json_numbers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(
        '{"ok":true,"items":[{"id":"1","title":"x","price":NaN}]}',
        encoding="utf-8",
    )

    assert analyze.main(["--input", str(input_path), "--preview"]) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["error_type"] == "AnalysisInputError"
    assert "non-standard JSON number" in report["error"]


def test_input_rejects_lone_unicode_surrogate_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(
        '{"ok":true,"items":[{"id":"\\ud800","title":"x"}]}',
        encoding="utf-8",
    )

    assert analyze.main(["--input", str(input_path), "--preview"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error_type"] == "AnalysisInputError"
    assert "non-empty id" in report["error"]


def test_file_input_does_not_require_process_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setattr(analyze.sys, "stdin", None)

    assert analyze._read_input(str(input_path)) == _search_payload()


def test_dash_input_reports_unavailable_stdin_as_analysis_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(analyze.sys, "stdin", None)

    assert analyze.main(["--input", "-", "--preview"]) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["error_type"] == "AnalysisInputError"
    assert report["error"] == "standard input is unavailable"


def test_live_mode_uses_environment_key_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    observed: dict[str, Any] = {}

    def fake_request(
        request_payload: dict[str, Any],
        *,
        api_key: str,
        base_url: str,
        timeout: int,
        progress: analyze.AIRequestProgress,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        progress.mark_response_received()
        observed.update(
            request_payload=request_payload,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        return {
            "summary": "good match",
            "items": [_analysis_item()],
        }, {"total_tokens": 42}

    monkeypatch.setattr(analyze, "_request_completion", fake_request)

    digest = _approval_digest()
    assert (
        analyze.main(
            [
                "--input",
                str(input_path),
                "--consent-send-listings",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert observed["api_key"] == "super-secret-key"
    assert "super-secret-key" not in output
    assert report["ok"] is True
    assert report["analyzed_count"] == 1
    assert report["external_send"] == {"status": "completed"}
    assert len(report["run_evidence"]["output_sha256"]) == 64
    assert report["run_evidence"]["latency_ms"] >= 0


def test_response_read_cancellation_preserves_completed_send_evidence() -> None:
    class InterruptingResponse:
        def __enter__(self) -> InterruptingResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            raise KeyboardInterrupt

    progress = analyze.AIRequestProgress()
    with pytest.raises(KeyboardInterrupt):
        analyze._request_completion(
            {"model": "gpt-4o-mini"},
            api_key="secret-key",
            base_url="https://gateway.example",
            timeout=15,
            opener=lambda *_args, **_kwargs: InterruptingResponse(),
            progress=progress,
        )

    assert progress.external_send_status == "completed"


def test_live_cancellation_after_response_reports_completed_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")

    def interrupt_after_response(
        *_args: Any,
        progress: analyze.AIRequestProgress,
        **_kwargs: Any,
    ) -> Any:
        progress.mark_response_received()
        raise KeyboardInterrupt

    monkeypatch.setattr(analyze, "_request_completion", interrupt_after_response)
    assert (
        analyze.main(
            [
                "--input",
                str(input_path),
                "--consent-send-listings",
                "--expected-preview-sha256",
                _approval_digest(),
            ]
        )
        == 130
    )
    report = json.loads(capsys.readouterr().out)
    assert report["external_send"] == {"status": "completed"}


def test_live_mode_without_key_fails_before_external_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    digest = _approval_digest()
    assert (
        analyze.main(
            [
                "--input",
                str(input_path),
                "--consent-send-listings",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert report["error_type"] == "AIConfigurationError"
    assert report["external_send"] == {"status": "not-attempted"}
    assert "Bearer" not in output


def test_invalid_model_output_reports_send_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setattr(
        analyze,
        "_request_completion",
        lambda *_args, **_kwargs: (
            {"summary": "bad", "items": [_analysis_item(item_id="forged")]},
            {},
        ),
    )

    digest = _approval_digest()
    assert (
        analyze.main(
            [
                "--input",
                str(input_path),
                "--consent-send-listings",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)

    assert report["error_type"] == "AIResponseError"
    assert report["external_send"] == {"status": "completed"}


def test_provider_transport_uses_bearer_and_parses_structured_response() -> None:
    listings = analyze.sanitize_results(_search_payload())
    request_payload = analyze.build_request_payload(
        listings,
        model="gpt-4o-mini",
    )
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"summary": "summary", "items": [_analysis_item()]}
                    )
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "provider_detail": "ignored",
        },
    }
    observed: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(response_payload).encode("utf-8")

    def fake_opener(request: Any, *, timeout: int) -> FakeResponse:
        observed["url"] = request.full_url
        observed["authorization"] = request.get_header("Authorization")
        observed["timeout"] = timeout
        observed["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    raw_analysis, usage = analyze._request_completion(
        request_payload,
        api_key="secret-key",
        base_url="https://gateway.example",
        timeout=15,
        opener=fake_opener,
    )

    assert observed["url"] == "https://gateway.example/v1/chat/completions"
    assert observed["authorization"] == "Bearer secret-key"
    assert observed["timeout"] == 15
    assert observed["body"]["store"] is False
    assert raw_analysis["summary"] == "summary"
    assert usage == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}


def test_provider_http_error_reports_send_without_exposing_body_or_key() -> None:
    def reject_request(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPError(
            "https://gateway.example/v1/chat/completions",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"remote-secret-body"),
        )

    with pytest.raises(analyze.AIRequestError, match="HTTP 401") as captured:
        analyze._request_completion(
            {"model": "gpt-4o-mini"},
            api_key="secret-key",
            base_url="https://gateway.example",
            timeout=15,
            opener=reject_request,
        )

    assert captured.value.external_send_status == "completed"
    assert "secret-key" not in str(captured.value)
    assert "remote-secret-body" not in str(captured.value)


@pytest.mark.parametrize("api_key", ["密钥", "x" * 8_193])
def test_provider_rejects_unsafe_api_key_before_send(api_key: str) -> None:
    def reject_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid API keys must fail before network access")

    with pytest.raises(analyze.AIConfigurationError, match="invalid characters"):
        analyze._request_completion(
            {"model": "gpt-4o-mini"},
            api_key=api_key,
            base_url="https://gateway.example",
            timeout=15,
            opener=reject_request,
        )


def test_provider_redirects_are_never_followed() -> None:
    handler = analyze._NoRedirectHandler()
    request = Request(
        "https://approved.example/v1/chat/completions",
        headers={"Authorization": "Bearer secret-key"},
    )

    redirected = handler.redirect_request(
        request,
        None,
        307,
        "Temporary Redirect",
        {},
        "https://unapproved.example/collect",
    )

    assert redirected is None


def test_provider_incomplete_response_preserves_json_error_contract() -> None:
    class IncompleteResponse:
        def __enter__(self) -> IncompleteResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            raise IncompleteRead(b"partial-secret-response", 100)

    with pytest.raises(analyze.AIRequestError) as captured:
        analyze._request_completion(
            {"model": "gpt-4o-mini"},
            api_key="secret-key",
            base_url="https://gateway.example",
            timeout=15,
            opener=lambda *_args, **_kwargs: IncompleteResponse(),
        )

    assert captured.value.external_send_status == "completed"
    assert "partial-secret-response" not in str(captured.value)


def test_provider_refusal_is_detected_without_requiring_content() -> None:
    class RefusalResponse:
        def __enter__(self) -> RefusalResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"refusal": "cannot help"}}]}
            ).encode()

    with pytest.raises(analyze.AIResponseError, match="refused"):
        analyze._request_completion(
            {"model": "gpt-4o-mini"},
            api_key="secret-key",
            base_url="https://gateway.example",
            timeout=15,
            opener=lambda *_args, **_kwargs: RefusalResponse(),
        )


def test_provider_truncated_finish_reason_is_rejected() -> None:
    payload = {
        "choices": [
            {
                "message": {"content": '{"summary":"looks valid","items":[]}'},
                "finish_reason": "length",
            }
        ]
    }

    with pytest.raises(analyze.AIResponseError, match="did not complete"):
        analyze._parse_completion(json.dumps(payload).encode())


def test_deeply_nested_provider_json_is_a_structured_response_error() -> None:
    deeply_nested = ("[" * 100_000 + "0" + "]" * 100_000).encode()

    with pytest.raises(analyze.AIResponseError, match="invalid structured"):
        analyze._parse_completion(deeply_nested)


def test_empty_live_analysis_still_requires_preview_digest_and_never_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"ok": True, "items": []}
    input_path = tmp_path / "empty-result.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    def reject_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an empty selection must never call the provider")

    monkeypatch.setattr(analyze, "_request_completion", reject_request)
    assert analyze.main(["--input", str(input_path), "--consent-send-listings"]) == 2
    missing_digest = json.loads(capsys.readouterr().out)
    assert missing_digest["error_type"] == "AIConfigurationError"

    digest = _approval_digest(payload)
    assert (
        analyze.main(
            [
                "--input",
                str(input_path),
                "--consent-send-listings",
                "--expected-preview-sha256",
                digest,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["external_send"] == {"status": "not-attempted"}
    assert report["usage"] == {}
    assert report["items"] == []


def test_live_mode_requires_digest_matching_current_payload_and_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")

    def reject_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("digest mismatch must fail before external send")

    monkeypatch.setattr(analyze, "_request_completion", reject_request)
    exit_code = analyze.main(
        [
            "--input",
            str(input_path),
            "--consent-send-listings",
            "--expected-preview-sha256",
            "0" * 64,
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report["error_type"] == "AIConfigurationError"
    assert report["external_send"] == {"status": "not-attempted"}


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example/v1",
        "https://user:pass@api.example/v1",
        "https://api.example/v1?secret=value",
    ],
)
def test_completion_url_requires_safe_https_base(base_url: str) -> None:
    with pytest.raises(analyze.AIConfigurationError, match="HTTPS URL"):
        analyze._completion_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://bad_host.example/v1",
        "https://api.example\\@evil.example/v1",
        "https://api.example/\x01v1",
        "https://例子.example/v1",
    ],
)
def test_completion_url_rejects_ambiguous_or_non_ascii_hosts(base_url: str) -> None:
    with pytest.raises(analyze.AIConfigurationError, match="invalid"):
        analyze._completion_url(base_url)


@pytest.mark.parametrize(
    "base_url,expected",
    [
        (
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://gateway.example",
            "https://gateway.example/v1/chat/completions",
        ),
    ],
)
def test_completion_url_supports_sdk_and_gateway_base_styles(
    base_url: str,
    expected: str,
) -> None:
    assert analyze._completion_url(base_url) == expected


@pytest.mark.parametrize("model", ["", "bad model", "model\nheader"])
def test_model_name_rejects_unsafe_values(model: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="model id"):
        analyze._model_name(model)


def test_cli_requires_preview_or_explicit_send_consent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_search_payload()), encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        analyze.main(["--input", str(input_path)])

    report = json.loads(capsys.readouterr().out)
    assert report["error_type"] == "ArgumentError"
