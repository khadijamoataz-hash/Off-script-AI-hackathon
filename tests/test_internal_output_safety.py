"""Internal analysis is screened whole, before claims, downstream agents or exports."""

import json

import pytest

from guardrails import (
    CONTENT_CLASSIFIER_PROMPT,
    INTERNAL_OUTPUT_CLASSIFIER_PROMPT,
    SECOND_OPINION_INTERNAL_PROMPT,
    ClassifierVerdict,
    PolicyCategory,
    PolicyContext,
    PolicyEngine,
)
from llm import (
    MAX_INTERNAL_OUTPUT_CHARS, LLMAdapter, OfflineDeterministicProvider,
    build_llm_safety_classifier,
)
from tests.test_workflow_discover import BRIEF, RESEARCH, SCREEN_OK, SCRIPT, _critic, _workflow
from workflow import WorkflowError


def _classifier(*replies):
    provider = OfflineDeterministicProvider([json.dumps(reply) for reply in replies])
    return build_llm_safety_classifier(LLMAdapter(provider=provider))


def _internal_checklist(**changes):
    return {
        "reason": "Internal analysis.", "promise_results": False,
        "fabricated_proof": False, "overall": "CLEAN",
    } | changes


@pytest.mark.parametrize("text", [
    'Cited survey: 40% found jobs last year (https://example.org/survey).',
    'Risk: no customers pay. Kill criterion: stop after ten negative interviews.',
    'Competitor claims "get hired immediately"; this is not evidence or our promise.',
    '{"do_not_ask": ["Would you buy if we guaranteed you a job?"]}',
    '{"summary": "ASSUMPTION: 20% of workshop organizers may need a CSV checklist.", "risks": ["There are no measured sign-ups or interviews yet."], "kill_criteria": ["Stop if the demand target is missed."]}',
])
def test_internal_analysis_uses_dedicated_prompts_and_second_opinion(text):
    prompts = []
    payloads = []
    provider = OfflineDeterministicProvider([
        json.dumps(_internal_checklist()),
        json.dumps({"would_require": "Honest analysis.", "category": "CLEAN"}),
    ])
    original = provider.chat

    def chat(**kwargs):
        prompts.append(kwargs["system_prompt"])
        payloads.append(kwargs["user_prompt"])
        return original(**kwargs)

    provider.chat = chat
    engine = PolicyEngine(classifier=build_llm_safety_classifier(LLMAdapter(provider=provider)))
    assert engine.evaluate_text(text, PolicyContext(surface="internal_output")).allowed
    assert len(prompts) == 2
    assert prompts[0].startswith(INTERNAL_OUTPUT_CLASSIFIER_PROMPT.strip() + "\n\n")
    assert prompts[1].startswith(SECOND_OPINION_INTERNAL_PROMPT.strip() + "\n\n")
    assert all(not prompt.startswith(CONTENT_CLASSIFIER_PROMPT) for prompt in prompts)
    assert all(text in payload for payload in payloads)


@pytest.mark.parametrize("reply,category", [
    (_internal_checklist(promise_results=True), PolicyCategory.INCOME_GUARANTEE),
    (_internal_checklist(fabricated_proof=True), PolicyCategory.FAKE_SOCIAL_PROOF),
    (_internal_checklist(overall="CLASSIFIER_UNCERTAIN_OR_UNSAFE"),
     PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE),
])
def test_internal_model_blocks_unsafe_or_uncertain(reply, category):
    engine = PolicyEngine(classifier=_classifier(reply))
    decision = engine.evaluate_text(
        "Get hired immediately. Our first hundred customers love us.",
        PolicyContext(surface="internal_output"),
    )
    assert not decision.allowed and decision.category == category


def test_internal_missing_model_and_disabled_checks_fail_closed():
    context = PolicyContext(surface="internal_output")
    assert not PolicyEngine().evaluate_text("Analysis.", context).allowed
    assert not PolicyEngine().evaluate_text("Analysis.", context, use_classifier=False).allowed
    engine = PolicyEngine(classifier=_classifier())
    assert not engine.evaluate_text("Analysis.", context).allowed


@pytest.mark.parametrize("failure", ["unavailable", "uncertain"])
def test_internal_classifier_failure_prevents_research_persistence(tmp_path, failure):
    wf, log = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go")])
    pid = wf.start_project(BRIEF).id

    def classifier(text, ctx):
        if ctx.surface == "internal_output":
            if failure == "unavailable":
                raise ConnectionError("Model unavailable")
            return ClassifierVerdict(
                safe=True, confident=False, category=PolicyCategory.CLEAN, reason="Unsure.",
            )
        return ClassifierVerdict(safe=True, category=PolicyCategory.CLEAN, reason="Cleared.")

    wf.engine.classifier = classifier
    with pytest.raises(WorkflowError, match="output refused"):
        wf.run_discover_step1(pid)
    assert not wf.ledger.list_claims(project_id=pid)
    assert not wf.orch.get_project(pid).phase_artifacts
    assert not list((tmp_path / "outputs").glob("phase1*"))
    assert wf.get_state(pid)["awaiting"] is None
    assert any(e.action == "block_classifier_uncertain_or_unsafe" for e in log.read_all())


def test_internal_second_opinion_sees_entire_payload_and_can_veto():
    provider = OfflineDeterministicProvider([
        json.dumps(_internal_checklist()),
        json.dumps({"would_require": "Promise a job.", "category": "INCOME_GUARANTEE"}),
    ])
    payloads = []
    original = provider.chat

    def chat(**kwargs):
        payloads.append(kwargs["user_prompt"])
        return original(**kwargs)

    provider.chat = chat
    engine = PolicyEngine(classifier=build_llm_safety_classifier(LLMAdapter(provider=provider)))
    decision = engine.evaluate_text(
        "Ordinary research. " * 500 + "Get hired immediately.",
        PolicyContext(surface="internal_output"),
    )
    assert not decision.allowed
    assert len(payloads) == 2
    assert all("Get hired immediately." in payload for payload in payloads)


def test_oversized_internal_payload_fails_closed_instead_of_truncating():
    engine = PolicyEngine(classifier=_classifier(
        _internal_checklist(), {"would_require": "Analysis.", "category": "CLEAN"},
    ))
    decision = engine.evaluate_text(
        "x" * MAX_INTERNAL_OUTPUT_CHARS + "Get hired immediately.",
        PolicyContext(surface="internal_output"),
    )
    assert not decision.allowed
    assert decision.rule_id == "POL-900-CLASSIFIER-ERROR"


@pytest.mark.parametrize("boundary", ["researcher", "critic", "interview"])
def test_refusal_prevents_invalid_artifacts_and_gates(tmp_path, boundary):
    research = RESEARCH | (
        {"recommended_reason": "Get hired immediately."} if boundary == "researcher" else {}
    )
    critic = _critic("go") | (
        {"strongest_case": "Get hired immediately."} if boundary == "critic" else {}
    )
    script = SCRIPT | (
        {"opening": "Get hired immediately."} if boundary == "interview" else {}
    )
    wf, log = _workflow(tmp_path, [SCREEN_OK, research, critic, script])
    pid = wf.start_project(BRIEF).id
    surfaces = []
    internal_calls = 0
    blocked_call = {"researcher": 1, "critic": 2, "interview": 5}[boundary]

    def classifier(text, ctx):
        nonlocal internal_calls
        surfaces.append(ctx.surface)
        if ctx.surface == "internal_output":
            internal_calls += 1
            if internal_calls == blocked_call:
                assert "Get hired immediately." in text
                return ClassifierVerdict(
                    safe=False, category=PolicyCategory.INCOME_GUARANTEE,
                    reason="Get hired immediately is a promise.",
                )
        return ClassifierVerdict(safe=True, reason="Cleared.", category=PolicyCategory.CLEAN)

    wf.engine.classifier = classifier
    if boundary == "interview":
        wf.run_discover_step1(pid)
        wf.decide_step(pid, "continue")
        run = wf.run_discover_step2
    else:
        run = wf.run_discover_step1
    with pytest.raises(WorkflowError, match="output refused"):
        run(pid)
    assert wf.get_state(pid)["awaiting"] is None
    assert "refused" in wf.get_state(pid)["refusal"]
    assert "internal_output" in surfaces
    entries = log.read_all()
    assert any(e.action == "block_income_guarantee" for e in entries)
    assert not (tmp_path / "outputs" / "phase1_gate_decision.md").exists()
    if boundary != "interview":
        assert not wf.orch.get_project(pid).phase_artifacts
        assert not (tmp_path / "outputs" / "phase1_step1_opportunities.md").exists()
        assert not any(e.action == "discover_step1_gate" for e in entries)
    else:
        assert not (tmp_path / "outputs" / "phase1_step2_interview_script.md").exists()
        assert not any(e.action == "draft_interview_script" for e in entries)
    if boundary == "researcher":
        assert not wf.ledger.list_claims(project_id=pid)
        assert not any(e.action == "critique_idea" for e in entries)


def test_clean_injected_classifier_preserves_discover_workflow(tmp_path):
    wf, _ = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go"), SCRIPT])
    pid = wf.start_project(BRIEF).id
    assert wf.run_discover_step1(pid)["awaiting"] == "step1_gate"
    wf.decide_step(pid, "continue")
    assert wf.run_discover_step2(pid)["awaiting"] == "phase_gate"
    assert "[Removed by guardrails:" not in (
        tmp_path / "outputs" / "phase1_step1_opportunities.md"
    ).read_text()
