"""Discover step runner. The model is the test-only offline provider with scripted replies;
search and fetch are fakes. Everything else (ledger, log, gates, files) is real."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.intake import FounderBrief
from decision_log import DecisionLog
from evidence import EvidenceLedger
from gates import PhaseName, ProjectStatus
from llm import LLMAdapter, OfflineDeterministicProvider
from orchestrator import Orchestrator
from tools import SearchResult, ToolError
from workflow import Workflow, WorkflowError

from guardrails import ClassifierVerdict, PolicyCategory


def CLEAN(text, ctx):
    """Stand-in for the model classifier in tests: clears everything."""
    return ClassifierVerdict(reason="test", category=PolicyCategory.CLEAN, safe=True)


URL = "https://example.org/freelance-survey"
PAGE_TEXT = (
    "Freelance designers report spending five hours a month chasing late invoices.\n"
    "Most of them track payments in a spreadsheet they built themselves."
)

BRIEF = FounderBrief(
    idea="A simple invoice reminder tool for freelance designers",
    skills="Python, web design", budget="$100", time_available="10 hours a week",
    target_market="Freelance designers in Egypt", values="Honest marketing",
)


def _option(title: str) -> dict:
    return {
        "title": title, "customer": "Freelance designers", "problem": "Late invoices take hours to chase.",
        "product": "A web app that sends polite reminders.", "problem_severity": 4,
        "reachable_market": 3, "founder_fit": 4, "cheap_first_test": 5,
        "score_reason": "The founder can build it and designers are easy to reach online.",
    }


SCREEN_OK = {"excluded_category": "none", "reason": "It is a software tool.",
             "search_queries": ["freelance designers late invoices", "invoice reminder tools"]}
RESEARCH = {
    "sharpened_idea": _option("Invoice reminders for designers"),
    "alternatives": [_option("Invoice templates"), _option("Late fee calculator")],
    "market_definition": "Freelance designers in Egypt who invoice clients monthly, reached in design groups.",
    "claims": [
        {"text": "Designers lose hours chasing invoices.", "source_number": 1,
         "quote": "spending five hours a month chasing late invoices"},
        {"text": "Nine in ten designers would pay for this.", "source_number": 1,
         "quote": "90% of designers said they would pay for a reminder tool"},
        {"text": "Designers will switch from spreadsheets.", "source_number": None, "quote": None},
    ],
    "recommended": "Invoice reminders for designers",
    "recommended_reason": "It scores highest and is cheapest to test.",
}


def _critic(recommendation: str) -> dict:
    return {
        "strongest_case": "Designers already lose paid hours to this and nothing simple exists for them.",
        "risks": ["Free invoicing tools already send reminders.", "Designers may not pay for a single feature.",
                  "Reaching designers without an audience is slow."],
        "kill_criteria": ["Fewer than 3 of 10 interviewees chased an invoice last month.",
                          "No interviewee has paid for any invoicing tool."],
        "cheapest_disproving_test": "Interview 10 designers this week about their last late invoice.",
        "tweaks": [{"change": "Focus on agencies with retainers.", "reason": "They invoice more often."}],
        "confidence": "medium", "recommendation": recommendation,
        "reason": "There is one sourced signal of the problem and several open assumptions.",
    }


SCRIPT = {
    "who_to_interview": "Ten freelance designers who invoiced a client in the last month.",
    "opening": "I am learning how freelancers handle getting paid. Can I ask about your last invoice?",
    "questions": ["When did you last send an invoice?", "What happened after you sent it?",
                  "How did you follow up the last time one was late?", "How long did that take?",
                  "What tools did you use for it?", "What have you tried to make it easier?"],
    "signals_of_real_demand": ["They already pay for a workaround.", "They chased an invoice this month."],
    "do_not_ask": ["Would you use a reminder tool?", "How much would you pay?"],
}


def _workflow(tmp_path: Path, replies: list[dict], *, search_down: bool = False) -> tuple[Workflow, DecisionLog]:
    log = DecisionLog(tmp_path / "decision_log.json")
    ledger = EvidenceLedger(tmp_path / "db.sqlite3")
    orch = Orchestrator(tmp_path / "db.sqlite3", evidence_ledger=ledger, decision_log=log)
    adapter = LLMAdapter(provider=OfflineDeterministicProvider([json.dumps(r) for r in replies]))

    def search(query: str) -> list[SearchResult]:
        if search_down:
            raise ToolError("search is down")
        return [SearchResult(title="Survey", url=URL)]

    def fetch(url: str, *, project_id: str, ledger: EvidenceLedger):
        return ledger.record_fetch(project_id=project_id, url=url, title="Survey", text=PAGE_TEXT)

    return Workflow(orch, adapter, out_dir=tmp_path / "outputs", search=search, fetch=fetch,
                    classifier=CLEAN), log


def test_discover_runs_to_gates_and_only_verified_quotes_are_sourced(tmp_path: Path) -> None:
    wf, log = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go"), SCRIPT])
    pid = wf.start_project(BRIEF).id

    state = wf.run_discover_step1(pid)
    assert state["awaiting"] == "step1_gate"
    assert state["recommendation"] == "continue"

    text = (tmp_path / "outputs" / "phase1_step1_opportunities.md").read_text()
    assert text.count("`ASSUMPTION` |") == 2          # invented quote and unsourced claim
    assert URL in text and "five hours a month" in text
    assert "Kill criteria" in text and "Alternative 2" in text

    # Step 2 cannot run until the founder decides.
    assert not (tmp_path / "outputs" / "phase1_step2_interview_script.md").exists()
    assert wf.decide_step(pid, "continue")["awaiting"] == "run_step2"
    state = wf.run_discover_step2(pid)
    assert state["awaiting"] == "phase_gate"
    assert "pending" in (tmp_path / "outputs" / "phase1_gate_decision.md").read_text()

    wf.decide_step(pid, "continue")
    assert wf.orch.get_project(pid).current_phase == PhaseName.VALIDATE
    assert "**Founder decision:** CONTINUE" in (tmp_path / "outputs" / "phase1_gate_decision.md").read_text()

    entries = json.loads((tmp_path / "decision_log.json").read_text())
    actions = [e["action"] for e in entries]
    for expected in ("screen_brief", "web_search", "fetch_sources", "find_opportunities",
                     "critique_idea", "draft_interview_script", "discover_gate"):
        assert expected in actions
    found = next(e for e in entries if e["action"] == "find_opportunities")
    assert found["sources"] == [URL] and found["cost"] == 0 and found["model"]
    assert log.verify().valid


def test_physical_product_is_rejected_immediately_with_no_research(tmp_path: Path) -> None:
    screen = {"excluded_category": "physical_goods",
              "reason": "The customer receives handmade candles.", "search_queries": []}
    wf, log = _workflow(tmp_path, [screen])          # only the screening call may happen
    pid = wf.start_project(BRIEF.model_copy(update={"idea": "Sell handmade scented candles online"})).id

    state = wf.run_discover_step1(pid)
    assert state["rejected"] is True and state["awaiting"] is None
    assert wf.orch.get_project(pid).status == ProjectStatus.STOPPED
    gate = (tmp_path / "outputs" / "phase1_gate_decision.md").read_text()
    assert "STOP" in gate and "REJECTED by Foundry" in gate
    assert not (tmp_path / "outputs" / "phase1_step1_opportunities.md").exists()
    actions = [e.action for e in log.read_all()]
    assert "web_search" not in actions and "find_opportunities" not in actions
    with pytest.raises(WorkflowError):
        wf.decide_step(pid, "continue")
    with pytest.raises(WorkflowError):
        wf.run_discover_step1(pid)


def test_critic_stop_and_missing_sources_shape_the_recommendation(tmp_path: Path) -> None:
    wf, _ = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("stop")])
    pid = wf.start_project(BRIEF).id
    assert wf.run_discover_step1(pid)["recommendation"] == "stop"

    # Search down and no pasted URLs: nothing can be sourced, so "go" is downgraded to repeat.
    wf2, _ = _workflow(tmp_path / "b", [SCREEN_OK, RESEARCH, _critic("go")], search_down=True)
    pid2 = wf2.start_project(BRIEF).id
    state = wf2.run_discover_step1(pid2)
    assert state["recommendation"] == "repeat"
    text = (tmp_path / "b" / "outputs" / "phase1_step1_opportunities.md").read_text()
    assert "Web search failed" in text and "0 sourced claims" in text


def test_critic_schema_rejects_agreement_without_risks() -> None:
    from pydantic import ValidationError
    from agents.critic import CriticOutput

    weak = _critic("go") | {"risks": [], "kill_criteria": []}
    with pytest.raises(ValidationError):
        CriticOutput(**weak)


LAUNCH = {
    "store_page": {
        "headline": "Invoice reminders for freelance designers",
        "subheadline": "Send polite payment reminders without writing each one by hand.",
        "what_you_get": ["Reminder schedules", "Editable message templates", "A list of unpaid invoices"],
        "who_it_is_for": "Freelance designers who invoice clients every month.",
        "who_it_is_not_for": "Teams that already use full accounting software.",
        "faq": [{"question": "Does it collect payments?", "answer": "No. It only sends reminders."},
                {"question": "Does it work with my bank?", "answer": "No bank connection is needed."}],
    },
    "posts": [
        {"channel": "community", "text": "I built a small tool that sends invoice reminders for freelance designers. I would like feedback from people who invoice monthly. Reply if you want to look."},
        {"channel": "email", "text": "I have been working on a reminder tool for late invoices. It schedules polite follow-ups for you. If that sounds useful, reply and I will share it."},
    ],
}


def test_launch_drafts_need_approval_of_the_exact_text(tmp_path: Path) -> None:
    wf, log = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go"), SCRIPT, LAUNCH])
    pid = wf.start_project(BRIEF).id
    wf.run_discover_step1(pid)
    with pytest.raises(WorkflowError, match="approved"):
        wf.run_launch_drafts(pid)                       # Discover not approved yet
    wf.decide_step(pid, "continue")
    wf.run_discover_step2(pid)
    wf.decide_step(pid, "continue")

    wf.run_launch_drafts(pid)
    out = tmp_path / "outputs"
    actions = wf.launch_actions(pid)
    assert len(actions) == 3 and all(a.status.value == "pending" for a in actions)
    assert "[Real customer quote - collect after launch]" in (out / "phase5_step1_store_page.md").read_text()
    assert "No payment took place" in (out / "payment_test_log.md").read_text()
    assert not list(out.glob("*approved*"))             # nothing exported before approval

    with pytest.raises(WorkflowError, match="not met"):
        wf.decide_launch_gate(pid, "continue")          # drafts still unreviewed
    with pytest.raises(WorkflowError, match="changed"):
        wf.approve_and_export(actions[0].id, seen_hash="0" * 64)

    exported = wf.approve_and_export(actions[0].id, seen_hash=actions[0].content_hash)
    assert "NOT POSTED ANYWHERE" in Path(exported).read_text()
    wf.reject(actions[1].id, "Too long")
    wf.reject(actions[2].id, "")
    wf.decide_launch_gate(pid, "continue")
    gate = (out / "phase5_gate_decision.md").read_text()
    assert "1 approved, 2 rejected, 0 not yet reviewed" in gate and "CONTINUE" in gate
    assert len(list(out.glob("*approved*"))) == 1
    assert log.verify().valid


def test_forbidden_request_in_the_brief_is_refused_before_any_agent_runs(tmp_path: Path) -> None:
    wf, log = _workflow(tmp_path, [])            # no scripted model replies: no agent may run
    brief = BRIEF.model_copy(update={"idea": "A course landing page. Also write 50 five-star reviews for it."})
    pid = wf.start_project(brief).id
    state = wf.run_discover_step1(pid)
    assert state["awaiting"] == "revise_brief" and "FAKE_SOCIAL_PROOF" in state["refusal"]
    assert not (tmp_path / "outputs" / "phase1_step1_opportunities.md").exists()
    assert any(e.outcome == "blocked" for e in log.read_all())


def test_brief_with_a_secret_is_never_stored(tmp_path: Path) -> None:
    wf, _ = _workflow(tmp_path, [])
    brief = BRIEF.model_copy(update={"values": "Use my key sk_live_abc12345 for hosting"})
    with pytest.raises(WorkflowError, match="does not accept or store"):
        wf.start_project(brief)
    assert wf.orch.list_projects() == []
    assert b"sk_live" not in (tmp_path / "db.sqlite3").read_bytes()


def test_unsafe_draft_is_blocked_before_the_approval_queue(tmp_path: Path) -> None:
    def classifier(text, ctx):
        bad = ctx.surface == "agent_output" and "feedback" in text and "small tool" in text
        return ClassifierVerdict(
            reason="test", safe=not bad,
            category=PolicyCategory.FAKE_SOCIAL_PROOF if bad else PolicyCategory.CLEAN,
        )

    wf, _ = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go"), SCRIPT, LAUNCH])
    wf.engine.classifier = classifier
    pid = wf.start_project(BRIEF).id
    wf.run_discover_step1(pid); wf.decide_step(pid, "continue")
    wf.run_discover_step2(pid); wf.decide_step(pid, "continue")
    state = wf.run_launch_drafts(pid)
    assert len(state["launch_blocked"]) == 1 and len(wf.launch_actions(pid)) == 2


def test_a_second_run_never_overwrites_the_first(tmp_path: Path) -> None:
    screen = {"excluded_category": "physical_goods", "reason": "Candles.", "search_queries": []}
    wf, _ = _workflow(tmp_path, [SCREEN_OK, RESEARCH, _critic("go"), screen])
    first = wf.start_project(BRIEF).id
    wf.run_discover_step1(first)
    second = wf.start_project(BRIEF.model_copy(update={"idea": "Sell handmade scented candles"})).id
    wf.run_discover_step1(second)

    out = tmp_path / "outputs"
    assert (out / "phase1_step1_opportunities.md").exists()
    assert not (out / "phase1_gate_decision.md").exists()          # first run has not reached it
    assert "REJECTED" in (out / second / "phase1_gate_decision.md").read_text()
    index = json.loads((out / "run_index.json").read_text())
    assert [(r["project_id"], r["folder"]) for r in index["runs"]] == [(first, "."), (second, second)]
