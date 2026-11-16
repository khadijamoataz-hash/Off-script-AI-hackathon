"""Stage 3 unit & integration tests: LLM Adapter (retry/repair + classifier) and 6-Phase Orchestrator."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from decision_log import DecisionLog
from evidence import ClaimType, EvidenceLedger
from llm import (
    LLMAdapter,
    LLMConfig,
    LLMError,
    OfflineDeterministicProvider,
    build_llm_safety_classifier,
    extract_json_object,
)
from orchestrator import (
    GateOutcome,
    Orchestrator,
    OrchestratorError,
    PhaseName,
    ProjectStatus,
)
from approval import ActionType, OutboxService
from guardrails import PolicyCategory, PolicyEngine


class _SampleBrief(BaseModel):
    headline: str = Field(min_length=3)
    hypotheses: list[str] = Field(min_length=1)
    confidence_score: int = Field(ge=1, le=5)


def test_llm_adapter_repairs_malformed_json_and_validates_schema() -> None:
    # First output is broken JSON, second misses a required field, third is valid fenced JSON
    scripted = [
        "Sure! Here is the output: {headline: broken json",
        '{"headline": "Valid headline"}',
        '```json\n{"headline": "AUC Notion Study Pack", "hypotheses": ["Students track finals"], "confidence_score": 4}\n```',
    ]
    provider = OfflineDeterministicProvider(scripted_responses=scripted)
    adapter = LLMAdapter(
        config=LLMConfig(provider="offline", max_repair_retries=2),
        provider=provider,
    )

    resp = adapter.generate_structured(
        system_prompt="Return an idea brief.",
        user_prompt="Notion study pack for engineering students",
        response_model=_SampleBrief,
    )
    assert resp.attempts == 3
    assert resp.parsed.headline == "AUC Notion Study Pack"
    assert resp.parsed.confidence_score == 4
    assert resp.provider_used == "offline_deterministic"


def test_llm_adapter_raises_after_exhausting_repair_retries() -> None:
    provider = OfflineDeterministicProvider(
        scripted_responses=["not json 1", "still not json 2"]
    )
    adapter = LLMAdapter(
        config=LLMConfig(provider="offline", max_repair_retries=1),
        provider=provider,
    )
    with pytest.raises(LLMError, match="Structured output validation failed after 2 attempt"):
        adapter.generate_structured(
            system_prompt="Return brief",
            user_prompt="Idea",
            response_model=_SampleBrief,
        )

    assert extract_json_object("Preface {\"a\": 1} trailing") == {"a": 1}


def test_llm_safety_classifier_integrates_with_policy_engine(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    # Model classifier flags a subtle income promise that avoided exact keyword patterns
    scripted_unsafe = [
        '{"safe": false, "confident": true, "category": "INCOME_GUARANTEE", "reason": "Implied certainty of passive wealth"}'
    ]
    adapter = LLMAdapter(
        config=LLMConfig(provider="offline"),
        provider=OfflineDeterministicProvider(scripted_responses=scripted_unsafe),
    )
    engine = PolicyEngine(
        decision_log=log,
        classifier=build_llm_safety_classifier(adapter),
    )

    decision = engine.evaluate_text("Every single student who buys this effortlessly retires by May.")
    assert decision.allowed is False
    assert decision.category == PolicyCategory.INCOME_GUARANTEE
    assert log.verify().valid is True


def test_orchestrator_full_six_phase_lifecycle_and_gates(tmp_path: Path) -> None:
    db_path = tmp_path / "foundry.sqlite3"
    log = DecisionLog(tmp_path / "decision_log.json")
    ledger = EvidenceLedger(db_path)
    outbox = OutboxService(
        db_path,
        export_dir=tmp_path / "outputs",
        decision_log=log,
        signing_secret="stage3-secret",
    )
    orch = Orchestrator(
        db_path,
        evidence_ledger=ledger,
        decision_log=log,
        outbox=outbox,
    )

    proj = orch.create_project(
        name="Campus Notion Pack",
        idea_summary="Exam & project tracker templates for MENA engineering students",
        validate_evidence_target=3,
    )
    assert proj.current_phase == PhaseName.DISCOVER

    # Cannot advance Discover before saving artifact or without founder approval
    with pytest.raises(OrchestratorError, match="without explicit founder approval"):
        orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=False)

    c_hyp = ledger.add_claim(
        "Engineering students juggle 5+ lab deadlines weekly.",
        project_id=proj.id,
        linked_phase="discover",
    )
    orch.save_phase_artifact(
        proj.id,
        PhaseName.DISCOVER,
        {"problem": "Scattered deadlines", "evidence_ids": [c_hyp.id]},
    )
    proj = orch.decide_gate(
        proj.id,
        GateOutcome.ADVANCE,
        founder_approved=True,
        rationale="Brief looks sharp.",
        evidence_ids=[c_hyp.id],
    )
    assert proj.current_phase == PhaseName.VALIDATE

    # Phase 2 Validate: requires sourced FACT + combined evidence >= target (3)
    orch.save_phase_artifact(proj.id, PhaseName.VALIDATE, {"plan": "Interview 10 students"})
    with pytest.raises(OrchestratorError, match="at least 1 sourced FACT"):
        orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)

    fact = ledger.add_claim(
        "Notion student plans are free with university email addresses.",
        project_id=proj.id,
        linked_phase="validate",
        claim_type=ClaimType.FACT,
        source_url="https://www.notion.so/product/notion-for-education",
    )
    # 1 fact + 0 signals = 1 < 3 -> still blocked
    with pytest.raises(OrchestratorError, match="Evidence target not yet reached"):
        orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)

    orch.record_demand_signals(proj.id, 2, note="2 student pre-order interview opt-ins", evidence_ids=[fact.id])
    proj = orch.decide_gate(
        proj.id, GateOutcome.ADVANCE, founder_approved=True, evidence_ids=[fact.id]
    )
    assert proj.current_phase == PhaseName.BUILD

    # Phase 3 Build: requires passing sandbox tests
    orch.save_phase_artifact(proj.id, PhaseName.BUILD, {"files": ["template.json"]})
    orch.record_build_test_result(proj.id, passed=False, summary="1 schema check failed")
    with pytest.raises(OrchestratorError, match="automated sandbox tests to pass"):
        orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)

    orch.record_build_test_result(proj.id, passed=True, summary="4/4 template checks passed")
    proj = orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    assert proj.current_phase == PhaseName.COMMERCIAL

    # Phase 4 Commercial: founder approval
    orch.save_phase_artifact(
        proj.id,
        PhaseName.COMMERCIAL,
        {"price_assumption": "$9 solo pack [ASSUMPTION]", "refund_policy": "14-day refund"},
    )
    proj = orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    assert proj.current_phase == PhaseName.LAUNCH

    # Phase 5 Launch: requires Outbox actions with 0 pending & >=1 approved
    orch.save_phase_artifact(proj.id, PhaseName.LAUNCH, {"channels": ["campus_forum", "email"]})
    a_keep = outbox.propose_action(
        project_id=proj.id,
        phase="launch",
        action_type=ActionType.POST,
        target="campus_forum",
        title="Beta Study Tracker for Finals",
        body="Free preview for first 10 testers.",
    )
    a_drop = outbox.propose_action(
        project_id=proj.id,
        phase="launch",
        action_type=ActionType.EMAIL,
        target="cold_list",
        title="Newsletter blast",
        body="Check out our planner.",
    )
    # Pending items block advancing
    with pytest.raises(OrchestratorError, match="still PENDING"):
        orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)

    outbox.approve_action(a_keep.id)
    outbox.reject_action(a_drop.id, reason="Skip email blast for initial beta")

    proj = orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    assert proj.current_phase == PhaseName.OPERATE

    # Phase 6 Operate: test repeat, pivot_back, and completion/stop
    proj = orch.decide_gate(proj.id, GateOutcome.REPEAT, rationale="Collect Week 2 metrics")
    assert proj.phase_iterations["operate"] == 2

    orch.save_phase_artifact(proj.id, PhaseName.OPERATE, {"next_step": "Keep iterating"})
    proj = orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    assert proj.status == ProjectStatus.COMPLETED
    assert log.verify().valid is True


def test_orchestrator_pivot_back_and_stop_outcomes(tmp_path: Path) -> None:
    db_path = tmp_path / "foundry.sqlite3"
    log = DecisionLog(tmp_path / "decision_log.json")
    orch = Orchestrator(db_path, decision_log=log)

    proj = orch.create_project(name="Pivot Demo", idea_summary="Initial B2B idea")
    orch.save_phase_artifact(proj.id, PhaseName.DISCOVER, {"brief": "v1"})
    proj = orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    assert proj.current_phase == PhaseName.VALIDATE

    # Pivot back to Discover when interviews invalidate customer segment
    proj = orch.decide_gate(
        proj.id,
        GateOutcome.PIVOT_BACK,
        target_phase=PhaseName.DISCOVER,
        rationale="Interviews showed students have the pain, not enterprise HR.",
    )
    assert proj.current_phase == PhaseName.DISCOVER
    assert proj.phase_iterations["discover"] == 2

    # Stop project cleanly when founder decides not to pursue
    proj = orch.decide_gate(
        proj.id,
        GateOutcome.STOP,
        rationale="Founder decided to halt after second round of interviews.",
    )
    assert proj.status == ProjectStatus.STOPPED
    assert log.verify().valid is True
