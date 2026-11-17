"""Discover FACTs require provenance and a separate, fail-closed entailment review."""

import json
from copy import deepcopy

import pytest

from agents.researcher import ResearchOutput
from evidence import ClaimType, EvidenceLedger
from guardrails import (
    CLAIM_SUPPORT_CLASSIFIER_PROMPT,
    SECOND_OPINION_CLAIM_SUPPORT_PROMPT,
    ClassifierVerdict,
    PolicyCategory,
    PolicyContext,
    PolicyEngine,
)
from llm import LLMAdapter, OfflineDeterministicProvider, build_llm_safety_classifier
from tests.test_workflow_discover import (
    BRIEF, PAGE_TEXT, RESEARCH, SCREEN_OK, URL, _critic, _option, _workflow,
)


def _research_fixture():
    research = deepcopy(RESEARCH)
    research["sharpened_idea"] = _option("Invoice reminders for designers")
    research["alternatives"] = [_option("Invoice templates"), _option("Late fee calculator")]
    research["recommended"] = "Invoice reminders for designers"
    return research


def _model_engine(*replies):
    provider = OfflineDeterministicProvider([json.dumps(reply) for reply in replies])
    prompts, payloads = [], []
    chat = provider.chat

    def capture(**kwargs):
        prompts.append(kwargs["system_prompt"])
        payloads.append(kwargs["user_prompt"])
        return chat(**kwargs)

    provider.chat = capture
    return PolicyEngine(classifier=build_llm_safety_classifier(LLMAdapter(provider=provider))), prompts, payloads


@pytest.mark.parametrize("claim,quote,reason", [
    ("Designers want automatic reminders.", "Designers do not want automatic reminders.",
     "The claim reverses the explicit negation."),
    ("All designers buy reminder tools.", "Some designers may consider buying reminder tools.",
     "Possible interest does not establish a universal purchase."),
    ("The tool reduces invoice delays.", "Our hypothesis is that the tool reduces invoice delays.",
     "An explicit hypothesis is not an established result."),
    ("Designers pay for reminder tools.", "Designers spend five hours organizing invoice spreadsheets.",
     "Time spent does not imply willingness to pay."),
    ("Every designer requires reminders.", "Some designers use spreadsheets to manage project timelines.",
     "The quote is unrelated to the absolute conclusion."),
])
def test_unsupported_claims_use_entailment_not_policy_prompt(claim, quote, reason):
    engine, prompts, payloads = _model_engine({
        "reason": reason, "unsupported": True, "overall": "CLASSIFIER_UNCERTAIN_OR_UNSAFE",
    })
    verdict = engine.review_claim_support(claim, quote)
    assert not verdict.safe
    assert reason in verdict.reason
    assert len(prompts) == 1
    assert prompts[0].startswith(CLAIM_SUPPORT_CLASSIFIER_PROMPT)
    assert claim in payloads[0] and quote in payloads[0]


@pytest.mark.parametrize("supported,confident", [(True, True), (False, True), (True, False)])
def test_two_independent_support_checks_must_agree(supported, confident):
    engine, prompts, payloads = _model_engine(
        {"reason": "The quote describes time spent.", "unsupported": False, "overall": "CLEAN"},
        {"reason": "Independent scope review.", "supported": supported, "confident": confident},
    )
    claim = "Designers spend hours chasing invoices."
    quote = "Designers spend five hours a month chasing invoices."
    verdict = engine.review_claim_support(claim, quote)
    assert verdict.safe is (supported and confident)
    assert len(prompts) == 2
    assert prompts[1].startswith(SECOND_OPINION_CLAIM_SUPPORT_PROMPT)
    assert all(claim in payload and quote in payload for payload in payloads)


@pytest.mark.parametrize("failure", ["missing", "error", "uncertain", "offline", "second_offline"])
def test_claim_support_unavailability_and_uncertainty_fail_closed(failure):
    def classify(text, ctx):
        if failure == "error":
            raise ConnectionError("Do not persist provider error details.")
        return ClassifierVerdict(safe=True, confident=False, reason="Unsure.")

    engine = PolicyEngine(classifier=None if failure == "missing" else classify)
    if failure == "offline":
        engine, _, _ = _model_engine()
    if failure == "second_offline":
        engine, _, _ = _model_engine(
            {"reason": "Seems supported.", "unsupported": False, "overall": "CLEAN"},
        )
    verdict = engine.review_claim_support("A claim.", "The exact fetched supporting quote.")
    assert not verdict.safe
    assert verdict.reason


@pytest.mark.parametrize("failure", ["unsupported", "uncertain", "error"])
def test_discover_downgrade_survives_persistence_export_and_logs_reason(tmp_path, failure):
    research = _research_fixture() | {"claims": [{
        "text": "Every designer will pay for reminders.",
        "source_number": 1,
        "quote": "spending five hours a month chasing late invoices",
    }, {
        "text": "Designers may prefer a simpler interface.", "source_number": None, "quote": None,
    }, {
        "text": "A monthly reminder may be useful.", "source_number": None, "quote": None,
    }]}
    wf, log = _workflow(tmp_path, [deepcopy(SCREEN_OK), research, _critic("go")])
    pid = wf.start_project(BRIEF).id
    surfaces = []

    def classify(text, ctx):
        surfaces.append(ctx.surface)
        if ctx.surface == "claim_support":
            payload = json.loads(text)
            assert payload["claim"] == research["claims"][0]["text"]
            assert payload["supporting_quote"] == research["claims"][0]["quote"]
            if failure == "error":
                raise ConnectionError("Model down")
            return ClassifierVerdict(
                safe=failure == "uncertain", confident=failure != "uncertain",
                category=PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE,
                reason="The quote does not establish purchases.",
            )
        return ClassifierVerdict(safe=True, reason="Safe internal analysis.")

    wf.engine.classifier = classify
    state = wf.run_discover_step1(pid)
    assert state["awaiting"] == "step1_gate"
    assert state["recommendation"] == "repeat"
    saved = EvidenceLedger(wf.orch.db_path).list_claims(project_id=pid)
    assert len(saved) == 3
    assert all(claim.claim_type == ClaimType.ASSUMPTION for claim in saved)
    reviewed = next(claim for claim in saved if claim.text == research["claims"][0]["text"])
    assert "ASSUMPTION" in wf.ledger.export_markdown(project_id=pid)
    report = (tmp_path / "outputs" / "phase1_step1_opportunities.md").read_text()
    assert reviewed.text in report and "`ASSUMPTION`" in report
    reviews = [entry for entry in log.read_all() if entry.action == "review_claim_support"]
    assert len(reviews) == 1 and reviews[0].detail
    assert reviews[0].outcome == "assumption"
    assert reviews[0].metadata["supporting_quote"] == research["claims"][0]["quote"]
    assert log.verify().valid
    assert "claim_support" in surfaces and "internal_output" in surfaces


def test_review_uses_only_proven_sentence_preserving_numeric_citations(tmp_path):
    wf, _ = _workflow(tmp_path, [])
    page = wf.ledger.record_fetch(
        project_id="p", url=URL,
        text=PAGE_TEXT.replace("five hours", "five[12] hours"),
    )
    seen = []

    def classify(text, ctx):
        assert ctx.surface == "claim_support"
        seen.append(json.loads(text))
        return ClassifierVerdict(safe=True, reason="Faithful narrower claim.")

    wf.engine.classifier = classify
    research = ResearchOutput(**(_research_fixture() | {"claims": [
        {"text": "Designers spend hours chasing invoices.", "source_number": 1,
         "quote": "Freelance designers report spending five hours a month chasing late invoices. "
                  "Everyone is guaranteed to buy our product."},
        {"text": "An unverified claim.", "source_number": 1,
         "quote": "This alleged quote is not present on the fetched page."},
        {"text": "A simpler reminder may be useful.", "source_number": None, "quote": None},
    ]}))
    claims = wf._record_claims("p", research, [page])
    assert len(seen) == 1
    assert seen[0]["supporting_quote"] == (
        "Freelance designers report spending five hours a month chasing late invoices."
    )
    assert claims[0].claim_type == ClaimType.FACT
    assert "Everyone" not in claims[0].quote
    assert claims[1].claim_type == ClaimType.ASSUMPTION
    assert claims[2].claim_type == ClaimType.ASSUMPTION


def test_internal_output_phase_is_optional_and_preserved(tmp_path):
    wf, _ = _workflow(tmp_path, [])
    phases = []

    def classify(text, ctx):
        phases.append(ctx.phase)
        return ClassifierVerdict(safe=True, reason="Cleared.")

    wf.engine.classifier = classify
    output = ResearchOutput(**_research_fixture())
    wf._check_internal_output("p", output, "researcher")
    wf._check_internal_output("p", output, "operator", phase="operate")
    assert phases == ["discover", "operate"]
