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


RULE_BLOCK_CASES = [c for c in REDTEAM_CASES if c.expect == "block" and not c.needs_model]
ALLOW_CASES = [c for c in REDTEAM_CASES if c.expect == "allow" and not c.needs_model]


@pytest.mark.parametrize("case", ALLOW_CASES, ids=[c.case_id for c in ALLOW_CASES])
def test_ordinary_text_is_not_blocked_by_the_rules(case) -> None:
    engine = PolicyEngine()
    if case.surface == "tool_call":
        decision = engine.evaluate_tool_call(case.tool_name, {"payload": case.prompt_or_payload})
    else:
        decision = engine.evaluate_text(case.prompt_or_payload, context=PolicyContext(surface=case.surface))
    assert decision.allowed is True


def test_secrets_are_refused_and_never_written_to_the_log(tmp_path: Path) -> None:
    from guardrails import redact_secrets

    log = DecisionLog(tmp_path / "decision_log.json")
    engine = PolicyEngine(decision_log=log)
    for text in ("my key is sk_live_abc12345", "card 4242 4242 4242 4242", "password: hunter22"):
        decision = engine.screen_request(text)
        assert decision.allowed is False
        assert decision.category == PolicyCategory.CREDENTIAL_OR_CARD_STORAGE
    raw = (tmp_path / "decision_log.json").read_text()
    assert "sk_live" not in raw and "4242" not in raw and "hunter22" not in raw
    assert "sk_live" not in redact_secrets("keep sk_live_abc12345 safe")
    assert engine.screen_request("We sold 4000 units in 2024 and 12 in 2025").allowed


def test_any_classifier_category_other_than_clean_blocks() -> None:
    verdict = ClassifierVerdict(reason="looks like a review", category=PolicyCategory.FAKE_SOCIAL_PROOF, safe=True)
    engine = PolicyEngine(classifier=lambda text, ctx: verdict)
    assert engine.evaluate_text("Some reworded request").allowed is False
    assert engine.evaluate_text("Some reworded request", use_classifier=False).allowed is True


@pytest.mark.parametrize("case", RULE_BLOCK_CASES, ids=[c.case_id for c in RULE_BLOCK_CASES])
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
    assert len(results) == len([c for c in REDTEAM_CASES if not c.needs_model])
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
        engine.enforce_text(
            "We guarantee you will make $10k profit in 7 days!",
            context=PolicyContext(surface="agent_output"),
        )


def test_exporting_an_approved_draft_checks_it_as_a_draft() -> None:
    engine = PolicyEngine()
    body = "We send the right message at the right time automatically, so you can focus on design."
    assert engine.evaluate_tool_call(
        "execute_outbox_action", {"body": body}, has_valid_approval_token=True
    ).allowed
    assert not engine.evaluate_tool_call(
        "execute_outbox_action", {"body": "You will definitely make $5,000 in your first month."},
        has_valid_approval_token=True,
    ).allowed
    assert not engine.evaluate_tool_call("execute_outbox_action", {"body": body}).allowed  # no token
