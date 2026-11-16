"""Common LLM interface over Ollama (default open-weight local runtime) and optional providers.

Features:
- Default path uses local Ollama (`http://localhost:11434`) with zero API keys and zero paid services.
- Optional OpenAI-compatible HTTP endpoint behind `FOUNDRY_LLM_PROVIDER=openai_compat`.
- `OfflineDeterministicProvider` for tests and CI ONLY. It is never selected automatically: it must
  be requested with `FOUNDRY_LLM_PROVIDER=offline` (or passed explicitly), and is tagged
  `provider_used="offline_deterministic"`. If Ollama is unreachable in the default mode, calls fail.
- Structured output generation validated against Pydantic v2 models + JSON Schema, with automatic
  JSON extraction and repair retries when a model emits malformed JSON or misses required fields.
- Model-backed safety classifier builder (`build_llm_safety_classifier`) that plugs directly into
  `PolicyEngine` (with fail-closed semantics).
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import httpx
import jsonschema
from pydantic import BaseModel, ValidationError

from guardrails import ClassifierCallable, ClassifierVerdict, PolicyCategory, PolicyContext

TModel = TypeVar("TModel", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when an LLM provider fails or structured output cannot be repaired within max retries."""


@dataclass
class LLMConfig:
    provider: str = field(
        default_factory=lambda: os.environ.get("FOUNDRY_LLM_PROVIDER", "auto")
    )  # auto | ollama | openai_compat | offline
    ollama_url: str = field(
        default_factory=lambda: os.environ.get("FOUNDRY_OLLAMA_URL", "http://localhost:11434")
    )
    model: str = field(
        default_factory=lambda: os.environ.get("FOUNDRY_MODEL", "qwen2.5:7b")
    )
    openai_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "FOUNDRY_OPENAI_BASE_URL", "http://localhost:11434/v1"
        )
    )
    openai_api_key: str = field(
        default_factory=lambda: os.environ.get("FOUNDRY_OPENAI_API_KEY", "")
    )
    timeout_seconds: float = 45.0
    max_repair_retries: int = 2
    temperature: float = 0.2


@dataclass(frozen=True)
class LLMResponse(Generic[TModel]):
    parsed: TModel
    raw_text: str
    provider_used: str
    model_used: str
    attempts: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0  # USD. Local open-weight models cost 0; no paid API is ever called.


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract a JSON object from plain JSON, fenced ```json blocks, or surrounding prose."""
    cleaned = raw_text.strip()
    if not cleaned:
        raise ValueError("Empty response text; expected JSON object.")

    # 1. Direct parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2. Markdown code fence ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence_match:
        parsed = json.loads(fence_match.group(1))
        if isinstance(parsed, dict):
            return parsed

    # 3. Outermost balanced '{' ... '}'
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = cleaned[first_brace : last_brace + 1]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Could not locate a valid JSON object in LLM response.")


class LLMProvider(ABC):
    """Abstract base provider for chat completions."""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend is reachable."""

    @abstractmethod
    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, str]:
        """Return `(raw_text, model_identifier)`."""


class OllamaProvider(LLMProvider):
    """Default zero-key, zero-cost open-weight local LLM client using Ollama's `/api/chat`."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        timeout_seconds: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def has_model(self) -> bool:
        """Return True if the configured model has finished downloading into Ollama."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            resp.raise_for_status()
            names = {str(m.get("name", "")) for m in resp.json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            return False
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return wanted in names

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": temperature},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_schema is not None:
            payload["format"] = json_schema

        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed ({self.base_url}): {exc}") from exc

        self.last_usage = {
            "prompt_tokens": int(data.get("prompt_eval_count") or 0),
            "completion_tokens": int(data.get("eval_count") or 0),
        }
        content = data.get("message", {}).get("content", "")
        return str(content), str(data.get("model", self.model))


class OpenAICompatibleProvider(LLMProvider):
    """Optional flag-gated provider for vLLM / llama.cpp / OpenAI-compatible endpoints."""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = httpx.get(f"{self.base_url}/models", headers=headers, timeout=2.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return str(content), str(data.get("model", self.model))
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(f"OpenAI-compatible request failed: {exc}") from exc


class OfflineDeterministicProvider(LLMProvider):
    """Schema-aware offline fallback provider clearly labeled `offline_deterministic`.

    Allows CI, unit/e2e tests, and clean machines without Ollama currently running to execute
    structured agent schemas deterministically without pretending to be a live neural model.
    """

    name = "offline_deterministic"

    def __init__(self, scripted_responses: list[str] | None = None) -> None:
        self._scripted = list(scripted_responses or [])

    def is_available(self) -> bool:
        return True

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, str]:
        if self._scripted:
            return self._scripted.pop(0), "offline-scripted"

        if json_schema is not None:
            synthesized = self._synthesize_from_schema(json_schema, user_prompt=user_prompt)
            return json.dumps(synthesized), "offline-schema-synthesizer"

        return (
            json.dumps({"summary": f"[OFFLINE_FALLBACK] Processed prompt ({len(user_prompt)} chars)."}),
            "offline-schema-synthesizer",
        )

    def _synthesize_from_schema(
        self,
        schema: dict[str, Any],
        *,
        user_prompt: str,
        defs: dict[str, Any] | None = None,
        field_name: str = "",
    ) -> Any:
        root_defs = defs if defs is not None else schema.get("$defs", {})

        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            return self._synthesize_from_schema(
                root_defs[ref_name], user_prompt=user_prompt, defs=root_defs, field_name=field_name
            )

        if "anyOf" in schema:
            non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
            chosen = non_null[0] if non_null else schema["anyOf"][0]
            return self._synthesize_from_schema(
                chosen, user_prompt=user_prompt, defs=root_defs, field_name=field_name
            )

        if "default" in schema:
            return schema["default"]

        if "enum" in schema and schema["enum"]:
            return schema["enum"][0]

        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            obj: dict[str, Any] = {}
            props = schema.get("properties", {})
            for prop_key, prop_schema in props.items():
                obj[prop_key] = self._synthesize_from_schema(
                    prop_schema,
                    user_prompt=user_prompt,
                    defs=root_defs,
                    field_name=prop_key,
                )
            return obj

        if schema_type == "array":
            item_schema = schema.get("items", {"type": "string"})
            min_items = max(1, int(schema.get("minItems", 1)))
            return [
                self._synthesize_from_schema(
                    item_schema,
                    user_prompt=user_prompt,
                    defs=root_defs,
                    field_name=field_name,
                )
                for _ in range(min_items)
            ]

        if schema_type == "boolean":
            # Safety classifier defaults to safe=True, confident=True on benign text
            return True

        if schema_type == "integer":
            return int(schema.get("minimum", 1))

        if schema_type == "number":
            return float(schema.get("minimum", 1.0))

        if schema_type == "null":
            return None

        # Contextual string synthesis based on prompt snippet
        idea_line = user_prompt.strip().splitlines()[0][:80] if user_prompt.strip() else "digital product"
        label = field_name.replace("_", " ").strip() or "detail"
        return f"{label.capitalize()} for: {idea_line} [ASSUMPTION]"


class LLMAdapter:
    """Unified LLM adapter with provider resolution and schema-validated retry/repair."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.config = config or LLMConfig()
        self._explicit_provider = provider

    def resolve_provider(self) -> LLMProvider:
        if self._explicit_provider is not None:
            return self._explicit_provider

        mode = self.config.provider.lower().strip()
        if mode == "ollama":
            return OllamaProvider(
                base_url=self.config.ollama_url,
                model=self.config.model,
                timeout_seconds=self.config.timeout_seconds,
            )
        if mode == "openai_compat":
            return OpenAICompatibleProvider(
                base_url=self.config.openai_base_url,
                model=self.config.model,
                api_key=self.config.openai_api_key,
                timeout_seconds=self.config.timeout_seconds,
            )
        if mode == "offline":
            return OfflineDeterministicProvider()

        # Default "auto": always the real local Ollama model. There is deliberately NO silent
        # fallback to the offline synthesizer: if Ollama is unreachable, calls raise LLMError so a
        # mock can never be mistaken for a working model. Offline mode must be requested
        # explicitly (FOUNDRY_LLM_PROVIDER=offline) and is for tests/CI only.
        return OllamaProvider(
            base_url=self.config.ollama_url,
            model=self.config.model,
            timeout_seconds=self.config.timeout_seconds,
        )

    def runtime_status(self) -> dict[str, Any]:
        """Report honest live vs offline fallback LLM status for UI and CLI diagnostics."""
        ollama = OllamaProvider(
            base_url=self.config.ollama_url,
            model=self.config.model,
            timeout_seconds=self.config.timeout_seconds,
        )
        ollama_live = ollama.is_available()
        active = self.resolve_provider()
        model_ready = ollama.has_model() if ollama_live else False
        return {
            "configured_mode": self.config.provider,
            "active_provider": active.name,
            "configured_model": self.config.model,
            "ollama_url": self.config.ollama_url,
            "ollama_reachable": ollama_live,
            "model_downloaded": model_ready,
            "is_mock": active.name == OfflineDeterministicProvider.name,
            "ready": (active.name == OfflineDeterministicProvider.name)
            or (ollama_live and model_ready),
        }

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[TModel],
        temperature: float | None = None,
    ) -> LLMResponse[TModel]:
        """Generate a response strictly conforming to `response_model`, with automatic repair retries."""
        provider = self.resolve_provider()
        schema = response_model.model_json_schema()
        temp = self.config.temperature if temperature is None else temperature

        augmented_system = (
            f"{system_prompt.strip()}\n\n"
            "IMPORTANT: Respond ONLY with a valid JSON object conforming to this JSON Schema:\n"
            f"{json.dumps(schema)}"
        )

        current_user_prompt = user_prompt
        last_error: str = ""
        last_raw: str = ""
        total_attempts = 1 + max(0, self.config.max_repair_retries)
        prompt_tokens = 0
        completion_tokens = 0

        for attempt in range(1, total_attempts + 1):
            raw_text, model_used = provider.chat(
                system_prompt=augmented_system,
                user_prompt=current_user_prompt,
                json_schema=schema,
                temperature=temp,
            )
            last_raw = raw_text
            usage = getattr(provider, "last_usage", None) or {}
            prompt_tokens += int(usage.get("prompt_tokens", 0))
            completion_tokens += int(usage.get("completion_tokens", 0))

            try:
                data = extract_json_object(raw_text)
                jsonschema.validate(instance=data, schema=schema)
                parsed = response_model.model_validate(data)
                return LLMResponse(
                    parsed=parsed,
                    raw_text=raw_text,
                    provider_used=provider.name,
                    model_used=model_used,
                    attempts=attempt,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            except (ValueError, jsonschema.ValidationError, ValidationError) as exc:
                last_error = str(exc)
                current_user_prompt = (
                    f"{user_prompt}\n\n"
                    "--- SCHEMA REPAIR REQUIRED ---\n"
                    f"Your previous output failed validation:\n{last_error}\n"
                    f"Previous raw output:\n{raw_text[:1000]}\n"
                    "Return ONLY the corrected JSON object matching the required schema."
                )

        raise LLMError(
            f"Structured output validation failed after {total_attempts} attempt(s): {last_error} "
            f"(last output: {last_raw[:300]})"
        )


SAFETY_CLASSIFIER_SYSTEM_PROMPT = """You are Foundry's fail-closed safety classifier.
Inspect the text and determine whether it violates ANY of Foundry's hard safety boundaries:
- INCOME_GUARANTEE: promises or guarantees of income, revenue, profit, or guaranteed business success
- FAKE_SOCIAL_PROOF: fabricated reviews, testimonials, ratings, or fake users/sign-ups
- IMPERSONATION: pretending to be the founder, a customer, a real person, or an external brand
- MONEY_HANDLING: charging cards, holding/moving funds, or spending money / buying ads
- CREDENTIAL_OR_CARD_STORAGE: storing or transmitting passwords, API secrets, or payment card numbers
- UNAPPROVED_EXTERNAL_ACTION: attempting to publish, email, deploy, or spend immediately without approval

If clean, set safe=true, confident=true, category="CLEAN".
If unsafe or ambiguous, set safe=false (or confident=false) and specify the matching category and reason."""


def build_llm_safety_classifier(adapter: LLMAdapter) -> ClassifierCallable:
    """Create a model-backed `ClassifierCallable` for `PolicyEngine`."""

    def _classify(text: str, ctx: PolicyContext) -> ClassifierVerdict:
        if not text.strip():
            return ClassifierVerdict(safe=True, confident=True, category=PolicyCategory.CLEAN)
        resp = adapter.generate_structured(
            system_prompt=SAFETY_CLASSIFIER_SYSTEM_PROMPT,
            user_prompt=(
                f"Surface: {ctx.surface}\nPhase: {ctx.phase}\nTool: {ctx.tool_name or 'none'}\n\n"
                f"Content to inspect:\n{text}"
            ),
            response_model=ClassifierVerdict,
            temperature=0.0,
        )
        if resp.model_used == "offline-schema-synthesizer":
            # The offline synthesizer cannot assess content (it would answer safe=True to
            # anything). Raising here makes PolicyEngine fail closed instead of fail open.
            raise LLMError("Safety classifier has no real model available; failing closed.")
        return resp.parsed

    return _classify
