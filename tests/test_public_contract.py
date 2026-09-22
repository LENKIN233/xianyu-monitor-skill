from __future__ import annotations

import demo
import version_info
import xianyu

EXPECTED_COMMANDS = {
    "analyze",
    "deliver",
    "demo",
    "doctor",
    "evaluate",
    "install",
    "login",
    "monitor",
    "search",
    "setup",
    "state",
    "task",
    "version",
}

EXPECTED_CONTRACTS = {
    "task_schema": 3,
    "task_transfer_schema": 1,
    "outbox_schema": 1,
    "delivery_schema": 1,
    "ai_run_evidence_schema": 1,
    "ai_evaluation_schema": 1,
    "ai_feedback_schema": 1,
    "demo_schema": 1,
    "version_schema": 1,
}


def test_release_candidate_command_surface_is_explicitly_frozen() -> None:
    assert set(xianyu.COMMANDS) == EXPECTED_COMMANDS
    assert {item["command"] for item in version_info.CAPABILITIES} == EXPECTED_COMMANDS


def test_release_candidate_discovery_contract_is_explicitly_frozen() -> None:
    payload = version_info.capability_payload()

    assert set(payload) == {
        "ok",
        "schema_version",
        "skill",
        "runtime",
        "capabilities",
        "contracts",
    }
    assert payload["contracts"] == EXPECTED_CONTRACTS
    assert all(
        set(capability) == {"id", "command", "network", "credentials", "status"}
        for capability in payload["capabilities"]
    )


def test_release_candidate_demo_contract_is_explicitly_frozen() -> None:
    payload = demo.build_demo()

    assert set(payload) == {
        "ok",
        "schema_version",
        "demo",
        "search",
        "analysis",
        "evaluation",
        "outbox",
        "delivery_preview",
        "next_action",
    }
    assert set(payload["demo"]) == {
        "status",
        "network",
        "credentials",
        "local_writes",
        "claims_real_xianyu_state",
    }
    assert set(payload["next_action"]) == {"code", "hint"}
