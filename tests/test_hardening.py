"""Hardening tests: no silent mock fallback, tool allowlist, key persistence, log locking, status page."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from decision_log import DecisionLog
from llm import (
    LLMAdapter,
    LLMConfig,
    OfflineDeterministicProvider,
    OllamaProvider,
    build_llm_safety_classifier,
)
from approval import OutboxService
from guardrails import PolicyCategory, PolicyEngine


def test_auto_mode_never_falls_back_to_offline_mock() -> None:
    adapter = LLMAdapter(config=LLMConfig(provider="auto", ollama_url="http://127.0.0.1:9"))
    assert isinstance(adapter.resolve_provider(), OllamaProvider)
    status = adapter.runtime_status()
    assert status["is_mock"] is False
    assert status["ready"] is False


def test_classifier_fails_closed_on_offline_synthesizer() -> None:
    adapter = LLMAdapter(config=LLMConfig(provider="offline"), provider=OfflineDeterministicProvider())
    engine = PolicyEngine(classifier=build_llm_safety_classifier(adapter))
    decision = engine.evaluate_text("A perfectly normal launch post.")
    assert decision.allowed is False
    assert decision.rule_id == "POL-900-CLASSIFIER-ERROR"


@pytest.mark.parametrize("tool", ["post_to_twitter", "send_mail", "stripe_charge", "http_request"])
def test_unknown_tools_are_blocked(tool: str) -> None:
    decision = PolicyEngine().evaluate_tool_call(tool, {"body": "hello"})
    assert decision.allowed is False
    assert decision.rule_id == "POL-009-UNKNOWN-TOOL"
    assert decision.category == PolicyCategory.UNAPPROVED_EXTERNAL_ACTION


def test_allowlisted_internal_tool_is_allowed() -> None:
    assert PolicyEngine().evaluate_tool_call("web_search", {"query": "student planners"}).allowed


def test_approval_token_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_APPROVAL_SECRET", raising=False)
    db = tmp_path / "data" / "foundry.sqlite3"
    first = OutboxService(db, export_dir=tmp_path / "outputs")
    action = first.propose_action(action_type="post", target="forum", title="Launch", body="Hello")
    token = first.approve_action(action.id)

    restarted = OutboxService(db, export_dir=tmp_path / "outputs")
    receipt = restarted.execute_action(action.id, token.encode())
    assert receipt.action_id == action.id


def test_concurrent_appends_keep_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "decision_log.json"
    DecisionLog(path)

    def worker(n: int) -> None:
        log = DecisionLog(path)
        for i in range(15):
            log.append(action=f"w{n}_{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = DecisionLog(path).verify()
    assert result.valid is True, result.errors
    assert result.entry_count == 90


def test_status_page_renders_and_is_honest() -> None:
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "STUBBED" in page.text
    data = client.get("/api/status").json()
    assert data["redteam"]["passed"] == data["redteam"]["total"]
    assert data["decision_log"]["valid"] is True
