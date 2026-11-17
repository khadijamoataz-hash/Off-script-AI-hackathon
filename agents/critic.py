"""Critic agent: strongest case, then risks, kill criteria and the cheapest disproving test.

The schema is the enforcement: an answer with no risks, no kill criteria or no test does not
validate, so the critic cannot simply agree.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agents import AgentResult, run_agent
from agents.intake import FounderBrief
from agents.researcher import Opportunity
from evidence import Claim, ClaimType
from llm import LLMAdapter


class Tweak(BaseModel):
    change: str = Field(min_length=5, max_length=300)
    reason: str = Field(min_length=5, max_length=400)


class CriticOutput(BaseModel):
    strongest_case: str = Field(min_length=20, max_length=800)
    risks: list[str] = Field(min_length=3, max_length=6)
    kill_criteria: list[str] = Field(min_length=2, max_length=5)
    cheapest_disproving_test: str = Field(min_length=20, max_length=600)
    tweaks: list[Tweak] = Field(min_length=1, max_length=3)
    confidence: Literal["high", "medium", "low"]
    recommendation: Literal["go", "repeat", "pivot", "stop"]
    reason: str = Field(min_length=10, max_length=600)

    @model_validator(mode="after")
    def no_blank_items(self) -> "CriticOutput":
        for name in ("risks", "kill_criteria"):
            if any(len(item.strip()) < 10 for item in getattr(self, name)):
                raise ValueError(f"Every item in {name} must be a specific sentence.")
        return self


def critique(
    adapter: LLMAdapter, brief: FounderBrief, option: Opportunity, claims: list[Claim]
) -> AgentResult[CriticOutput]:
    evidence_lines = [
        f"- [{'FACT' if c.claim_type == ClaimType.FACT else 'ASSUMPTION'}] {c.text}" for c in claims
    ] or ["- (no evidence recorded)"]
    facts = sum(1 for c in claims if c.claim_type == ClaimType.FACT)
    user_prompt = (
        f"{brief.as_prompt()}\n\n"
        "RECOMMENDED OPTION\n"
        f"Title: {option.title}\nCustomer: {option.customer}\n"
        f"Problem: {option.problem}\nProduct: {option.product}\n\n"
        f"EVIDENCE ({facts} FACT, {len(claims) - facts} ASSUMPTION)\n" + "\n".join(evidence_lines)
    )
    return run_agent(
        adapter, prompt_name="critic", user_prompt=user_prompt, response_model=CriticOutput
    )
