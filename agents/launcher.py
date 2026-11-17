"""Launcher agent: drafts the store page and launch posts.

The schema has no field for testimonials, counters, prices or deadlines, so those cannot be
expressed as structured content; the renderer inserts fixed placeholder slots instead.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents import AgentResult, run_agent
from agents.intake import FounderBrief
from agents.researcher import Opportunity
from llm import LLMAdapter

TESTIMONIAL_SLOT = "[Real customer quote - collect after launch]"
PRICE_SLOT = "[Price - set by the founder in Commercial setup]"


class FaqItem(BaseModel):
    question: str = Field(min_length=5, max_length=200)
    answer: str = Field(min_length=5, max_length=500)


class StorePage(BaseModel):
    headline: str = Field(min_length=5, max_length=120)
    subheadline: str = Field(min_length=10, max_length=300)
    what_you_get: list[str] = Field(min_length=3, max_length=5)
    who_it_is_for: str = Field(min_length=10, max_length=300)
    who_it_is_not_for: str = Field(min_length=10, max_length=300)
    faq: list[FaqItem] = Field(min_length=2, max_length=3)


class LaunchPost(BaseModel):
    channel: Literal["community", "social", "email"]
    text: str = Field(min_length=40, max_length=900)


class LaunchDrafts(BaseModel):
    store_page: StorePage
    posts: list[LaunchPost] = Field(min_length=2, max_length=3)


def render_store_page(page: StorePage) -> str:
    items = "\n".join(f"- {item}" for item in page.what_you_get)
    faq = "\n\n".join(f"**{item.question}**\n{item.answer}" for item in page.faq)
    return (
        f"# {page.headline}\n\n{page.subheadline}\n\n"
        f"## What you get\n\n{items}\n\n"
        f"## Who it is for\n\n{page.who_it_is_for}\n\n"
        f"## Who it is not for\n\n{page.who_it_is_not_for}\n\n"
        f"## Price\n\n{PRICE_SLOT}\n\n"
        f"## What customers say\n\n{TESTIMONIAL_SLOT}\n\n"
        f"## Questions\n\n{faq}\n"
    )


def draft_launch(
    adapter: LLMAdapter, brief: FounderBrief, option: Opportunity
) -> AgentResult[LaunchDrafts]:
    user_prompt = (
        f"{brief.as_prompt()}\n\n"
        "OPTION BEING TESTED\n"
        f"Title: {option.title}\nCustomer: {option.customer}\n"
        f"Problem: {option.problem}\nProduct: {option.product}"
    )
    return run_agent(
        adapter, prompt_name="launcher", user_prompt=user_prompt, response_model=LaunchDrafts
    )
