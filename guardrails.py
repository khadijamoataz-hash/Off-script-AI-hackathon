"""Hard-coded safety Policy Engine ("must never" rules in code + classifier pass).

Enforces six non-bypassable rules across prompts, agent outputs, and tool calls:
1. INCOME_GUARANTEE: No income, revenue, profit, ROI, or success guarantees.
2. FAKE_SOCIAL_PROOF: No fabricated reviews, testimonials, ratings, or fake sign-ups/users.
3. IMPERSONATION: Never impersonate the founder, customers, real people, or external brands.
4. MONEY_HANDLING: Never hold, charge, transfer, or spend money / buy ads / process payments.
5. CREDENTIAL_OR_CARD_STORAGE: Never accept, store, or log passwords, API secrets, or payment card details.
6. UNAPPROVED_EXTERNAL_ACTION: Never publish, send, deploy, or spend directly without an Outbox approval token.

Fail-closed guarantee:
- If any deterministic rule matches -> BLOCK, log to DecisionLog, prompt founder.
- If a classifier pass is configured and raises an error, times out, or returns an uncertain/unsafe verdict -> BLOCK, log, prompt founder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from decision_log import DecisionLog


class PolicyCategory(str, Enum):
    CLEAN = "CLEAN"
    INCOME_GUARANTEE = "INCOME_GUARANTEE"
    FAKE_SOCIAL_PROOF = "FAKE_SOCIAL_PROOF"
    IMPERSONATION = "IMPERSONATION"
    MONEY_HANDLING = "MONEY_HANDLING"
    CREDENTIAL_OR_CARD_STORAGE = "CREDENTIAL_OR_CARD_STORAGE"
    UNAPPROVED_EXTERNAL_ACTION = "UNAPPROVED_EXTERNAL_ACTION"
    CLASSIFIER_UNCERTAIN_OR_UNSAFE = "CLASSIFIER_UNCERTAIN_OR_UNSAFE"


@dataclass(frozen=True)
class _CompiledRule:
    rule_id: str
    category: PolicyCategory
    description: str
    founder_prompt: str
    patterns: tuple[re.Pattern[str], ...]


def _compile_patterns(*exprs: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(expr, re.IGNORECASE | re.DOTALL) for expr in exprs)


HARD_RULES: tuple[_CompiledRule, ...] = (
    _CompiledRule(
        rule_id="POL-001-INCOME-GUARANTEE",
        category=PolicyCategory.INCOME_GUARANTEE,
        description="Blocks income, revenue, profit, or business success guarantees.",
        founder_prompt=(
            "Foundry blocked an income/success guarantee. Reframe this as a testable "
            "hypothesis or ASSUMPTION requiring founder validation?"
        ),
        patterns=_compile_patterns(
            r"\bguarantee(?:d|s|ing)?\b.{0,60}\b(?:earn|make|revenue|income|profit|sales|mrr|arr|\$\s*\d+|\d+\s*k\b|customers|success)\b",
            r"\b(?:earn|make|generate|hit)\b.{0,40}\b(?:\$\s*\d+|\d+\s*k\b|revenue|income|profit)\b.{0,40}\bguarantee(?:d|s)?\b",
            r"\b100%\s*(?:guaranteed|certain|sure)\s*(?:to\s*)?(?:succeed|profit|earn|make money|sell)\b",
            r"\brisk[-\s]?free\s+(?:income|profit|return|money|revenue)\b",
            r"\bpromise\s+(?:you\s+will|to)\s+(?:earn|make|profit)\b",
        ),
    ),
    _CompiledRule(
        rule_id="POL-002-FAKE-SOCIAL-PROOF",
        category=PolicyCategory.FAKE_SOCIAL_PROOF,
        description="Blocks fabricated customer reviews, testimonials, ratings, or fake sign-ups.",
        founder_prompt=(
            "Foundry blocked creation of fabricated social proof (reviews/testimonials/sign-ups). "
            "Would you like an ethical customer interview or feedback request template instead?"
        ),
        patterns=_compile_patterns(
            r"\b(?:write|generate|create|draft|fabricate|invent|make\s+up|fake|seed)\b.{0,50}\b(?:\d+\s+)?(?:fake\s+|glowing\s+|5[-\s]?star\s+|positive\s+)?(?:customer\s+|user\s+|client\s+|product\s+)?(?:reviews?|testimonials?|endorsements?|ratings?|sign[-\s]?ups?|waitlist\s+users?)\b",
            r"\bfake\s+(?:reviews?|testimonials?|customers?|sign[-\s]?ups?|traction|users?)\b",
            r"\b⭐⭐⭐⭐⭐\b.{0,80}\b(?:verified\s+buyer|changed\s+my\s+life)\b",
        ),
    ),
    _CompiledRule(
        rule_id="POL-003-IMPERSONATION",
        category=PolicyCategory.IMPERSONATION,
        description="Blocks impersonating the founder, customers, real individuals, or external brands.",
        founder_prompt=(
            "Foundry blocked impersonation of a founder, person, or brand. "
            "Drafts must clearly stay in the Outbox for the founder to review and send under their own identity."
        ),
        patterns=_compile_patterns(
            r"\b(?:reply|respond|send|write|sign|post|act|speak)\s+as\s+(?:the\s+founder|me\s+directly|ceo)\b",
            r"\b(?:pretend|pose|act|claim)\s+to\s+be\b.{0,50}",
            r"\bimpersonat(?:e|ing|ion)\b",
            r"\bfrom\s+official\s+[a-z0-9_-]+\s+support\b",
            r"\bi\s+am\s+the\s+official\s+representative\s+of\b",
        ),
    ),
    _CompiledRule(
        rule_id="POL-004-MONEY-HANDLING",
        category=PolicyCategory.MONEY_HANDLING,
        description="Blocks holding, moving, charging, or spending money (including buying ads).",
        founder_prompt=(
            "Foundry never holds, moves, charges, or spends money. "
            "Review any pricing or ad budget plan manually outside the agent runtime."
        ),
        patterns=_compile_patterns(
            r"\b(?:pay\s+for|buy|purchase|run\s+paid|spend\s+(?:money|\$\d+)|charge\s+(?:the\s+)?card|wire\s+funds|transfer\s+(?:money|funds)|process\s+payment|collect\s+payment)\b.{0,40}\b(?:ads?|advertising|campaign|stripe|paypal|card|account|now|immediately)?\b",
            r"\b(?:pay|spend)\b.{0,25}\b(?:ads?|google\s+ads|meta\s+ads|facebook\s+ads|tiktok\s+ads)\b",
            r"\b(?:hold|escrow|custody|withdraw|deposit)\s+(?:customer\s+|founder\s+)?(?:funds|money|payments)\b",
        ),
    ),
    _CompiledRule(
        rule_id="POL-005-CREDENTIALS-AND-CARDS",
        category=PolicyCategory.CREDENTIAL_OR_CARD_STORAGE,
        description="Blocks storing or handling passwords, secret tokens, CVVs, or payment card numbers.",
        founder_prompt=(
            "Foundry blocked storage or handling of credentials/payment card details. "
            "Never paste cards or passwords into Foundry; perform logins and billing directly yourself."
        ),
        patterns=_compile_patterns(
            r"\b(?:save|store|remember|keep|record|log|cache)\s+(?:my\s+|this\s+|the\s+)?(?:credit\s+card|debit\s+card|card\s+(?:number|details|info)|cvv|cvc|password|passphrase|login\s+credentials|private\s+key|secret\s+key|bank\s+account)\b",
            r"\b(?:\d{4}[-\s]?){3}\d{4}\b",  # 16-digit card pattern
            r"\b(?:cvv|cvc)\s*[:=]?\s*\d{3,4}\b",
            r"\b(?:my\s+password\s+is|password\s*[:=]\s*\S{4,})\b",
        ),
    ),
    _CompiledRule(
        rule_id="POL-006-UNAPPROVED-EXTERNAL-ACTION",
        category=PolicyCategory.UNAPPROVED_EXTERNAL_ACTION,
        description="Blocks direct publishing, emailing, deploying, or spending bypassing the Outbox approval queue.",
        founder_prompt=(
            "External actions cannot run immediately or bypass founder approval. "
            "Queue this as a draft in the Outbox for founder review and signed single-use approval?"
        ),
        patterns=_compile_patterns(
            r"\b(?:post|publish|tweet|send|blast|email|dm|deploy|launch)\s+(?:this|it|the\s+\w+|all\s+\w+)\s+(?:right\s+now|now|immediately|automatically|without\s+(?:asking|approval|review))\b",
            r"\bwithout\s+(?:my\s+|founder\s+)?(?:approval|permission|consent|review)\b",
            r"\bbypass\s+(?:the\s+)?(?:outbox|approval|gate|founder)\b",
            r"\bauto[-\s]?(?:send|post|publish|spend)\b",
        ),
    ),
)

# Tool names that are permanently forbidden or require a verified Outbox approval token
FORBIDDEN_MONEY_OR_SECRET_TOOLS = frozenset(
    {
        "charge_card",
        "process_payment",
        "transfer_funds",
        "buy_ads",
        "pay_ads",
        "store_credentials",
        "save_card",
        "store_password",
    }
)

EXTERNAL_EXECUTION_TOOLS = frozenset(
    {
        "publish_post",
        "send_email",
        "deploy_site",
        "publish_listing",
        "send_support_reply",
        "execute_outbox_action",
    }
)


# Internal tools that touch nothing outside Foundry. Every tool an agent may call must be listed in
# one of the three sets in this module; anything else is blocked (allowlist, fail closed).
INTERNAL_TOOLS = frozenset(
    {
        "web_search",
        "fetch_url",
        "read_file",
        "write_file",
        "list_files",
        "run_sandbox_tests",
        "add_claim",
        "list_claims",
        "save_phase_artifact",
        "propose_action",
    }
)


class ClassifierVerdict(BaseModel):
    """Structured output returned by an optional model-based safety classifier pass."""

    safe: bool
    confident: bool = True
    category: PolicyCategory = PolicyCategory.CLEAN
    reason: str = ""


ClassifierCallable = Callable[[str, "PolicyContext"], ClassifierVerdict]


class PolicyContext(BaseModel):
    """Metadata describing what is being inspected by the Policy Engine."""

    project_id: str = "default"
    phase: str = "system"
    surface: str = Field(
        default="agent_output",
        description="input_prompt | agent_output | tool_call",
    )
    tool_name: str | None = None
    has_valid_approval_token: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    allowed: bool
    category: PolicyCategory
    rule_id: str | None = None
    reason: str
    founder_prompt: str | None = None


class PolicyViolationError(RuntimeError):
    """Raised when fail-closed enforcement blocks an unsafe action or output."""

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class PolicyEngine:
    """Deterministic + optional classifier Policy Engine that fails closed and logs every block."""

    def __init__(
        self,
        decision_log: DecisionLog | None = None,
        classifier: ClassifierCallable | None = None,
    ) -> None:
        self.decision_log = decision_log
        self.classifier = classifier

    def evaluate_text(
        self, text: str, context: PolicyContext | None = None
    ) -> PolicyDecision:
        ctx = context or PolicyContext()
        if text is None:
            return self._block(
                ctx=ctx,
                category=PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE,
                rule_id="POL-000-NULL-INPUT",
                reason="Blocked null content (fail-closed).",
                founder_prompt="Empty/null payload was blocked by fail-closed policy. Please review.",
            )

        # 1. Deterministic hard-rule pass (cannot be overridden by any model)
        for rule in HARD_RULES:
            for pattern in rule.patterns:
                match = pattern.search(text)
                if match:
                    snippet = match.group(0)[:80].strip()
                    return self._block(
                        ctx=ctx,
                        category=rule.category,
                        rule_id=rule.rule_id,
                        reason=f"{rule.description} Matched: '{snippet}'",
                        founder_prompt=rule.founder_prompt,
                    )

        # 2. Optional model-based classifier pass (fail-closed on uncertainty or exception)
        if self.classifier is not None:
            try:
                verdict = self.classifier(text, ctx)
            except Exception as exc:  # noqa: BLE001 - intentional fail-closed boundary
                return self._block(
                    ctx=ctx,
                    category=PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE,
                    rule_id="POL-900-CLASSIFIER-ERROR",
                    reason=f"Safety classifier failed ({type(exc).__name__}); failing closed.",
                    founder_prompt=(
                        "Foundry's safety classifier could not verify this content and failed closed. "
                        "Founder review required before continuing."
                    ),
                )

            if not verdict.safe or not verdict.confident:
                cat = (
                    verdict.category
                    if verdict.category != PolicyCategory.CLEAN
                    else PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE
                )
                return self._block(
                    ctx=ctx,
                    category=cat,
                    rule_id="POL-901-CLASSIFIER-BLOCK",
                    reason=verdict.reason or "Safety classifier flagged content or reported low confidence.",
                    founder_prompt=(
                        "Foundry's safety classifier flagged this item (or was uncertain). "
                        "Please inspect and explicitly decide how to proceed."
                    ),
                )

        return PolicyDecision(
            allowed=True,
            category=PolicyCategory.CLEAN,
            rule_id=None,
            reason="Passed deterministic safety rules and classifier checks.",
            founder_prompt=None,
        )

    def evaluate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        project_id: str = "default",
        phase: str = "system",
        has_valid_approval_token: bool = False,
    ) -> PolicyDecision:
        normalized_tool = (tool_name or "").strip().lower()
        args = arguments or {}
        ctx = PolicyContext(
            project_id=project_id,
            phase=phase,
            surface="tool_call",
            tool_name=normalized_tool,
            has_valid_approval_token=has_valid_approval_token,
        )

        if not normalized_tool:
            return self._block(
                ctx=ctx,
                category=PolicyCategory.CLASSIFIER_UNCERTAIN_OR_UNSAFE,
                rule_id="POL-000-EMPTY-TOOL",
                reason="Tool call missing tool_name; failing closed.",
                founder_prompt="An unnamed tool call was blocked. Please inspect agent output.",
            )

        if normalized_tool in FORBIDDEN_MONEY_OR_SECRET_TOOLS:
            cat = (
                PolicyCategory.CREDENTIAL_OR_CARD_STORAGE
                if any(k in normalized_tool for k in ("card", "password", "credential"))
                and "charge" not in normalized_tool
                else PolicyCategory.MONEY_HANDLING
            )
            return self._block(
                ctx=ctx,
                category=cat,
                rule_id="POL-007-FORBIDDEN-TOOL",
                reason=f"Tool '{normalized_tool}' is permanently prohibited by Foundry safety architecture.",
                founder_prompt=(
                    f"Agent attempted prohibited tool '{normalized_tool}'. "
                    "Foundry never handles money, ads spend, or stored credentials."
                ),
            )

        if normalized_tool in EXTERNAL_EXECUTION_TOOLS and not has_valid_approval_token:
            return self._block(
                ctx=ctx,
                category=PolicyCategory.UNAPPROVED_EXTERNAL_ACTION,
                rule_id="POL-008-TOOL-NEEDS-APPROVAL-TOKEN",
                reason=(
                    f"External tool '{normalized_tool}' blocked: missing verified single-use founder approval token."
                ),
                founder_prompt=(
                    f"Action '{normalized_tool}' requires explicit founder approval in the Outbox before execution."
                ),
            )

        if (
            normalized_tool not in INTERNAL_TOOLS
            and normalized_tool not in EXTERNAL_EXECUTION_TOOLS
        ):
            return self._block(
                ctx=ctx,
                category=PolicyCategory.UNAPPROVED_EXTERNAL_ACTION,
                rule_id="POL-009-UNKNOWN-TOOL",
                reason=f"Tool '{normalized_tool}' is not on the allowlist; failing closed.",
                founder_prompt=(
                    f"An agent tried to call an unregistered tool '{normalized_tool}'. "
                    "Only allowlisted tools can run; external actions must go through the Outbox."
                ),
            )

        # Also inspect serialized arguments against all text rules (e.g., card numbers, impersonation, fake reviews)
        serialized_args = " ".join(f"{k}={v}" for k, v in sorted(args.items()))
        return self.evaluate_text(serialized_args, context=ctx)

    def enforce_text(self, text: str, context: PolicyContext | None = None) -> PolicyDecision:
        decision = self.evaluate_text(text, context=context)
        if not decision.allowed:
            raise PolicyViolationError(decision)
        return decision

    def _block(
        self,
        *,
        ctx: PolicyContext,
        category: PolicyCategory,
        rule_id: str,
        reason: str,
        founder_prompt: str,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            allowed=False,
            category=category,
            rule_id=rule_id,
            reason=reason,
            founder_prompt=founder_prompt,
        )
        if self.decision_log is not None:
            valid_phases = {
                "system",
                "discover",
                "validate",
                "build",
                "commercial",
                "launch",
                "operate",
            }
            safe_phase = ctx.phase.lower() if ctx.phase.lower() in valid_phases else "system"
            self.decision_log.append(
                project_id=ctx.project_id,
                phase=safe_phase,
                decision=f"[{rule_id}] Blocked ({category.value}): {reason}",
                action=f"block_{category.value.lower()}",
                actor="policy_engine",
                outcome="blocked",
                metadata={
                    "surface": ctx.surface,
                    "tool_name": ctx.tool_name,
                    "category": category.value,
                    "rule_id": rule_id,
                    "founder_prompt": founder_prompt,
                },
            )
        return decision
