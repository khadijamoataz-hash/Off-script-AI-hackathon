"""Generation budgets must repair oversized drafts without breaking saved artifacts."""

from __future__ import annotations

import json

import pytest

from agents.intake import FounderBrief
from agents.researcher import ResearchOutput, research_opportunities
from llm import LLMAdapter, LLMConfig, LLMError, OfflineDeterministicProvider


def _research(claims: int = 3, alternatives: int = 2) -> dict:
    def option(title: str) -> dict:
        return {
            "title": title,
            "customer": "Independent language tutors",
            "problem": "Tutors track lesson preparation across scattered notes.",
            "product": "A digital lesson preparation checklist template.",
            "problem_severity": 3,
            "reachable_market": 2,
            "founder_fit": 4,
            "cheap_first_test": 4,
            "score_reason": "Templates fit the founder's skills; customer reach remains untested.",
        }

    return {
        "sharpened_idea": option("Lesson preparation checklist"),
        "alternatives": [option(f"Tutor workflow option {i}") for i in range(alternatives)],
        "market_definition": "ASSUMPTION: Independent tutors reachable through local teaching groups.",
        "claims": [
            {"text": f"ASSUMPTION: Tutor workflow pain hypothesis {i} needs interviews.",
             "source_number": None, "quote": None}
            for i in range(claims)
        ],
        "recommended": "Lesson preparation checklist",
        "recommended_reason": "Test preparation friction before investing in a larger product.",
    }


def _adapter(*responses: dict, retries: int = 1) -> LLMAdapter:
    return LLMAdapter(
        config=LLMConfig(provider="offline", max_repair_retries=retries),
        provider=OfflineDeterministicProvider(
            scripted_responses=[json.dumps(response) for response in responses],
        ),
    )


@pytest.mark.parametrize("claims,alternatives", [(7, 2), (3, 3)])
def test_oversized_generation_requires_repair_instead_of_silent_slicing(
    claims: int, alternatives: int,
) -> None:
    repaired = _research()
    repaired["claims"][0]["text"] = "ASSUMPTION: Repaired draft targets a newly identified tutor problem."
    result = research_opportunities(
        _adapter(_research(claims, alternatives), repaired),
        FounderBrief(idea="Digital lesson preparation tools for language tutors"),
        [], [],
    )
    assert result.attempts == 2
    assert type(result.output) is ResearchOutput
    assert len(result.output.claims) == 3
    assert len(result.output.options()) == 3
    assert result.output.claims[0].text == repaired["claims"][0]["text"]
    assert result.model == "offline-scripted"
    assert all(4 <= option.total <= 20 for option in result.output.options())


def test_historical_research_still_parses_seven_claims_and_three_alternatives() -> None:
    saved = _research(claims=7, alternatives=3)
    saved["market_definition"] = "Earlier market detail. " * 30
    saved["recommended_reason"] = "Earlier recommendation rationale. " * 15
    output = ResearchOutput.model_validate(saved)
    assert len(output.claims) == 7
    assert len(output.alternatives) == 3
    assert output.market_definition == saved["market_definition"]
    assert output.recommended_reason == saved["recommended_reason"]


def test_duplicate_generated_claims_require_repair() -> None:
    duplicate = _research()
    duplicate["claims"][1]["text"] = duplicate["claims"][0]["text"].upper()
    result = research_opportunities(
        _adapter(duplicate, _research()),
        FounderBrief(idea="Digital lesson preparation tools for language tutors"),
        [], [],
    )
    assert result.attempts == 2
    assert len({claim.text.casefold() for claim in result.output.claims}) == 3


def test_unrepaired_oversized_generation_fails_closed() -> None:
    with pytest.raises(LLMError, match="Structured output validation failed"):
        research_opportunities(
            _adapter(_research(claims=7), retries=0),
            FounderBrief(idea="Digital lesson preparation tools for language tutors"),
            [], [],
        )
