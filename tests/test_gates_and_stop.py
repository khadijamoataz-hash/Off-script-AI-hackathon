"""Smoke tests: the gate recommends STOP on weak evidence, and nothing goes out without approval."""

from __future__ import annotations

from pathlib import Path

import pytest

from approval import ActionType, OutboxService
from decision_log import DecisionLog
from evidence import EvidenceLedger
from gates import GateOutcome, PhaseName
from guardrails import PolicyViolationError
from orchestrator import Orchestrator, OrchestratorError


def _setup(tmp_path: Path, target: int) -> tuple[Orchestrator, EvidenceLedger, OutboxService, DecisionLog, str]:
    db = tmp_path / "foundry.sqlite3"
    log = DecisionLog(tmp_path / "decision_log.json")
    ledger = EvidenceLedger(db)
    approvals = OutboxService(db, export_dir=tmp_path / "outputs", decision_log=log, signing_secret="k")
    orch = Orchestrator(db, evidence_ledger=ledger, decision_log=log, outbox=approvals)
    proj = orch.create_project(name="Weak idea", idea_summary="x", validate_evidence_target=target)
    orch.save_phase_artifact(proj.id, PhaseName.DISCOVER, {"brief": "v1"}, agent="researcher")
    orch.decide_gate(proj.id, GateOutcome.ADVANCE, founder_approved=True)
    orch.save_phase_artifact(proj.id, PhaseName.VALIDATE, {"plan": "landing page"}, agent="strategist")
    return orch, ledger, approvals, log, proj.id


def test_gate_recommends_stop_when_evidence_is_weak(tmp_path: Path) -> None:
    orch, ledger, _, log, pid = _setup(tmp_path, target=100)
    ledger.record_fetch(project_id=pid, url="https://example.org/a", text="A sourced fact, as published.")
    fact = ledger.add_claim("A sourced fact", project_id=pid, linked_phase="validate",
                            source_url="https://example.org/a", quote="A sourced fact, as published.")
    orch.record_demand_signals(pid, 11, note="waitlist sign-ups", evidence_ids=[fact.id])

    check = orch.check_gate(pid)
    assert check.ready_to_advance is False
    assert check.recommendation == GateOutcome.STOP
    assert "11 of the 100" in check.recommendation_reason

    # The founder cannot push past a failed evidence gate, and the block is logged with the advice
    with pytest.raises(OrchestratorError, match="Evidence target not yet reached"):
        orch.decide_gate(pid, GateOutcome.ADVANCE, founder_approved=True)
    blocked = [e for e in log.read_all() if e.outcome == "blocked"][-1]
    assert blocked.agent == "gate" and blocked.action == "validate_gate"
    assert blocked.metadata["system_recommendation"] == "stop"

    # Sources in the log are the URLs behind the evidence IDs
    signal_entry = [e for e in log.read_all() if e.action == "record_demand_signals"][-1]
    assert signal_entry.sources == ["https://example.org/a"]

    orch.decide_gate(pid, GateOutcome.STOP, rationale="Evidence too weak")
    stop = log.read_all()[-1]
    assert stop.outcome == "stop" and stop.approved_by == "founder"
    assert log.verify().valid is True


def test_gate_recommends_repeat_when_close_and_target_is_fixed(tmp_path: Path) -> None:
    orch, ledger, _, _, pid = _setup(tmp_path, target=10)
    ledger.record_fetch(project_id=pid, url="https://example.org/a", text="A sourced fact, as published.")
    ledger.add_claim("A sourced fact", project_id=pid, linked_phase="validate",
                     source_url="https://example.org/a", quote="A sourced fact, as published.")
    orch.record_demand_signals(pid, 6)
    assert orch.check_gate(pid).recommendation == GateOutcome.REPEAT
    with pytest.raises(OrchestratorError, match="fixed once Validate starts"):
        orch.set_validate_evidence_target(pid, 1)


def test_sign_action_needs_approval_and_output_is_named_by_phase_and_step(tmp_path: Path) -> None:
    _, _, approvals, _, pid = _setup(tmp_path, target=3)
    action = approvals.propose_action(
        project_id=pid, phase="commercial", step=2, action_type=ActionType.SIGN,
        target="store_terms", title="Accept store terms", body="Founder accepts the store's seller terms.",
    )
    with pytest.raises(PolicyViolationError):
        approvals.execute_action(action.id, approval_token=None)
    receipt = approvals.execute_action(action.id, approvals.approve_action(action.id))
    assert Path(receipt.export_path).name.startswith("phase4_step2_approved_sign_")
