"""Step runner for the phases that are built: Phase 1 (Discover), Phase 2 (Validate),
Phase 4 (Commercial setup: price options and policy drafts) and the Phase 5 (Launch)
drafting, approval and MOCKED delivery steps.

A step runs its agents, records evidence, writes its output file to `outputs/`, logs every
action, and then stops at a gate. Nothing moves on until `decide_step` or `decide_phase_gate`
is called by a founder action in the UI or CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from agents.critic import CriticOutput, critique
from agents.launcher import LaunchDrafts, draft_launch, render_store_page
from agents.pricing import POLICIES, PricingOutput, render_policy, set_price_and_policies
from agents.strategist import CANVAS_BOXES, StrategyOutput, design_business, render_landing_page
from agents.intake import EXCLUDED_LABELS, BriefScreen, FounderBrief, screen_brief
from agents.researcher import (
    InterviewScript,
    Opportunity,
    ResearchOutput,
    draft_interview_script,
    research_opportunities,
)
from approval import ActionStatus, OutboxError, ProposedAction
from delivery import MockDeliveryService, run_mock_delivery_test
from evidence import Claim, ClaimType, FetchedPage
from guardrails import (
    ClassifierCallable,
    PolicyCategory,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyViolationError,
)
from gates import GateCheckResult, GateOutcome, PhaseName, ProjectStatus
from llm import LLMAdapter, build_llm_safety_classifier
from orchestrator import Orchestrator, OrchestratorError, ProjectRecord
from tools import SearchResult, ToolError, fetch_url, web_search

_INDEX_LOCK = threading.Lock()

MAX_PAGES = 4
EXCERPT_CHARS = 1400
STEP_CHOICES = ("continue", "repeat", "go_back", "stop")

SearchFn = Callable[[str], list[SearchResult]]
FetchFn = Callable[..., FetchedPage]


class WorkflowError(RuntimeError):
    """The requested step or decision is not allowed in the project's current state."""


def outputs_dir() -> Path:
    return Path(os.environ.get("FOUNDRY_OUTPUTS_DIR", "outputs"))


def select_excerpt(text: str, keywords: set[str], limit: int = EXCERPT_CHARS) -> str:
    """Pick the lines of a page that share the most words with the brief, in page order.

    Lines are passed to the model unchanged so that a quote copied from the excerpt can be
    found again in the stored page text.
    """
    lines = [line for line in text.splitlines() if len(line) >= 40]
    if not lines:
        return text[:limit]
    scored = []
    for index, line in enumerate(lines):
        words = set(re.findall(r"[a-z]{4,}", line.lower()))
        scored.append((len(words & keywords), index))
    chosen: set[int] = set()
    used = 0
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        line = lines[index][:600]
        if used + len(line) > limit:
            continue
        chosen.add(index)
        used += len(line)
    return "\n".join(lines[i][:600] for i in sorted(chosen))


def discover_recommendation(critic: CriticOutput, sourced_facts: int) -> tuple[str, str]:
    """The system's recommendation at the Discover gate: continue, repeat or stop."""
    if critic.recommendation == "stop":
        return "stop", f"The critic recommends dropping this idea: {critic.reason}"
    if sourced_facts == 0:
        return (
            "repeat",
            "No claim is backed by a fetched source; everything is an ASSUMPTION. "
            "Add source URLs or interview notes and run the step again before going on.",
        )
    if critic.recommendation == "go":
        return "continue", critic.reason
    if critic.recommendation == "pivot":
        return "repeat", f"The critic suggests testing a changed version first: {critic.reason}"
    return "repeat", critic.reason


class Workflow:
    def __init__(
        self,
        orchestrator: Orchestrator,
        adapter: LLMAdapter | None = None,
        *,
        out_dir: Path | None = None,
        search: SearchFn | None = None,
        fetch: FetchFn | None = None,
        classifier: ClassifierCallable | None = None,
    ) -> None:
        self.orch = orchestrator
        self.ledger = orchestrator.evidence_ledger
        self.log = orchestrator.decision_log
        self.adapter = adapter or LLMAdapter()
        self.out_dir = out_dir or outputs_dir()
        # One guardrail engine for everything. The model classifier is always on and fails
        # closed; it reads founder requests and every draft entering the approval queue.
        self.engine = PolicyEngine(
            decision_log=self.log,
            classifier=classifier or build_llm_safety_classifier(self.adapter),
        )
        self.orch.outbox.policy_engine = self.engine
        # Approved exports go to the same folder as the other outputs.
        self.orch.outbox.export_dir = self.out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.search = search or web_search
        self.fetch = fetch or fetch_url
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_state "
                "(project_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
            )

    # ------------------------------------------------------------------ state

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.orch.db_path)

    def get_state(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM workflow_state WHERE project_id = ?", (project_id,)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def _set_state(self, project_id: str, **changes: Any) -> dict[str, Any]:
        state = self.get_state(project_id)
        state.update(changes)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workflow_state (project_id, state_json) VALUES (?, ?)",
                (project_id, json.dumps(state)),
            )
        return state

    def _add_usage(self, project_id: str, result: Any) -> None:
        usage = self.get_state(project_id).get("usage", {})
        usage["model_calls"] = usage.get("model_calls", 0) + result.attempts
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + result.prompt_tokens
        usage["completion_tokens"] = usage.get("completion_tokens", 0) + result.completion_tokens
        usage["model"] = result.model
        self._set_state(project_id, usage=usage)

    def _scrub(self, project_id: str, phase: str, text: str) -> str:
        """Keyword check on an agent-written document, line by line. A line that breaks a
        rule is removed and the removal is logged; the rest of the document is kept."""
        quiet = PolicyEngine()
        kept: list[str] = []
        for line in text.splitlines():
            context = PolicyContext(project_id=project_id, phase=phase, surface="agent_output")
            if line.strip() and not quiet.evaluate_text(line, context, use_classifier=False).allowed:
                decision = self.engine.evaluate_text(line, context, use_classifier=False)
                kept.append(f"[Removed by guardrails: {decision.category.value}]")
            else:
                kept.append(line)
        return "\n".join(kept)

    def _tool_allowed(self, project_id: str, tool: str, args: dict[str, Any]) -> bool:
        return self.engine.evaluate_tool_call(
            tool, args, project_id=project_id, phase=PhaseName.DISCOVER.value
        ).allowed

    def existing_out_dir(self, project_id: str) -> Path | None:
        """The project's output folder if it has written anything yet. Never assigns one, so
        merely viewing a project cannot claim the top of outputs/."""
        index_path = self.out_dir / "run_index.json"
        if not index_path.exists():
            return None
        for run in json.loads(index_path.read_text()).get("runs", []):
            if run["project_id"] == project_id:
                return self.out_dir / run["folder"]
        return None

    def project_out_dir(self, project_id: str, *, may_take_root: bool = True) -> Path:
        """Where a project's files go. The first project owns the top of `outputs/`, which is
        where the file names are expected; every later project gets `outputs/<project id>/`
        so runs never overwrite each other. `outputs/run_index.json` lists them."""
        with _INDEX_LOCK:
            index_path = self.out_dir / "run_index.json"
            index = json.loads(index_path.read_text()) if index_path.exists() else {"runs": []}
            for run in index["runs"]:
                if run["project_id"] == project_id:
                    return self.out_dir / run["folder"]
            root_taken = any(run["folder"] == "." for run in index["runs"])
            folder = "." if may_take_root and not root_taken else project_id
            project = self.orch.get_project(project_id)
            index["runs"].append({
                "project_id": project_id,
                "folder": folder,
                "idea": project.idea_summary if project else "",
                "started_at": project.created_at if project else "",
            })
            self.out_dir.mkdir(parents=True, exist_ok=True)
            index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
            return self.out_dir / folder

    def _write(self, project_id: str, name: str, content: str, *, may_take_root: bool = True) -> str:
        folder = self.project_out_dir(project_id, may_take_root=may_take_root)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return str(path)

    def _brief(self, project: ProjectRecord) -> FounderBrief:
        return FounderBrief(**project.brief)

    # ------------------------------------------------------------------ intake

    def check_brief_for_secrets(self, brief: FounderBrief) -> PolicyDecision:
        """Refuse a brief that contains a password, key or card number before it is stored."""
        return self.engine.evaluate_text(
            brief.as_prompt(), PolicyContext(surface="founder_request", phase="discover"),
            use_classifier=False,
        )

    def start_project(self, brief: FounderBrief, *, evidence_target: int = 10) -> ProjectRecord:
        check = self.check_brief_for_secrets(brief)
        if check.category == PolicyCategory.CREDENTIAL_OR_CARD_STORAGE:
            raise WorkflowError(check.founder_prompt or check.reason)
        name = " ".join(brief.idea.split()[:8])
        project = self.orch.create_project(
            name=name,
            idea_summary=brief.idea,
            target_customer=brief.target_market,
            validate_evidence_target=evidence_target,
            brief=brief.model_dump(),
        )
        self._set_state(project.id, stage="new", awaiting=None, usage={})
        return project

    # ------------------------------------------------------------------ step 1

    def run_discover_step1(self, project_id: str) -> dict[str, Any]:
        """Screen the brief, gather sources, find opportunities, criticise. Ends at a step gate."""
        project = self.orch._require_project(project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise WorkflowError(f"Project is {project.status.value}; no step can run.")
        brief = self._brief(project)
        started = time.monotonic()
        phase = PhaseName.DISCOVER.value
        self._set_state(
            project_id, stage="screening the brief", awaiting=None, error=None, refusal=None
        )

        def refused(decision: PolicyDecision) -> dict[str, Any]:
            return self._set_state(
                project_id, stage="request refused", awaiting="revise_brief",
                refusal=f"{decision.founder_prompt} (Rule: {decision.category.value}.)",
            )

        request_context = PolicyContext(project_id=project_id, phase=phase, surface="founder_request")
        quick = self.engine.evaluate_text(brief.as_prompt(), request_context, use_classifier=False)
        if not quick.allowed:
            return refused(quick)

        brief_key = hashlib.sha256(brief.as_prompt().encode("utf-8")).hexdigest()
        cached = self.get_state(project_id).get("screen")
        if cached and cached.get("brief") == brief_key:
            # Same brief as last time: the product type was already decided. Asking again could
            # only produce a different answer for the same idea.
            screen_output = BriefScreen(**cached["result"])
        else:
            screen = screen_brief(self.adapter, brief)
            self._add_usage(project_id, screen)
            screen_output = screen.output
            self.log.append(
                action="screen_brief", agent="intake", actor="agent_proposal", phase=phase,
                decision=f"Product type: {screen_output.excluded_category}. {screen_output.reason}",
                outcome="excluded" if screen_output.is_excluded else "allowed",
                sources=[], model=screen.model, cost=0, project_id=project_id,
                metadata={"tokens": screen.tokens},
            )
            self._set_state(
                project_id, screen={"brief": brief_key, "result": screen_output.model_dump()}
            )
        if screen_output.is_excluded:
            return self._excluded(project, screen_output)

        self._set_state(project_id, stage="safety check")
        request = self.engine.evaluate_text(brief.as_prompt(), request_context)
        if not request.allowed:
            return refused(request)

        self._set_state(project_id, stage="searching and reading sources")
        pages, search_note = self._gather_sources(project_id, brief, screen_output.search_queries)
        keywords = set(re.findall(r"[a-z]{4,}", f"{brief.idea} {brief.target_market}".lower()))
        excerpts = [select_excerpt(page.text, keywords) for page in pages]

        self._set_state(project_id, stage="finding opportunities")
        research = research_opportunities(self.adapter, brief, pages, excerpts)
        self._add_usage(project_id, research)
        claims = self._record_claims(project_id, research.output, pages)
        facts = [c for c in claims if c.claim_type == ClaimType.FACT]
        self.log.append(
            action="find_opportunities", agent="researcher", actor="agent_proposal", phase=phase,
            decision=(
                f"Sharpened the idea and scored {len(research.output.options())} options; "
                f"{len(facts)} claims sourced, {len(claims) - len(facts)} labelled ASSUMPTION."
            ),
            outcome="proposed", sources=sorted({c.source_url for c in facts if c.source_url}),
            model=research.model, cost=0, project_id=project_id,
            evidence_ids=[c.id for c in claims], metadata={"tokens": research.tokens},
        )

        self._set_state(project_id, stage="critic review")
        option = self._recommended_option(research.output)
        critic = critique(self.adapter, brief, option, claims)
        self._add_usage(project_id, critic)
        self.log.append(
            action="critique_idea", agent="critic", actor="agent_proposal", phase=phase,
            decision=(
                f"Recommendation: {critic.output.recommendation} "
                f"(confidence {critic.output.confidence}). {critic.output.reason}"
            ),
            outcome=critic.output.recommendation, sources=[], model=critic.model, cost=0,
            project_id=project_id, metadata={"tokens": critic.tokens},
        )

        recommendation, reason = discover_recommendation(critic.output, len(facts))
        path = self._write(project_id, 
            "phase1_step1_opportunities.md",
            self._scrub(project_id, phase, render_opportunities(
                project, research.output, critic.output, claims, search_note,
                recommendation, reason,
            )),
        )
        self.orch.save_phase_artifact(
            project_id, PhaseName.DISCOVER,
            {
                "research": research.output.model_dump(),
                "critic": critic.output.model_dump(),
                "recommended_option": option.model_dump(),
            },
            evidence_ids=[c.id for c in claims], agent="researcher", model=research.model,
        )
        self.log.append(
            action="discover_step1_gate", agent="gate", actor="gate_check", phase=phase,
            decision=f"Step 1 complete. System recommendation: {recommendation}. {reason}",
            outcome=f"recommend_{recommendation}", sources=[], model=None, cost=0,
            project_id=project_id, metadata={"sourced_facts": len(facts), "claims": len(claims)},
        )
        return self._set_state(
            project_id, stage="step 1 done", awaiting="step1_gate",
            recommendation=recommendation, recommendation_reason=reason,
            files=[path], seconds_step1=round(time.monotonic() - started, 1),
        )

    def _excluded(self, project: ProjectRecord, screen: BriefScreen) -> dict[str, Any]:
        """An excluded product type is rejected at once: no research, no agents, no gate."""
        label = EXCLUDED_LABELS[screen.excluded_category]
        reason = (
            f"Foundry only works on digital products. This brief describes {label}, which it "
            f"does not work on. {screen.reason}"
        )
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET status = ? WHERE id = ?",
                (ProjectStatus.STOPPED.value, project.id),
            )
        self.log.append(
            action="discover_gate", agent="gate", actor="gate_check",
            phase=PhaseName.DISCOVER.value,
            decision=f"Brief rejected: excluded product type ({label}). {screen.reason}",
            outcome="stop", sources=[], model=None, cost=0, project_id=project.id,
            metadata={"excluded_category": screen.excluded_category, "rejected_by": "system"},
        )
        # A rejected idea never takes the top of outputs/, which is kept for a full run.
        path = self._write(project.id, may_take_root=False, name=
            "phase1_gate_decision.md", content=
            render_gate_decision(project, "stop", reason, founder_decision=None, excluded=True),
        )
        return self._set_state(
            project.id, stage="rejected: product type not supported", awaiting=None,
            rejected=True, recommendation="stop", recommendation_reason=reason, files=[path],
        )

    def _gather_sources(
        self, project_id: str, brief: FounderBrief, queries: list[str], *,
        phase: str = PhaseName.DISCOVER.value, include_brief_urls: bool = True,
    ) -> tuple[list[FetchedPage], str]:
        """Search, then fetch founder-supplied URLs first and the top results after."""
        urls: list[str] = list(brief.source_urls) if include_brief_urls else []
        notes: list[str] = []
        for query in queries[:3]:
            if not self._tool_allowed(project_id, "web_search", {"query": query}):
                notes.append("A search query was blocked by guardrails.")
                continue
            try:
                results = self.search(query)
            except ToolError as exc:
                notes.append(f"Web search failed for '{query}': {exc}")
                self.log.append(
                    action="web_search", agent="researcher", actor="system", phase=phase,
                    decision=f"Search failed for '{query}': {exc}", outcome="failed",
                    sources=[], model=None, cost=0, project_id=project_id,
                )
                continue
            self.log.append(
                action="web_search", agent="researcher", actor="system", phase=phase,
                decision=f"Searched '{query}': {len(results)} results.", outcome="recorded",
                sources=[r.url for r in results], model=None, cost=0, project_id=project_id,
            )
            for result in results[:2]:
                if result.url not in urls:
                    urls.append(result.url)

        def _fetch(url: str) -> FetchedPage | str:
            if not self._tool_allowed(project_id, "fetch_url", {"url": url}):
                return "A page fetch was blocked by guardrails."
            try:
                return self.fetch(url, project_id=project_id, ledger=self.ledger)
            except ToolError as exc:
                return f"Skipped a page that could not be read ({exc})."

        pages: list[FetchedPage] = []
        candidates = urls[: MAX_PAGES + 2]
        if candidates:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for outcome in pool.map(_fetch, candidates):
                    if isinstance(outcome, str):
                        notes.append(outcome)
                    elif len(pages) < MAX_PAGES:
                        pages.append(outcome)
        self.log.append(
            action="fetch_sources", agent="researcher", actor="system", phase=phase,
            decision=f"Fetched {len(pages)} of {len(candidates)} pages.", outcome="recorded",
            sources=[p.url for p in pages], model=None, cost=0, project_id=project_id,
            metadata={"failed": len(candidates) - len(pages)},
        )
        if not pages:
            notes.append("No page could be fetched, so no claim in this run can be a FACT.")
        return pages, " ".join(notes)

    def _record_claims(
        self, project_id: str, research: ResearchOutput, pages: list[FetchedPage]
    ) -> list[Claim]:
        """Store each drafted claim. The ledger checks the quote against the fetched page."""
        claims: list[Claim] = []
        for draft in research.claims:
            url = None
            if draft.source_number is not None and 1 <= draft.source_number <= len(pages):
                url = pages[draft.source_number - 1].url
            claims.append(
                self.ledger.add_claim(
                    draft.text, project_id=project_id, linked_phase=PhaseName.DISCOVER.value,
                    source_url=url, quote=draft.quote,
                )
            )
        return claims

    @staticmethod
    def _recommended_option(research: ResearchOutput) -> Opportunity:
        wanted = research.recommended.strip().lower()
        for option in research.options():
            if option.title.strip().lower() == wanted:
                return option
        return max(research.options(), key=lambda option: option.total)

    # ------------------------------------------------------------------ step 2

    def run_discover_step2(self, project_id: str) -> dict[str, Any]:
        """Draft the Mom Test interview script, then stop at the phase gate."""
        project = self.orch._require_project(project_id)
        artifact = project.phase_artifacts.get(PhaseName.DISCOVER.value)
        if not artifact:
            raise WorkflowError("Step 1 has not produced an option yet.")
        started = time.monotonic()
        self._set_state(project_id, stage="drafting the interview script", awaiting=None)
        option = Opportunity(**artifact["recommended_option"])
        research = ResearchOutput(**artifact["research"])
        script = draft_interview_script(
            self.adapter, self._brief(project), option, research.market_definition
        )
        self._add_usage(project_id, script)
        self.log.append(
            action="draft_interview_script", agent="researcher", actor="agent_proposal",
            phase=PhaseName.DISCOVER.value,
            decision=f"Drafted a {len(script.output.questions)}-question Mom Test script.",
            outcome="proposed", sources=[], model=script.model, cost=0, project_id=project_id,
            metadata={"tokens": script.tokens},
        )
        state = self.get_state(project_id)
        files = [
            self._write(project_id, 
                "phase1_step2_interview_script.md",
                self._scrub(project_id, PhaseName.DISCOVER.value,
                            render_interview_script(project, option, script.output)),
            ),
            self._write(project_id, 
                "phase1_gate_decision.md",
                render_gate_decision(
                    project, state.get("recommendation", "repeat"),
                    state.get("recommendation_reason", ""), founder_decision=None,
                    usage=state.get("usage"),
                ),
            ),
        ]
        return self._set_state(
            project_id, stage="step 2 done", awaiting="phase_gate",
            files=state.get("files", []) + files,
            seconds_step2=round(time.monotonic() - started, 1),
        )

    # ------------------------------------------------------------------ founder decisions

    def decide_step(self, project_id: str, choice: str, *, note: str = "") -> dict[str, Any]:
        """Record the founder's choice at a gate. Called only from a founder UI or CLI action."""
        if choice not in STEP_CHOICES:
            raise WorkflowError(f"Choice must be one of {STEP_CHOICES}, got {choice!r}.")
        project = self.orch._require_project(project_id)
        state = self.get_state(project_id)
        awaiting = state.get("awaiting")
        if awaiting == "revise_brief" and choice == "stop":
            self.orch.decide_gate(project_id, GateOutcome.STOP, founder_approved=True, rationale=note)
            return self._set_state(project_id, stage="stopped by founder", awaiting=None)
        if awaiting not in {"step1_gate", "phase_gate", "excluded_gate"}:
            raise WorkflowError("No gate is waiting for a decision.")
        if awaiting == "excluded_gate" and choice != "stop":
            raise WorkflowError(
                "Excluded product types cannot continue. Stop this project and submit a new brief."
            )

        if choice == "stop":
            self.orch.decide_gate(project_id, GateOutcome.STOP, founder_approved=True, rationale=note)
            self._write_decision(project, state, "stop")
            return self._set_state(project_id, stage="stopped by founder", awaiting=None)

        if awaiting == "step1_gate":
            self.log.append(
                action="discover_step1_gate", agent="gate", actor="founder_approval",
                phase=PhaseName.DISCOVER.value,
                decision=f"Founder chose '{choice}' after step 1. {note}".strip(),
                outcome=choice, sources=[], model=None, cost=0, project_id=project_id,
                metadata={"system_recommendation": state.get("recommendation")},
            )
            if choice == "continue":
                return self._set_state(project_id, stage="ready for step 2", awaiting="run_step2")
            if choice == "repeat":
                return self._set_state(project_id, stage="ready to repeat step 1", awaiting="run_step1")
            return self._set_state(project_id, stage="back to the brief", awaiting="revise_brief")

        # Phase gate.
        if choice == "continue":
            if not state.get("validate_target"):
                raise WorkflowError(
                    "Set the demand target for Validate first. It is fixed before the test starts."
                )
            self.orch.decide_gate(project_id, GateOutcome.ADVANCE, founder_approved=True, rationale=note)
            self._write_decision(project, state, "continue")
            return self._set_state(
                project_id, stage="Discover approved", awaiting=None, discover_approved=True
            )
        if choice == "repeat":
            self.orch.decide_gate(project_id, GateOutcome.REPEAT, founder_approved=True, rationale=note)
            self._write_decision(project, state, "repeat")
            return self._set_state(project_id, stage="ready to repeat step 1", awaiting="run_step1")
        self.log.append(
            action="discover_gate", agent="gate", actor="founder_approval",
            phase=PhaseName.DISCOVER.value,
            decision=f"Founder chose to go back to step 1. {note}".strip(), outcome="go_back",
            sources=[], model=None, cost=0, project_id=project_id,
        )
        return self._set_state(project_id, stage="back to step 1 gate", awaiting="step1_gate")

    def revise_brief(self, project_id: str, brief: FounderBrief) -> dict[str, Any]:
        state = self.get_state(project_id)
        if state.get("awaiting") != "revise_brief":
            raise WorkflowError("The brief can only be revised after choosing 'go back'.")
        check = self.check_brief_for_secrets(brief)
        if check.category == PolicyCategory.CREDENTIAL_OR_CARD_STORAGE:
            raise WorkflowError(check.founder_prompt or check.reason)
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET brief_json = ?, idea_summary = ? WHERE id = ?",
                (json.dumps(brief.model_dump()), brief.idea, project_id),
            )
        self.log.append(
            action="revise_brief", agent="founder", actor="founder_approval",
            phase=PhaseName.DISCOVER.value, decision="Founder revised the brief.",
            outcome="recorded", sources=[], model=None, cost=0, project_id=project_id,
        )
        return self._set_state(project_id, stage="ready to repeat step 1", awaiting="run_step1")

    # ------------------------------------------------------------------ validate

    def set_validate_target(self, project_id: str, target: int, what_counts: str) -> dict[str, Any]:
        """The founder fixes the demand target before Validate starts. It cannot change later."""
        state = self.get_state(project_id)
        if state.get("awaiting") != "phase_gate":
            raise WorkflowError("The demand target is set at the end of Discover, before Validate starts.")
        if state.get("validate_target"):
            raise WorkflowError("The demand target is already fixed and cannot be changed.")
        what = " ".join(what_counts.split())[:200] or "waitlist sign-ups from people in the target market"
        if target < 1:
            raise WorkflowError("The target must be at least 1.")
        self.orch.set_validate_evidence_target(project_id, target)
        self.log.append(
            action="fix_demand_target", agent="founder", actor="founder_approval",
            phase=PhaseName.DISCOVER.value,
            decision=f"Demand target fixed before Validate: {target} {what}.",
            outcome="recorded", sources=[], model=None, cost=0, project_id=project_id,
            metadata={"target": target, "what_counts": what},
        )
        return self._set_state(project_id, validate_target={"target": target, "what": what})

    def run_validate_step1(self, project_id: str) -> dict[str, Any]:
        """Lean Canvas, demand-test plan, landing page and invite post. The page and the post go
        to the approval queue; the founder runs the test and records real results."""
        project = self.orch._require_project(project_id)
        state = self.get_state(project_id)
        if not state.get("discover_approved") or project.current_phase != PhaseName.VALIDATE:
            raise WorkflowError("Validate starts after the founder approves Discover.")
        if state.get("validate_stage") not in (None, "ready", "drafting"):
            raise WorkflowError("This Validate step has already run.")
        phase = PhaseName.VALIDATE.value
        started = time.monotonic()
        self._set_state(project_id, validate_stage="drafting", error=None)
        artifact = project.phase_artifacts[PhaseName.DISCOVER.value]
        option = Opportunity(**artifact["recommended_option"])
        brief = self._brief(project)
        pages = self.ledger.list_fetches(project_id=project_id)[:MAX_PAGES]
        keywords = set(re.findall(r"[a-z]{4,}", f"{brief.idea} {option.problem}".lower()))
        excerpts = [select_excerpt(page.text, keywords) for page in pages]

        strategy = design_business(
            self.adapter, brief, option, artifact.get("critic", {}).get("risks", []), pages, excerpts
        )
        self._add_usage(project_id, strategy)
        out = strategy.output
        claims: list[tuple[str, Claim]] = []
        for key, label in CANVAS_BOXES:
            box = getattr(out.canvas, key)
            url = None
            if box.source_number is not None and 1 <= box.source_number <= len(pages):
                url = pages[box.source_number - 1].url
            claims.append((label, self.ledger.add_claim(
                f"{label}: {box.text}", project_id=project_id, linked_phase=phase,
                source_url=url, quote=box.quote,
            )))
        facts = [c for _, c in claims if c.claim_type == ClaimType.FACT]
        self.log.append(
            action="design_business", agent="strategist", actor="agent_proposal", phase=phase,
            decision=(
                f"Drafted a Lean Canvas ({len(facts)} boxes sourced, {len(claims) - len(facts)} "
                "labelled ASSUMPTION), a demand-test plan, a landing page and an invite post."
            ),
            outcome="proposed", sources=sorted({c.source_url for c in facts if c.source_url}),
            model=strategy.model, cost=0, project_id=project_id,
            evidence_ids=[c.id for _, c in claims], metadata={"tokens": strategy.tokens},
        )

        landing_html = render_landing_page(out.landing)
        queued: list[str] = []
        blocked: list[str] = []
        for step, action_type, target, title, body in (
            (2, "deploy", "landing page", "Publish the landing page", landing_html),
            (2, "post", "invite post", "Post inviting people to the landing page", out.invite_post),
        ):
            try:
                queued.append(self.orch.outbox.propose_action(
                    project_id=project_id, phase=phase, step=step, action_type=action_type,
                    target=target, title=title, body=body,
                ).id)
            except PolicyViolationError as exc:
                blocked.append(f"{title}: {exc}")

        target = state.get("validate_target") or {}
        files = [
            self._write(project_id, "phase2_step1_lean_canvas.md", self._scrub(project_id, phase, render_lean_canvas(
                project, out, claims, target, iteration=project.phase_iterations.get(phase, 1)))),
            self._write(project_id, "phase2_step2_landing_page.html", landing_html if not any(
                b.startswith("Publish the landing page") for b in blocked)
                else "<!doctype html><p>The landing page draft was blocked by the guardrails and was not queued.</p>\n"),
        ]
        self.orch.save_phase_artifact(
            project_id, PhaseName.VALIDATE, {"strategy": out.model_dump()},
            evidence_ids=[c.id for _, c in claims], agent="strategist", model=strategy.model,
        )
        self._write_validate_gate(project, founder_decision=None)
        return self._set_state(
            project_id, validate_stage="awaiting_results", validate_actions=queued,
            validate_blocked=blocked, files=state.get("files", []) + files,
            seconds_validate=round(time.monotonic() - started, 1),
        )

    def validate_actions(self, project_id: str) -> list[ProposedAction]:
        return [
            a for a in self.orch.outbox.list_actions(project_id=project_id)
            if a.phase == PhaseName.VALIDATE.value
        ]

    def record_validate_results(
        self, project_id: str, count: int, kind: str, note: str = "", *, confirmed_real: bool
    ) -> dict[str, Any]:
        """The founder records what really happened in the demand test. Only real people count."""
        state = self.get_state(project_id)
        if state.get("validate_stage") not in ("awaiting_results", "results_recorded"):
            raise WorkflowError("Results can be recorded after the Validate drafts are ready.")
        if not confirmed_real:
            raise WorkflowError(
                "Only real results count. Confirm these are real people, not estimates or test entries."
            )
        if count < 0 or count > 100000:
            raise WorkflowError("Enter the number of real signals, 0 or more.")
        kind = " ".join(kind.split())[:80]
        if not re.search(r"[^\W\d_]", kind):      # needs at least one letter, e.g. "sign-ups"
            kind = "sign-ups"
        note = " ".join(note.split())[:300]
        self.orch.record_demand_signals(
            project_id, count, note=f"Founder recorded {count} {kind}. {note}".strip()
        )
        project = self.orch._require_project(project_id)
        history = state.get("validate_results", []) + [{"count": count, "kind": kind, "note": note}]
        self._set_state(project_id, validate_stage="results_recorded", validate_results=history)
        self._write_validate_gate(project, founder_decision=None)
        return self.get_state(project_id)

    def validate_check(self, project_id: str) -> GateCheckResult:
        return self.orch.check_gate(project_id)

    def decide_validate(self, project_id: str, choice: str, *, note: str = "") -> dict[str, Any]:
        if choice not in STEP_CHOICES:
            raise WorkflowError(f"Choice must be one of {STEP_CHOICES}.")
        project = self.orch._require_project(project_id)
        state = self.get_state(project_id)
        if project.current_phase != PhaseName.VALIDATE or state.get("validate_stage") not in (
            "awaiting_results", "results_recorded"
        ):
            raise WorkflowError("No Validate decision is waiting.")
        if choice == "continue" and state.get("validate_stage") != "results_recorded":
            raise WorkflowError("Record the real results of the demand test first.")
        check = self.orch.check_gate(project_id) if choice == "continue" else None
        if check is not None and not check.ready_to_advance:
            # Logged by the orchestrator with the system's recommendation, then refused.
            try:
                self.orch.decide_gate(project_id, GateOutcome.ADVANCE, founder_approved=True)
            except OrchestratorError:
                pass
            raise WorkflowError(
                "The Validate gate is not met, so Foundry cannot continue: "
                + "; ".join(check.blockers)
                + " You can repeat the test, go back to Discover, or stop."
            )
        self._write_validate_gate(project, founder_decision=choice)
        if choice == "continue":
            self.orch.decide_gate(project_id, GateOutcome.ADVANCE, founder_approved=True, rationale=note)
            return self._set_state(project_id, validate_stage="passed", validate_passed=True)
        if choice == "repeat":
            self.orch.decide_gate(project_id, GateOutcome.REPEAT, founder_approved=True, rationale=note)
            return self._set_state(project_id, validate_stage="ready")
        if choice == "go_back":
            self.orch.decide_gate(
                project_id, GateOutcome.PIVOT_BACK, founder_approved=True,
                target_phase=PhaseName.DISCOVER, rationale=note,
            )
            return self._set_state(
                project_id, validate_stage=None, discover_approved=False,
                awaiting="phase_gate", stage="back to the Discover decision",
            )
        self.orch.decide_gate(project_id, GateOutcome.STOP, founder_approved=True, rationale=note)
        return self._set_state(project_id, validate_stage="stopped", stage="stopped by founder")

    def _write_validate_gate(self, project: ProjectRecord, *, founder_decision: str | None) -> None:
        project = self.orch._require_project(project.id)
        state = self.get_state(project.id)
        check = self.orch.check_gate(project.id) if project.current_phase == PhaseName.VALIDATE else None
        self._write(project.id, "phase2_gate_decision.md", render_validate_gate(
            project, state, check, founder_decision))

    # ------------------------------------------------------------------ commercial setup

    def run_commercial_setup(self, project_id: str) -> dict[str, Any]:
        """Phase 4: price options with sourced comparables, and policy drafts. Each policy is a
        `sign` action in the approval queue. No payment code exists; nothing is charged."""
        project = self.orch._require_project(project_id)
        artifact = project.phase_artifacts.get(PhaseName.DISCOVER.value)
        state = self.get_state(project_id)
        if not artifact or not state.get("validate_passed"):
            raise WorkflowError(
                "Commercial setup needs a passed Validate gate: real demand at or above the target."
            )
        if state.get("commercial_stage") not in (None, "ready", "drafting", "starting", "failed"):
            raise WorkflowError("This Commercial setup step has already run.")
        phase = PhaseName.COMMERCIAL.value
        started = time.monotonic()
        self._set_state(project_id, commercial_stage="drafting", error=None)
        option = Opportunity(**artifact["recommended_option"])
        brief = self._brief(project)

        new_pages, notes = self._gather_sources(
            project_id, brief,
            [f"{option.title} price", f"{option.product[:80]} pricing"],
            phase=phase, include_brief_urls=False,
        )
        known = {p.url for p in new_pages}
        earlier = [p for p in self.ledger.list_fetches(project_id=project_id) if p.url not in known]
        pages = (new_pages + earlier)[:MAX_PAGES + 2]
        keywords = {"price", "pricing", "month", "year", "plan", "plans", "free", "once", "lifetime"}
        keywords |= set(re.findall(r"[a-z]{4,}", option.title.lower()))
        excerpts = [select_excerpt(page.text, keywords) for page in pages]

        target = state.get("validate_target") or {}
        results = state.get("validate_results") or []
        validate_result = (
            f"Target fixed in advance: {target.get('target', '?')} {target.get('what', '')}. "
            "Recorded: " + "; ".join(f"{r['count']} {r['kind']}" for r in results)
        )
        priced = set_price_and_policies(self.adapter, brief, option, validate_result, pages, excerpts)
        self._add_usage(project_id, priced)
        out = priced.output
        claims: list[Claim] = []
        for comp in out.comparables:
            url = None
            if comp.source_number is not None and 1 <= comp.source_number <= len(pages):
                url = pages[comp.source_number - 1].url
            claims.append(self.ledger.add_claim(
                f"{comp.product} is priced at {comp.price}.", project_id=project_id,
                linked_phase=phase, source_url=url, quote=comp.quote,
            ))
        facts = [c for c in claims if c.claim_type == ClaimType.FACT]
        self.log.append(
            action="set_price_and_policies", agent="pricing", actor="agent_proposal", phase=phase,
            decision=(
                f"Proposed {len(out.options)} price options (recommended: {out.recommended_option}), "
                f"{len(claims)} comparable prices ({len(facts)} sourced, {len(claims) - len(facts)} "
                "labelled ASSUMPTION) and drafts of the terms, privacy and refund policies."
            ),
            outcome="proposed", sources=sorted({c.source_url for c in facts if c.source_url}),
            model=priced.model, cost=0, project_id=project_id,
            evidence_ids=[c.id for c in claims], metadata={"tokens": priced.tokens},
        )

        queued: list[str] = []
        blocked: list[str] = []
        for key, label in POLICIES:
            try:
                queued.append(self.orch.outbox.propose_action(
                    project_id=project_id, phase=phase, step=2, action_type="sign",
                    target=key, title=f"Publish the {label.lower()}",
                    body=render_policy(label, getattr(out, key)),
                ).id)
            except PolicyViolationError as exc:
                blocked.append(f"{label}: {exc}")

        files = [
            self._write(project_id, "phase4_step1_pricing.md", self._scrub(project_id, phase, render_pricing(
                project, out, claims, notes, iteration=len(state.get("commercial_rounds", [])) + 1))),
            self._write(project_id, "phase4_step2_policies.md", render_policies(out, blocked)),
        ]
        self.orch.save_phase_artifact(
            project_id, PhaseName.COMMERCIAL, {"pricing": out.model_dump()},
            evidence_ids=[c.id for c in claims], agent="pricing", model=priced.model,
        )
        self._write_commercial_gate(project_id, founder_decision=None)
        return self._set_state(
            project_id, commercial_stage="awaiting approvals", commercial_actions=queued,
            commercial_blocked=blocked, files=state.get("files", []) + files,
            commercial_rounds=state.get("commercial_rounds", []) + [queued],
            seconds_commercial=round(time.monotonic() - started, 1),
        )

    def commercial_actions(self, project_id: str) -> list[ProposedAction]:
        """The policy drafts of the latest Commercial setup round."""
        ids = set(self.get_state(project_id).get("commercial_actions") or [])
        return [a for a in self.orch.outbox.list_actions(project_id=project_id) if a.id in ids]

    def commercial_check(self, project_id: str) -> tuple[bool, str]:
        actions = self.commercial_actions(project_id)
        state = self.get_state(project_id)
        pending = [a for a in actions if a.status == ActionStatus.PENDING]
        approved = [a for a in actions if a.status in {ActionStatus.APPROVED, ActionStatus.EXECUTED}]
        missing = len(POLICIES) - len(approved)
        ready = bool(actions) and not pending and missing == 0
        reason = (
            f"{len(approved)} of {len(POLICIES)} policies approved, {len(pending)} not yet reviewed"
            + (f", {len(state.get('commercial_blocked') or [])} blocked by the guardrails" if state.get("commercial_blocked") else "")
            + "."
        )
        return ready, reason

    def decide_commercial_gate(self, project_id: str, choice: str) -> dict[str, Any]:
        """Commercial gate: all three policies approved by the founder, plus the founder's choice."""
        if choice not in STEP_CHOICES:
            raise WorkflowError(f"Choice must be one of {STEP_CHOICES}.")
        state = self.get_state(project_id)
        if state.get("commercial_stage") != "awaiting approvals":
            raise WorkflowError("No Commercial setup decision is waiting.")
        ready, reason = self.commercial_check(project_id)
        if choice == "continue" and not ready:
            raise WorkflowError(
                "The Commercial setup gate is not met: the terms, privacy and refund policies must "
                f"all be approved before selling. {reason} You can repeat the step to get new drafts."
            )
        recommendation = "continue" if ready else "repeat"
        self.log.append(
            action="commercial_gate", agent="gate", actor="founder_approval",
            phase=PhaseName.COMMERCIAL.value,
            decision=f"Founder chose '{choice}' at the Commercial setup gate. {reason}",
            outcome=choice, sources=[], model=None, cost=0, project_id=project_id,
            metadata={"system_recommendation": recommendation},
        )
        self._write_commercial_gate(project_id, founder_decision=choice)
        if choice == "continue":
            return self._set_state(project_id, commercial_stage="gate decided: continue", commercial_passed=True)
        if choice == "repeat":
            return self._set_state(project_id, commercial_stage="ready", commercial_actions=[], commercial_blocked=[])
        if choice == "stop":
            try:
                self.orch.decide_gate(project_id, GateOutcome.STOP, founder_approved=True,
                                      rationale="Stopped at Commercial setup")
            except OrchestratorError:
                pass
        return self._set_state(project_id, commercial_stage=f"gate decided: {choice}")

    def _write_commercial_gate(self, project_id: str, *, founder_decision: str | None) -> None:
        project = self.orch._require_project(project_id)
        ready, reason = self.commercial_check(project_id)
        self._write(project_id, "phase4_gate_decision.md", render_commercial_gate(
            project, self.commercial_actions(project_id), ready, reason, founder_decision))

    # ------------------------------------------------------------------ launch drafts

    def run_launch_drafts(self, project_id: str) -> dict[str, Any]:
        """Draft the store page and launch posts, queue each for approval, run the MOCKED
        delivery test. Nothing is published: approved items become copy-ready text files."""
        project = self.orch._require_project(project_id)
        artifact = project.phase_artifacts.get(PhaseName.DISCOVER.value)
        state = self.get_state(project_id)
        if not artifact or not state.get("validate_passed"):
            raise WorkflowError(
                "Launch drafts need a passed Validate gate: real demand at or above the target."
            )
        if not state.get("commercial_passed"):
            raise WorkflowError(
                "Launch drafts need a passed Commercial setup gate: price options and approved policies."
            )
        phase = PhaseName.LAUNCH.value
        self._set_state(project_id, launch_stage="drafting")
        option = Opportunity(**artifact["recommended_option"])
        drafts = draft_launch(self.adapter, self._brief(project), option)
        self._add_usage(project_id, drafts)
        self.log.append(
            action="draft_launch_assets", agent="launcher", actor="agent_proposal", phase=phase,
            decision=f"Drafted a store page and {len(drafts.output.posts)} launch posts.",
            outcome="proposed", sources=[], model=drafts.model, cost=0, project_id=project_id,
            metadata={"tokens": drafts.tokens},
        )

        store_text = render_store_page(drafts.output.store_page)
        proposals = [(1, "listing", "store page", drafts.output.store_page.headline, store_text)]
        proposals += [
            (2, "email" if post.channel == "email" else "post", post.channel,
             f"Launch post ({post.channel})", post.text)
            for post in drafts.output.posts
        ]
        queued: list[str] = []
        blocked: list[str] = []
        for step, action_type, target, title, body in proposals:
            try:
                action = self.orch.outbox.propose_action(
                    project_id=project_id, phase=phase, step=step, action_type=action_type,
                    target=target, title=title, body=body,
                )
                queued.append(action.id)
            except PolicyViolationError as exc:
                blocked.append(f"{title}: {exc}")

        note = (
            "Status: **DRAFT, not published.** Each item waits in the approval queue; the founder "
            "approves or rejects the exact text. Foundry has no posting connector.\n\n"
            "Phase 3 Build is STUBBED in this prototype, so this draft is based on the Discover "
            "option. Product details are `ASSUMPTION` until the product is built."
        )
        files = [
            self._write(project_id, "phase5_step1_store_page.md",
                        f"# Phase 5, step 1: store page draft\n\n{note}\n\n---\n\n{store_text}"),
            self._write(project_id, 
                "phase5_step2_launch_posts.md",
                f"# Phase 5, step 2: launch post drafts\n\n{note}\n\n"
                + "\n\n".join(f"## {post.channel}\n\n{post.text}" for post in drafts.output.posts)
                + ("\n\n## Blocked by guardrails\n\n" + "\n".join(f"- {b}" for b in blocked) if blocked else ""),
            ),
        ]
        delivery_log = run_mock_delivery_test(
            MockDeliveryService(self.orch.db_path), project_id=project_id, product_name=option.title
        )
        files.append(self._write(project_id, "payment_test_log.md", delivery_log))
        self.log.append(
            action="mock_delivery_test", agent="delivery", actor="system", phase=phase,
            decision="Ran the MOCKED delivery test (idempotent delivery, refund revokes access). "
                     "No payment took place.",
            outcome="recorded", sources=[], model=None, cost=0, project_id=project_id,
            metadata={"label": "MOCKED"},
        )
        return self._set_state(
            project_id, launch_stage="awaiting approvals", launch_actions=queued,
            launch_blocked=blocked, files=state.get("files", []) + files,
        )

    def launch_actions(self, project_id: str) -> list[ProposedAction]:
        return [
            a for a in self.orch.outbox.list_actions(project_id=project_id)
            if a.phase == PhaseName.LAUNCH.value
        ]

    def approve_and_export(self, action_id: str, *, seen_hash: str) -> str:
        """Founder approval of the exact content shown. Called only from a founder UI action.

        `seen_hash` is the hash of the text the founder was looking at; if the content changed
        since, nothing is approved.
        """
        action = self.orch.outbox.get_action(action_id)
        if action is None:
            raise WorkflowError("Action not found.")
        if action.content_hash != seen_hash:
            raise WorkflowError("The content changed after it was shown. Review it again.")
        token = self.orch.outbox.approve_action(action_id)
        self.orch.outbox.export_dir = self.project_out_dir(action.project_id)
        receipt = self.orch.outbox.execute_action(action_id, token)
        return receipt.export_path

    def reject(self, action_id: str, reason: str) -> None:
        self.orch.outbox.reject_action(action_id, reason=reason or "Rejected by founder")

    def decide_launch_gate(self, project_id: str, choice: str) -> dict[str, Any]:
        """Launch gate: every draft reviewed and at least one approved, plus the founder's choice."""
        if choice not in STEP_CHOICES:
            raise WorkflowError(f"Choice must be one of {STEP_CHOICES}.")
        project = self.orch._require_project(project_id)
        actions = self.launch_actions(project_id)
        if not actions:
            raise WorkflowError("There are no launch drafts yet.")
        pending = [a for a in actions if a.status == ActionStatus.PENDING]
        approved = [a for a in actions if a.status in {ActionStatus.APPROVED, ActionStatus.EXECUTED}]
        rejected = [a for a in actions if a.status == ActionStatus.REJECTED]
        ready = not pending and bool(approved)
        if choice == "continue" and not ready:
            raise WorkflowError(
                "The Launch gate is not met: every draft must be reviewed and at least one approved."
            )
        recommendation = "continue" if ready else "repeat"
        reason = (
            f"{len(approved)} approved, {len(rejected)} rejected, {len(pending)} not yet reviewed."
        )
        self.log.append(
            action="launch_gate", agent="gate", actor="founder_approval", phase=PhaseName.LAUNCH.value,
            decision=f"Founder chose '{choice}' at the Launch gate. {reason}", outcome=choice,
            sources=[], model=None, cost=0, project_id=project_id,
            metadata={"system_recommendation": recommendation},
        )
        self._write(project_id, 
            "phase5_gate_decision.md",
            "# Phase 5 gate decision: Launch\n\n"
            f"Project `{project.id}`: {project.idea_summary}\n\n"
            f"**System recommendation: {recommendation.upper()}.** {reason}\n\n"
            f"**Founder decision:** {choice.upper()}\n\n"
            "## What this gate checks\n\n"
            "No draft is left unreviewed and at least one is approved. Approved drafts were "
            "exported as copy-ready text for the founder to post; nothing was published by Foundry. "
            "Delivery is `MOCKED` (see `payment_test_log.md`); no payment took place.\n\n"
            "Phase 3 Build is STUBBED in this prototype, so the project's recorded phase is not "
            "advanced by this decision.\n\n"
            "| Draft | Type | Outcome |\n|---|---|---|\n"
            + "\n".join(f"| {_cell(a.title)} | {a.action_type.value} | {a.status.value} |" for a in actions),
        )
        return self._set_state(project_id, launch_stage=f"gate decided: {choice}")

    def _write_decision(self, project: ProjectRecord, state: dict[str, Any], decision: str) -> None:
        self._write(project.id, 
            "phase1_gate_decision.md",
            render_gate_decision(
                project, state.get("recommendation", "repeat"),
                state.get("recommendation_reason", ""), founder_decision=decision,
                excluded=state.get("awaiting") == "excluded_gate", usage=state.get("usage"),
            ),
        )


# ---------------------------------------------------------------------- rendering


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_claims(claims: list[Claim]) -> str:
    lines = ["| Claim | Status | Source and supporting quote |", "|---|---|---|"]
    for claim in claims:
        if claim.claim_type == ClaimType.FACT:
            source = f"{claim.source_url} : \"{_cell(claim.quote or '')}\""
            status = "Sourced"
        else:
            source, status = "none", "`ASSUMPTION`"
        lines.append(f"| {_cell(claim.text)} | {status} | {source} |")
    return "\n".join(lines)


def render_opportunities(
    project: ProjectRecord, research: ResearchOutput, critic: CriticOutput,
    claims: list[Claim], search_note: str, recommendation: str, reason: str,
) -> str:
    facts = sum(1 for c in claims if c.claim_type == ClaimType.FACT)
    parts = [
        "# Phase 1, step 1: opportunities",
        f"Project `{project.id}`. Generated by the researcher and critic agents.",
        f"**System recommendation: {recommendation.upper()}.** {reason}",
        f"Critic: **{critic.recommendation}**, confidence **{critic.confidence}**. "
        f"Evidence: {facts} sourced claims, {len(claims) - facts} labelled `ASSUMPTION`.",
        "## Options scored on the same criteria",
    ]
    table = [
        "| Option | Customer | Problem severity | Reachable market | Founder fit | Cheap first test | Total /20 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, option in enumerate(research.options()):
        label = "Sharpened idea" if index == 0 else f"Alternative {index}"
        table.append(
            f"| **{label}: {_cell(option.title)}** | {_cell(option.customer)} | "
            f"{option.problem_severity} | {option.reachable_market} | {option.founder_fit} | "
            f"{option.cheap_first_test} | {option.total} |"
        )
    parts.append("\n".join(table))
    for index, option in enumerate(research.options()):
        label = "Sharpened idea" if index == 0 else f"Alternative {index}"
        parts += [
            f"### {label}: {option.title}",
            f"- Problem: {option.problem}\n- Product: {option.product}\n"
            f"- Why these scores: {option.score_reason}",
        ]
    parts += [
        f"**Researcher would test first:** {research.recommended}. {research.recommended_reason}",
        "The alternatives are options for the founder. The idea is not changed unless the founder chooses to.",
        "## Market",
        research.market_definition,
        "## Claims and evidence",
        "A claim is Sourced only if the page was fetched in this run and contains the quote. "
        "Scores and the market definition above are the agents' judgement, not sourced facts.",
        render_claims(claims),
    ]
    if search_note:
        parts.append(f"Research notes: {search_note}")
    parts += [
        "## Critic review",
        f"**Strongest case:** {critic.strongest_case}",
        "**Risks**\n" + "\n".join(f"{i}. {risk}" for i, risk in enumerate(critic.risks, 1)),
        "**Kill criteria (stop if any is true)**\n" + "\n".join(f"- {k}" for k in critic.kill_criteria),
        f"**Cheapest test that could disprove the idea:** {critic.cheapest_disproving_test}",
        "**Changes to consider (options, not applied)**\n"
        + "\n".join(f"- {t.change} Reason: {t.reason}" for t in critic.tweaks),
        f"**Critic's reasoning:** {critic.reason}",
    ]
    return "\n\n".join(parts)


def render_interview_script(
    project: ProjectRecord, option: Opportunity, script: InterviewScript
) -> str:
    parts = [
        "# Phase 1, step 2: customer interview script (The Mom Test)",
        f"Project `{project.id}`. Option being tested: **{option.title}**.",
        "The founder runs these interviews with real people. Foundry does not simulate "
        "interviewees, and no interview results are recorded until the founder enters them.",
        f"## Who to interview\n\n{script.who_to_interview}",
        f"## Opening\n\n{script.opening}",
        "## Questions (about past behaviour, no pitching)\n\n"
        + "\n".join(f"{i}. {q}" for i, q in enumerate(script.questions, 1)),
        "## Signs of a real problem\n\n" + "\n".join(f"- {s}" for s in script.signals_of_real_demand),
        "## Do not ask\n\n" + "\n".join(f"- {q}" for q in script.do_not_ask),
    ]
    return "\n\n".join(parts)


def render_gate_decision(
    project: ProjectRecord, recommendation: str, reason: str, *,
    founder_decision: str | None, excluded: bool = False, usage: dict[str, Any] | None = None,
) -> str:
    parts = [
        "# Phase 1 gate decision: Discover",
        f"Project `{project.id}`: {project.idea_summary}",
        f"**System recommendation: {recommendation.upper()}.** {reason}",
        (
            "**Outcome:** REJECTED by Foundry. No research was run and no founder decision is needed."
            if excluded
            else "**Founder decision:** "
            + (founder_decision.upper() if founder_decision else "pending (waiting for the founder)")
        ),
        "## What this gate checks",
        "The founder reviews the sharpened idea, the critic's risks and kill criteria, and the "
        "interview script, then chooses: continue, repeat, go back, or stop. "
        "The system recommends; only the founder decides.",
    ]
    if excluded:
        parts.append(
            "Excluded product types (physical goods; financial, investment or crypto products; "
            "health or legal advice products) are rejected immediately. The founder can submit "
            "a new brief for a digital product."
        )
    if usage and usage.get("model_calls"):
        total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        parts.append(
            f"## Cost of this run so far\n\n$0.00 ({usage['model_calls']} calls to local model "
            f"`{usage.get('model')}`, {total} tokens)."
        )
    return "\n\n".join(parts)


def render_lean_canvas(
    project: ProjectRecord, strategy: StrategyOutput, claims: list[tuple[str, Claim]],
    target: dict[str, Any], *, iteration: int = 1,
) -> str:
    facts = sum(1 for _, c in claims if c.claim_type == ClaimType.FACT)
    rows = ["| Box | What we believe | Status | Source and supporting quote |", "|---|---|---|---|"]
    for label, claim in claims:
        text = claim.text.split(": ", 1)[-1]
        if claim.claim_type == ClaimType.FACT:
            rows.append(f"| {label} | {_cell(text)} | Sourced | {claim.source_url} : \"{_cell(claim.quote or '')}\" |")
        else:
            rows.append(f"| {label} | {_cell(text)} | `ASSUMPTION` | none |")
    target_line = (
        f"**{target.get('target')} {target.get('what')}**, fixed by the founder before Validate "
        "started. It cannot be changed now."
        if target else "Not set."
    )
    parts = [
        "# Phase 2, step 1: Lean Canvas and demand test",
        f"Project `{project.id}`, Validate iteration {iteration}. Generated by the strategist agent.",
        f"Evidence: {facts} of {len(claims)} boxes are backed by a fetched source; the rest are "
        "labelled `ASSUMPTION` and are what the demand test has to check.",
        "## Lean Canvas",
        "\n".join(rows),
        "## Riskiest assumption",
        strategy.riskiest_assumption,
        "## Demand test",
        f"Target: {target_line}",
        strategy.test_plan,
        "Only results from real people count. Foundry does not create sign-ups, and simulated "
        "or test entries are never counted toward the target.",
        "## Landing page and invite post",
        "Drafts for the founder: `phase2_step2_landing_page.html` and an invite post. Both wait "
        "in the approval queue; Foundry publishes nothing.",
        f"Invite post draft:\n\n> {strategy.invite_post}",
    ]
    return "\n\n".join(parts)


def render_validate_gate(
    project: ProjectRecord, state: dict[str, Any], check: GateCheckResult | None,
    founder_decision: str | None,
) -> str:
    target = state.get("validate_target") or {}
    results = state.get("validate_results") or []
    parts = [
        "# Phase 2 gate decision: Validate",
        f"Project `{project.id}`: {project.idea_summary}",
    ]
    if check is not None:
        metrics = check.metrics
        have, need = metrics.get("recorded_demand_signals", 0), metrics.get("target", 0)
        if not results:
            parts.append(
                "**System recommendation: pending.** The demand test has not been run yet; no "
                "results are recorded."
            )
        else:
            parts.append(
                f"**System recommendation: {check.recommendation.value.upper()}.** "
                f"{check.recommendation_reason}"
            )
        parts += [
            "## Evidence against the target",
            "| Measure | Value |\n|---|---|\n"
            f"| Target, fixed before Validate | {need} {target.get('what', '')} |\n"
            f"| Real demand signals recorded by the founder | {have} |\n"
            f"| Sourced facts in the evidence store | {metrics.get('sourced_facts', 0)} |\n"
            f"| Gate met | {'yes' if check.ready_to_advance else 'no'} |",
        ]
        if check.blockers:
            parts.append("What blocks the gate:\n" + "\n".join(f"- {b}" for b in check.blockers))
    if results:
        parts.append(
            "## Results recorded by the founder\n\n"
            + "\n".join(f"- {r['count']} {r['kind']}{': ' + r['note'] if r['note'] else ''}" for r in results)
        )
    parts += [
        "**Founder decision:** "
        + (founder_decision.upper() if founder_decision else "pending (waiting for the founder)"),
        "## How this gate works",
        "Only real demand counts: sign-ups, interviews or pre-orders from real people, recorded "
        "by the founder. Below half of the target the system recommends STOP; between half and "
        "the target it recommends REPEAT. Foundry does not let a project continue past a failed "
        "evidence gate: the founder can repeat the test, go back to Discover, or stop.",
    ]
    return "\n\n".join(parts)


def render_pricing(
    project: ProjectRecord, out: PricingOutput, claims: list[Claim], notes: str, *, iteration: int = 1
) -> str:
    rows = "\n".join(
        f"| {'**' + _cell(o.name) + '** (recommended)' if o.name.strip().lower() == out.recommended_option.strip().lower() else _cell(o.name)} "
        f"| ${o.price_usd:,.2f} | {o.billing} | {_cell(o.includes)} | {_cell(o.rationale)} |"
        for o in out.options
    )
    parts = [
        "# Phase 4, step 1: price options",
        f"Project `{project.id}`: {project.idea_summary}" + (f" (round {iteration})" if iteration > 1 else ""),
        "Generated by the pricing agent. These are options for the founder to choose from. Foundry "
        "takes no payments and has no checkout: the founder sells through their own payment provider.",
        f"**Recommended option: {out.recommended_option}.** {out.recommendation_reason}",
        "| Option | Price (USD) | Billing | Includes | Why |\n|---|---|---|---|---|\n" + rows,
        "## What similar products charge",
        "A price is Sourced only if the page was fetched in this run and contains the quote.",
        render_claims(claims) if claims else "No comparable prices were found in the fetched pages.",
        "## Willingness to pay (`ASSUMPTION`)",
        out.willingness_to_pay,
        "## Cheapest test of the price",
        out.price_test,
        "No income or sales are estimated or promised here.",
    ]
    if notes:
        parts.append(f"Research notes: {notes}")
    return "\n\n".join(parts) + "\n"


def render_policies(out: PricingOutput, blocked: list[str]) -> str:
    parts = [
        "# Phase 4, step 2: policy drafts",
        "Status: **DRAFT, not published.** Each policy waits in the approval queue as a `sign` "
        "action; the founder approves or rejects the exact text. This is not legal advice.",
    ]
    for key, label in POLICIES:
        parts.append(f"## {label}\n\n{getattr(out, key).strip()}")
    if blocked:
        parts.append("## Blocked by guardrails\n\n" + "\n".join(f"- {b}" for b in blocked))
    return "\n\n".join(parts) + "\n"


def render_commercial_gate(
    project: ProjectRecord, actions: list[ProposedAction], ready: bool, reason: str,
    founder_decision: str | None,
) -> str:
    recommendation = "CONTINUE" if ready else "REPEAT"
    return "\n\n".join([
        "# Phase 4 gate decision: Commercial setup",
        f"Project `{project.id}`: {project.idea_summary}",
        f"**System recommendation: {recommendation}.** {reason}",
        "**Founder decision:** "
        + (founder_decision.upper() if founder_decision else "pending (waiting for the founder)"),
        "## What this gate checks",
        "The founder has chosen a price from the options and approved the exact text of the "
        "terms, privacy and refund policies. Approving a policy is a `sign` action: Foundry only "
        "exports the approved text as a copy-ready file for the founder to publish. No payment "
        "code exists in Foundry; nothing was charged.",
        "| Policy | Outcome |\n|---|---|\n"
        + ("\n".join(f"| {_cell(a.title)} | {a.status.value} |" for a in actions) or "| (none queued) | - |"),
    ]) + "\n"
