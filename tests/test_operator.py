"""Operate reviews use scripted offline model replies; no workflow or external services."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from agents.intake import FounderBrief
from agents.operator import (
    OperateOutput,
    RecordedMetrics,
    render_operate_memo,
    review_operations,
)
from agents.researcher import Opportunity
from llm import LLMAdapter, LLMConfig, LLMError, OfflineDeterministicProvider


BRIEF = FounderBrief(
    idea="A digital interview planning kit for freelance designers",
    skills="Writing and design",
    budget="$0",
    time_available="Two hours a week",
    target_market="Freelance designers",
)
OPTION = Opportunity(
    title="Designer interview kit",
    customer="Freelance designers",
    problem="Designers need a repeatable way to learn about client needs.",
    product="A downloadable interview planning kit.",
    problem_severity=3,
    reachable_market=3,
    founder_fit=4,
    cheap_first_test=5,
    score_reason="The founder can test the problem with a short manual interview.",
)
METRICS = {
    "recorded_demand_count": None,
    "demand_target": 10,
    "approved_drafts": 3,
    "rejected_drafts": 1,
    "pending_drafts": 2,
    "exported_drafts": 2,
    "sourced_claims": 4,
    "assumptions": 5,
}
REPLY = {
    "working_hypothesis": "Designers may value a repeatable interview checklist.",
    "recommendation": "repeat",
    "rationale": "Test past interview preparation behavior before making a product commitment.",
    "confidence": "low",
    "traction_status": "not_established",
    "risks": ["Designers may already have a sufficient free checklist."],
    "learning_limits": ["Founder review activity does not establish independent customer demand."],
    "experiments": [{
        "hypothesis": "Designers recently struggled with planning client interviews.",
        "method": "After separate founder approval, invite five distinct freelance designers "
                  "with recent client projects. Record consented notes and provenance; "
                  "count only independently checked accounts of a preparation problem last month.",
        "metric": "independently_verified_problem_interviews",
        "sample_size": 5,
        "success_threshold": 3,
        "duration_days": 7,
        "cost_usd": 0,
        "if_below_threshold": "pivot",
    }],
}


def _adapter(*replies: dict) -> LLMAdapter:
    return LLMAdapter(
        config=LLMConfig(max_repair_retries=len(replies) - 1),
        provider=OfflineDeterministicProvider([json.dumps(reply) for reply in replies]),
    )


def test_missing_demand_is_unknown_and_renderer_preserves_recorded_facts() -> None:
    metrics = RecordedMetrics(**METRICS)
    result = review_operations(_adapter(REPLY), BRIEF, OPTION, metrics)
    memo = render_operate_memo(result.output, metrics)

    assert result.model == "offline-scripted"
    assert "DRAFT internal review" in memo
    assert "Founder-recorded demand (not independently verified) | not recorded" in memo
    assert "Independently verified demand: unavailable" in memo
    assert "retention telemetry: unavailable (not measured, not zero)" in memo
    assert "Copy-ready exports (not publication) | 2" in memo
    assert "Product Build | STUBBED" in memo and "Delivery | MOCKED" in memo
    assert "at least 3 " in memo and "out of 5 " in memo
    assert "proposals, not observations" in memo
    assert "separate exact-content founder approval" in memo


@pytest.mark.parametrize("recorded", [None, 0])
def test_no_recorded_demand_repairs_unsupported_medium_confidence(recorded) -> None:
    metrics = RecordedMetrics(**{**METRICS, "recorded_demand_count": recorded})
    too_confident = {**REPLY, "confidence": "medium"}
    result = review_operations(_adapter(too_confident, REPLY), BRIEF, OPTION, metrics)
    assert result.attempts == 2
    assert result.output.confidence == "low"


def test_missing_target_cannot_support_medium_confidence() -> None:
    metrics = RecordedMetrics(**{**METRICS, "recorded_demand_count": 8, "demand_target": None})
    with pytest.raises(LLMError, match="requires low confidence"):
        review_operations(_adapter({**REPLY, "confidence": "medium"}), BRIEF, OPTION, metrics)


@pytest.mark.parametrize("threshold", [0, -1, 6, 2.5, True])
def test_invalid_experiment_threshold_fails_closed(threshold) -> None:
    reply = copy.deepcopy(REPLY)
    reply["experiments"][0]["success_threshold"] = threshold
    with pytest.raises(LLMError, match="validation failed"):
        review_operations(_adapter(reply), BRIEF, OPTION, RecordedMetrics(**METRICS))


def test_mocked_delivery_and_exports_cannot_establish_real_traction() -> None:
    metrics = RecordedMetrics(**{
        **METRICS, "recorded_demand_count": 100, "exported_drafts": 200,
    })
    for unsupported in (
        {**REPLY, "confidence": "high"},
        {**REPLY, "traction_status": "established"},
        {**REPLY, "real_customers": 200},
    ):
        with pytest.raises(LLMError, match="validation failed"):
            review_operations(_adapter(unsupported), BRIEF, OPTION, metrics)

    result = review_operations(_adapter({**REPLY, "confidence": "medium"}), BRIEF, OPTION, metrics)
    memo = render_operate_memo(result.output, metrics)
    assert "Founder-recorded demand (not independently verified) | 100" in memo
    assert "Copy-ready exports (not publication) | 200" in memo
    assert "Product traction is not established." in memo


@pytest.mark.parametrize("change", [
    {"recorded_demand_count": -1},
    {"approved_drafts": True},
    {"exported_drafts": 1.5},
    {"demand_target": 0},
    {"delivery_label": "Working"},
    {"build_status": "Working"},
    {"live_telemetry": "available"},
    {"revenue": 500},
])
def test_snapshot_rejects_invalid_or_unsupported_measurements(change: dict) -> None:
    with pytest.raises(ValidationError):
        RecordedMetrics(**{**METRICS, **change})


def test_renderer_does_not_allow_model_prose_to_forge_metrics_table() -> None:
    reply = {**REPLY, "rationale": "Tentative only\n| Revenue | 999 |\n<script>alert(1)</script>"}
    memo = render_operate_memo(OperateOutput.model_validate(reply), RecordedMetrics(**METRICS))
    assert "\n| Revenue |" not in memo
    assert "<script>" not in memo
    assert "ASSUMPTION / interpretation:" in memo
