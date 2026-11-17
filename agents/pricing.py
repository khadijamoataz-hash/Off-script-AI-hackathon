"""Pricing agent (Phase 4 Commercial setup): price options and policy drafts.

No payment code exists in Foundry. The agent only writes text: pricing options for the founder
to choose from, and drafts of the terms, privacy and refund policies. Publishing a policy is a
`sign` action that waits for the founder's approval. The founder sets up any checkout with
their own payment provider, outside Foundry.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from agents import AgentResult, run_agent
from agents.intake import FounderBrief
from agents.researcher import Opportunity, format_sources
from evidence import FetchedPage
from llm import LLMAdapter

POLICY_NOTICE = (
    "DRAFT for the founder to review. This is not legal advice: have a qualified person check "
    "it for your country before you publish it."
)
POLICIES: tuple[tuple[str, str], ...] = (
    ("terms", "Terms of service"),
    ("privacy", "Privacy policy"),
    ("refund", "Refund policy"),
)


def _plain(text: str) -> str:
    if re.search(r"#\w", text):
        raise ValueError("Remove all hashtags.")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text):
        raise ValueError("Remove all emoji.")
    return text


class ComparablePrice(BaseModel):
    """What a similar product charges. Sourced only if the quote is on a fetched page."""

    product: str = Field(min_length=2, max_length=120)
    price: str = Field(min_length=1, max_length=80, description="As written on the page, e.g. '$29 one-time'.")
    source_number: int | None = None
    quote: str | None = Field(default=None, max_length=600)


class PriceOption(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    price_usd: float = Field(ge=0, le=5000)
    billing: Literal["one-time", "monthly", "yearly", "free"]
    includes: str = Field(min_length=10, max_length=400)
    rationale: str = Field(min_length=10, max_length=500)

    @field_validator("name", "includes", "rationale")
    @classmethod
    def plain(cls, text: str) -> str:
        return _plain(text)


class PricingOutput(BaseModel):
    comparables: list[ComparablePrice] = Field(max_length=5)
    options: list[PriceOption] = Field(min_length=2, max_length=3)
    recommended_option: str = Field(min_length=2, max_length=60)
    recommendation_reason: str = Field(min_length=10, max_length=500)
    willingness_to_pay: str = Field(min_length=10, max_length=500)
    price_test: str = Field(min_length=20, max_length=600)
    terms: str = Field(min_length=200, max_length=3000)
    privacy: str = Field(min_length=200, max_length=3000)
    refund: str = Field(min_length=100, max_length=2000)

    @field_validator("terms", "privacy", "refund")
    @classmethod
    def plain_policy(cls, text: str) -> str:
        return _plain(text)

    @model_validator(mode="after")
    def recommended_is_an_option(self) -> "PricingOutput":
        names = {o.name.strip().lower() for o in self.options}
        if self.recommended_option.strip().lower() not in names:
            raise ValueError("recommended_option must be the exact name of one of the options.")
        return self


def set_price_and_policies(
    adapter: LLMAdapter,
    brief: FounderBrief,
    option: Opportunity,
    validate_result: str,
    pages: list[FetchedPage],
    excerpts: list[str],
) -> AgentResult[PricingOutput]:
    user_prompt = (
        f"{brief.as_prompt()}\n\n"
        "APPROVED OPTION\n"
        f"Title: {option.title}\nCustomer: {option.customer}\n"
        f"Problem: {option.problem}\nProduct: {option.product}\n\n"
        f"VALIDATE RESULT (real demand recorded by the founder)\n{validate_result}\n\n"
        f"{format_sources(pages, excerpts)}"
    )
    return run_agent(
        adapter, prompt_name="pricing", user_prompt=user_prompt, response_model=PricingOutput
    )


def render_policy(label: str, text: str) -> str:
    return f"{label}\n\n{POLICY_NOTICE}\n\n{text.strip()}\n"
