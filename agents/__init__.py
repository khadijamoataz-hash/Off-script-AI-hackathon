"""Phase agents, one file per role (`agents/<role>.py`).

Each agent is a function that takes typed input, calls the local model once with the prompt in
`prompts/<role>.md`, and returns a typed result. Agents never write files, call tools that
leave the system, or log: `workflow.py` does that around them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

from llm import LLMAdapter

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

TOut = TypeVar("TOut", bound=BaseModel)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentResult(Generic[TOut]):
    """A typed agent output plus what it cost to produce."""

    output: TOut
    model: str
    prompt_tokens: int
    completion_tokens: int
    attempts: int

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def run_agent(
    adapter: LLMAdapter, *, prompt_name: str, user_prompt: str, response_model: type[TOut],
    temperature: float | None = None,
) -> AgentResult[TOut]:
    response = adapter.generate_structured(
        system_prompt=load_prompt(prompt_name),
        user_prompt=user_prompt,
        response_model=response_model,
        temperature=temperature,
    )
    return AgentResult(
        output=response.parsed,
        model=response.model_used,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        attempts=response.attempts,
    )
