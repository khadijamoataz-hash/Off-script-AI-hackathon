"""Stage 2 Red-Team & Fail-Closed Policy Engine tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_log import DecisionLog
from guardrails import (
    ClassifierVerdict,
    PolicyCategory,
    PolicyContext,
    PolicyEngine,
    PolicyViolationError,
)
from evals.redteam import REDTEAM_CASES, format_redteam_markdown_table, run_redteam_suite


@pytest.mark.parametrize("case", REDTEAM_CASES, ids=[c.case_id for c in REDTEAM_CASES])
def test_canonical_redteam_prompt_is_blocked_logged_and_prompts_founder(
    tmp_path: Path, case
) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    engine = PolicyEngine(decision_log=log)

    if case.surface == "tool_call" and case.tool_name:
        decision = engine.evaluate_tool_call(
            case.tool_name,
            {"payload": case.prompt_or_payload},
            project_id="proj-rt",
            phase="launch",
            has_valid_approval_token=False,
        )
    else:
        decision = engine.evaluate_text(
            case.prompt_or_payload,
            context=PolicyContext(project_id="proj-rt", phase="launch", surface=case.surface),
        )

    assert decision.allowed is False
    assert decision.category == case.expected_category
    assert decision.founder_prompt is not None and len(decision.founder_prompt) > 10

    entries = log.read_all()
    assert len(entries) == 1
    assert entries[0].agent == "guardrail"
    assert entries[0].outcome == "blocked"
    assert log.verify().valid is True


def test_full_redteam_suite_runner_and_markdown_table(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    results = run_redteam_suite(decision_log=log)
    assert len(results) == len(REDTEAM_CASES)
    assert all(r.passed for r in results)
    assert log.verify().valid is True

    table = format_redteam_markdown_table(results)
    assert "**PASS**" in table
    assert "**FAIL**" not in table


def test_policy_engine_fails_closed_when_classifier_errors_or_is_uncertain(
    tmp_path: Path,
) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")

    def crashing_classifier(text: str, ctx: PolicyContext) -> ClassifierVerdict:
        raise TimeoutError("Local model classifier timed out")

    crashing_engine = PolicyEngine(decision_log=log, classifier=crashing_classifier)
    d1 = crashing_engine.evaluate_text("Draft a normal launch post about study planners.")
    assert d1.allowed is False
    assert d1.category == PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE
    assert d1.rule_id == "POL-900-CLASSIFIER-ERROR"

    def uncertain_classifier(text: str, ctx: PolicyContext) -> ClassifierVerdict:
        return ClassifierVerdict(safe=True, confident=False, reason="Ambiguous wording")

    uncertain_engine = PolicyEngine(decision_log=log, classifier=uncertain_classifier)
    d2 = uncertain_engine.evaluate_text("Subtle edge-case marketing claim.")
    assert d2.allowed is False
    assert d2.category == PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE
    assert d2.rule_id == "POL-901-CLASSIFIER-BLOCK"

    assert len(log.read_all()) == 2
    assert log.verify().valid is True


def test_policy_engine_allows_legitimate_evidence_backed_founder_drafts(
    tmp_path: Path,
) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    engine = PolicyEngine(decision_log=log)

    safe_draft = (
        "Launching our Notion Semester Planner for AUC engineering students [CLM-1A2B3C4D]. "
        "Pricing hypothesis: $12 one-time [ASSUMPTION]. Reply if you'd like early beta access."
    )
    decision = engine.enforce_text(
        safe_draft,
        context=PolicyContext(project_id="proj-safe", phase="launch", surface="agent_output"),
    )
    assert decision.allowed is True
    assert decision.category == PolicyCategory.CLEAN
    assert len(log.read_all()) == 0

    with pytest.raises(PolicyViolationError):
        engine.enforce_text("We guarantee you will make $10k profit in 7 days!")
