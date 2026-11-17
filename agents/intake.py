"""Intake agent: holds the founder brief and screens out excluded product types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents import AgentResult, run_agent
from llm import LLMAdapter

ExcludedCategory = Literal[
    "none", "physical_goods", "financial_investment_crypto", "health_advice", "legal_advice"
]

EXCLUDED_LABELS: dict[str, str] = {
    "physical_goods": "physical goods",
    "financial_investment_crypto": "financial, investment or crypto products",
    "health_advice": "health advice products",
    "legal_advice": "legal advice products",
}


class FounderBrief(BaseModel):
    """What the founder gives Foundry at the start."""

    idea: str = Field(min_length=10, max_length=2000)
    skills: str = Field(default="", max_length=1000)
    budget: str = Field(default="", max_length=300)
    time_available: str = Field(default="", max_length=300)
    target_market: str = Field(default="", max_length=1000)
    values: str = Field(default="", max_length=1000)
    source_urls: list[str] = Field(default_factory=list, max_length=5)

    def as_prompt(self) -> str:
        return (
            "FOUNDER BRIEF\n"
            f"Starting idea: {self.idea}\n"
            f"Skills: {self.skills or 'not given'}\n"
            f"Budget: {self.budget or 'not given'}\n"
            f"Available time: {self.time_available or 'not given'}\n"
            f"Target market: {self.target_market or 'not given'}\n"
            f"Values: {self.values or 'not given'}"
        )


class BriefScreen(BaseModel):
    excluded_category: ExcludedCategory
    reason: str = Field(min_length=5, max_length=400)
    search_queries: list[str] = Field(default_factory=list, max_length=3)

    @property
    def is_excluded(self) -> bool:
        return self.excluded_category != "none"


def screen_brief(adapter: LLMAdapter, brief: FounderBrief) -> AgentResult[BriefScreen]:
    return run_agent(
        adapter, prompt_name="intake", user_prompt=brief.as_prompt(), response_model=BriefScreen
    )
