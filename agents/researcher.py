"""Researcher agent: sharpens the idea, scores it against alternatives, drafts the interview script."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from agents import AgentResult, run_agent
from agents.intake import FounderBrief
from evidence import FetchedPage
from llm import LLMAdapter


class Opportunity(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    customer: str = Field(min_length=3, max_length=300)
    problem: str = Field(min_length=10, max_length=500)
    product: str = Field(min_length=10, max_length=500)
    problem_severity: int = Field(ge=1, le=5)
    reachable_market: int = Field(ge=1, le=5)
    founder_fit: int = Field(ge=1, le=5)
    cheap_first_test: int = Field(ge=1, le=5)
    score_reason: str = Field(min_length=10, max_length=600)

    @property
    def total(self) -> int:
        return (
            self.problem_severity + self.reachable_market + self.founder_fit + self.cheap_first_test
        )


class ClaimDraft(BaseModel):
    """A market claim as the model proposes it. Code decides whether it is a FACT."""

    text: str = Field(min_length=10, max_length=400)
    source_number: int | None = None
    quote: str | None = Field(default=None, max_length=600)


class ResearchOutput(BaseModel):
    sharpened_idea: Opportunity
    alternatives: list[Opportunity] = Field(min_length=2, max_length=3)
    market_definition: str = Field(min_length=20, max_length=800)
    claims: list[ClaimDraft] = Field(min_length=3, max_length=8)
    recommended: str = Field(min_length=3, max_length=120)
    recommended_reason: str = Field(min_length=10, max_length=600)

    def options(self) -> list[Opportunity]:
        return [self.sharpened_idea, *self.alternatives]


HYPOTHETICAL_PHRASES = ("would you", "will you", "do you think you", "how much would")


class InterviewScript(BaseModel):
    who_to_interview: str = Field(min_length=10, max_length=500)
    opening: str = Field(min_length=10, max_length=500)
    questions: list[str] = Field(min_length=6, max_length=8)
    signals_of_real_demand: list[str] = Field(min_length=2, max_length=4)
    do_not_ask: list[str] = Field(min_length=2, max_length=3)

    @field_validator("questions")
    @classmethod
    def questions_are_about_the_past(cls, questions: list[str]) -> list[str]:
        for question in questions:
            lowered = question.lower()
            if any(phrase in lowered for phrase in HYPOTHETICAL_PHRASES):
                raise ValueError(
                    f"Question breaks The Mom Test (hypothetical or future): {question!r}. "
                    "Ask what the person actually did in the past instead."
                )
        return questions


def format_sources(pages: list[FetchedPage], excerpts: list[str]) -> str:
    if not pages:
        return "SOURCES\n(none could be fetched: every claim must have source_number null)"
    blocks = ["SOURCES"]
    for number, (page, excerpt) in enumerate(zip(pages, excerpts), start=1):
        blocks.append(f"[{number}] {page.title or page.url}\nURL: {page.url}\n{excerpt}")
    return "\n\n".join(blocks)


def research_opportunities(
    adapter: LLMAdapter, brief: FounderBrief, pages: list[FetchedPage], excerpts: list[str]
) -> AgentResult[ResearchOutput]:
    return run_agent(
        adapter,
        prompt_name="researcher",
        user_prompt=f"{brief.as_prompt()}\n\n{format_sources(pages, excerpts)}",
        response_model=ResearchOutput,
    )


def draft_interview_script(
    adapter: LLMAdapter, brief: FounderBrief, option: Opportunity, market_definition: str
) -> AgentResult[InterviewScript]:
    user_prompt = (
        f"{brief.as_prompt()}\n\n"
        "OPTION TO TEST\n"
        f"Customer: {option.customer}\nProblem: {option.problem}\n"
        f"Market: {market_definition}\n\n"
        "The product idea is NOT to be mentioned in the interview."
    )
    return run_agent(
        adapter, prompt_name="interviewer", user_prompt=user_prompt, response_model=InterviewScript
    )
