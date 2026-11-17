"""Foundry web app.

- `/`: the founder submits a brief and sees their projects.
- `/projects/<id>`: the current step, its output, the system's recommendation and the four
  choices. A step runs in the background; nothing moves past a gate without a founder choice.
- `/status`: live checks (model, search, decision log, guardrail evals) and the status table.

Working: brief intake and Phase 1 (Discover). Later phases are STUBBED and labelled so.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

import threading
from urllib.parse import quote

import markdown as markdown_lib
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from agents.intake import FounderBrief
from approval import OutboxError
from guardrails import PolicyViolationError
from orchestrator import Orchestrator, OrchestratorError
from tools import search_status
from workflow import STEP_CHOICES, Workflow, WorkflowError, outputs_dir

from decision_log import DecisionLog
from llm import LLMAdapter
from evals.redteam import run_redteam_suite

ROOT = Path(__file__).resolve().parent
DECISION_LOG = Path(os.environ.get("FOUNDRY_DECISION_LOG", ROOT / "decision_log.json"))

# Single source for status labels shown in the app. Keep in step with the README table.
STATUS_ROWS: list[tuple[str, str, str]] = [
    ("Evidence store (sourced claim or ASSUMPTION)", "Working", "evidence.py"),
    ("Decision log with hash chain", "Working", "decision_log.py"),
    ("Approval queue with signed single-use tokens", "Working", "approval.py"),
    ("Six-phase state machine and gates", "Working", "orchestrator.py, gates.py"),
    ("Guardrails: keyword rules and tool allowlist", "Working", "guardrails.py"),
    ("Guardrails: model classifier on every output", "STUBBED", "built, not switched on by default"),
    ("Local model adapter (Ollama)", "Working", "llm.py"),
    ("Founder brief intake, with excluded product types refused", "Working", "agents/intake.py, app.py"),
    ("Web search (self-hosted SearXNG) and page fetch with quote check", "Working", "tools.py"),
    ("Phase 1 Discover: opportunities, critic review, interview script", "Working", "agents/researcher.py, agents/critic.py, workflow.py"),
    ("Step gates in the browser: continue, repeat, go back, stop", "Working", "app.py, workflow.py"),
    ("Phase 2 Validate, 3 Build, 4 Commercial setup, 6 Operate", "STUBBED", "agents not built"),
    ("Phase 5 Launch: store page and launch post drafts into the approval queue", "Working", "agents/launcher.py, workflow.py"),
    ("Approving or rejecting the exact text in the browser, export as copy-ready text", "Working", "app.py, approval.py"),
    ("Delivery to buyers", "MOCKED", "delivery.py: simulated order, idempotent delivery, refund revokes access"),
    ("Posting to social, email or store sites", "STUBBED", "by design: copy-ready text only"),
    ("Payments", "None", "no payment code exists"),
]

app = FastAPI(title="Foundry", version="0.1.0")


def _verify(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "valid": None, "entries": 0, "head": "", "errors": []}
    result = DecisionLog(path).verify()
    return {
        "path": str(path),
        "exists": True,
        "valid": result.valid,
        "entries": result.entry_count,
        "head": result.head_hash,
        "errors": result.errors,
    }


def collect_status() -> dict[str, Any]:
    results = run_redteam_suite()
    return {
        "llm": LLMAdapter().runtime_status(),
        "search": search_status(),
        "decision_log": _verify(DECISION_LOG),
        "status": [{"part": p, "status": s, "where": w} for p, s, w in STATUS_ROWS],
        "redteam": {
            "passed": sum(1 for r in results if r.passed),
            "total": len(results),
            "cases": [
                {
                    "case_id": r.case_id,
                    "name": r.name,
                    "prompt": r.prompt_or_payload,
                    "blocked": r.blocked,
                    "logged": r.logged,
                    "founder_prompted": r.founder_prompted,
                    "rule_id": r.rule_id,
                    "passed": r.passed,
                }
                for r in results
            ],
        },
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return collect_status()


def _badge(ok: bool | None, yes: str, no: str, unknown: str = "n/a") -> str:
    if ok is None:
        return f'<span class="b grey">{html.escape(unknown)}</span>'
    return f'<span class="b {"green" if ok else "red"}">{html.escape(yes if ok else no)}</span>'


@app.get("/status", response_class=HTMLResponse)
def status_page() -> str:
    st = collect_status()
    llm = st["llm"]
    e = html.escape

    if llm["is_mock"]:
        llm_line = '<span class="b red">MOCKED: offline test provider, not a real model</span>'
    elif not llm["ollama_reachable"]:
        llm_line = '<span class="b red">Ollama not reachable - model calls will fail (no mock fallback)</span>'
    elif not llm["model_downloaded"]:
        llm_line = '<span class="b amber">Model still downloading - model calls will fail until it finishes</span>'
    else:
        llm_line = '<span class="b green">Real local model ready</span>'

    log_rows = ""
    for label, info in (("decision_log.json", st["decision_log"]),):
        state = _badge(info["valid"], "VALID", "INVALID", "not created yet")
        errs = "<br>".join(e(x) for x in info["errors"])
        log_rows += (
            f"<tr><td>{e(label)}</td><td>{state}</td><td>{info['entries']}</td>"
            f"<td><code>{e(info['head'][:16])}</code></td><td><code>{e(info['path'])}</code>"
            f"{'<br>' + errs if errs else ''}</td></tr>"
        )

    rt = st["redteam"]
    rt_rows = "".join(
        f"<tr><td><code>{e(c['case_id'])}</code></td><td>{e(c['name'])}</td><td>{e(c['prompt'])}</td>"
        f"<td>{_badge(c['blocked'], 'yes', 'no')}</td><td>{_badge(c['logged'], 'yes', 'no')}</td>"
        f"<td><code>{e(c['rule_id'] or '')}</code></td><td>{_badge(c['passed'], 'PASS', 'FAIL')}</td></tr>"
        for c in rt["cases"]
    )
    colour = {"Working": "green", "STUBBED": "amber", "MOCKED": "amber", "None": "grey"}
    caps = "".join(
        f"<tr><td>{e(p)}</td><td><span class=\"b {colour[s]}\">{e(s)}</span></td><td>{e(w)}</td></tr>"
        for p, s, w in STATUS_ROWS
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Foundry status</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}}td,th{{border:1px solid #ddd;padding:.4rem .5rem;text-align:left;vertical-align:top;font-size:.9rem}}
th{{background:#f5f5f5}}.b{{padding:.1rem .5rem;border-radius:.6rem;font-size:.85rem;font-weight:600;color:#fff}}
.green{{background:#1a7f37}}.red{{background:#c62828}}.amber{{background:#b26a00}}.grey{{background:#666}}
.stub{{border:2px dashed #b26a00;padding:.6rem 1rem;background:#fff8e6}}pre{{background:#f5f5f5;padding:1rem;overflow:auto;font-size:.8rem}}
</style></head><body>
<p><a href="/">Back to projects</a></p>
<h1>Foundry status</h1>
<p class="stub">Everything on this page is computed live. Parts marked <strong>STUBBED</strong> are designed
but not built; parts marked <strong>MOCKED</strong> are simulated.</p>
<h2>Local model</h2>
<p>{llm_line}</p>
<p>Web search: {_badge(st['search']['reachable'], 'SearXNG reachable', 'SearXNG not reachable - research uses pasted URLs only')}
<code>{e(str(st['search']['searxng_url']))}</code></p>
<p>Provider <code>{e(str(llm['active_provider']))}</code>, model <code>{e(str(llm['configured_model']))}</code>,
Ollama at <code>{e(str(llm['ollama_url']))}</code></p>
<h2>Decision log (required keys and hash chain verified now)</h2>
<table><tr><th>Log</th><th>Status</th><th>Entries</th><th>Head hash</th><th>Path</th></tr>{log_rows}</table>
<h2>Guardrail evals (run now): {rt['passed']}/{rt['total']} passed</h2>
<table><tr><th>Case</th><th>Challenge</th><th>Prompt</th><th>Blocked</th><th>Logged</th><th>Rule</th><th>Result</th></tr>{rt_rows}</table>
<h2>What works and what is mocked</h2>
<table><tr><th>Part</th><th>Status</th><th>Where</th></tr>{caps}</table>
</body></html>"""


# ---------------------------------------------------------------------- founder pages

PAGE_CSS = """
body{font-family:system-ui,sans-serif;max-width:920px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;line-height:1.45}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}td,th{border:1px solid #ddd;padding:.4rem .5rem;text-align:left;vertical-align:top;font-size:.9rem}
th{background:#f5f5f5}label{display:block;font-weight:600;margin-top:.8rem}
input[type=text],textarea{width:100%;padding:.5rem;font:inherit;box-sizing:border-box;border:1px solid #bbb;border-radius:.3rem}
button{font:inherit;padding:.5rem 1rem;border-radius:.3rem;border:1px solid #888;background:#fff;cursor:pointer;margin:.2rem .3rem .2rem 0}
button.primary{background:#1a4fd6;color:#fff;border-color:#1a4fd6}button.stop{background:#c62828;color:#fff;border-color:#c62828}
.b{padding:.1rem .5rem;border-radius:.6rem;font-size:.85rem;font-weight:600;color:#fff}
.green{background:#1a7f37}.red{background:#c62828}.amber{background:#b26a00}.grey{background:#666}
.box{border:1px solid #ccc;border-radius:.4rem;padding:.8rem 1rem;margin:1rem 0}
.gate{border:2px solid #1a4fd6;background:#f3f6ff}.warn{border:2px solid #c62828;background:#fff3f3}
.stub{border:2px dashed #b26a00;background:#fff8e6}.doc{background:#fafafa}small{color:#555}
"""

_workflow: Workflow | None = None
_workflow_lock = threading.Lock()
_running: set[str] = set()


def get_workflow() -> Workflow:
    global _workflow
    with _workflow_lock:
        if _workflow is None:
            db_path = os.environ.get("FOUNDRY_DB_PATH", "data/foundry.sqlite3")
            _workflow = Workflow(Orchestrator(db_path, decision_log=DecisionLog(DECISION_LOG)))
        return _workflow


def _page(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
        f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>"
        '<p><a href="/">Foundry</a> · <a href="/status">Status and what is mocked</a></p>'
        f"{body}</body></html>"
    )


def render_markdown(text: str) -> str:
    """Model output is escaped first, so it can never inject HTML into the page."""
    return markdown_lib.markdown(html.escape(text, quote=False), extensions=["tables"])


def _start_step(project_id: str, step: str) -> None:
    """Run a step in the background. A failure is recorded and shown, never hidden."""
    wf = get_workflow()
    with _workflow_lock:
        if project_id in _running:
            return
        _running.add(project_id)

    def _run() -> None:
        try:
            if step == "step1":
                wf.run_discover_step1(project_id)
            elif step == "step2":
                wf.run_discover_step2(project_id)
            else:
                wf.run_launch_drafts(project_id)
        except Exception as exc:  # noqa: BLE001 - shown to the founder with a retry button
            failure = f"{type(exc).__name__}: {exc}"
            if step == "launch":
                wf._set_state(project_id, launch_stage="failed", error=failure)
            else:
                wf._set_state(
                    project_id, stage="failed", error=failure,
                    awaiting="run_step1" if step == "step1" else "run_step2",
                )
        finally:
            with _workflow_lock:
                _running.discard(project_id)

    if step == "launch":
        wf._set_state(project_id, launch_stage="starting", error=None)
    else:
        wf._set_state(project_id, stage="starting", awaiting=None, error=None)
    threading.Thread(target=_run, daemon=True).start()


def parse_reply(text: str) -> str | None:
    """Map a typed reply to a choice. Anything unclear ('ok', 'sure') returns None."""
    cleaned = " ".join(text.lower().replace("_", " ").split()).strip(" .!")
    return {
        "continue": "continue", "go": "continue", "go on": "continue",
        "repeat": "repeat", "go back": "go_back", "back": "go_back", "stop": "stop",
    }.get(cleaned)


def _brief_form(brief: dict[str, Any] | None = None, action: str = "/projects") -> str:
    b = brief or {}
    e = html.escape

    def field(name: str, label: str, hint: str, rows: int = 1) -> str:
        value = e(str(b.get(name, "")))
        control = (
            f'<textarea name="{name}" rows="{rows}" placeholder="{e(hint)}">{value}</textarea>'
            if rows > 1 else f'<input type="text" name="{name}" value="{value}" placeholder="{e(hint)}">'
        )
        return f"<label>{e(label)}</label>{control}"

    urls = e("\n".join(b.get("source_urls", [])))
    return (
        f'<form method="post" action="{action}">'
        + field("idea", "Starting idea", "What digital product do you want to build, and for whom?", 3)
        + field("skills", "Your skills", "For example: Python, design, teaching")
        + field("budget", "Budget", "For example: $100")
        + field("time_available", "Available time", "For example: 10 hours a week for 2 months")
        + field("target_market", "Target market", "Who and where")
        + field("values", "Values", "What you will and will not do")
        + '<label>Source URLs to read (optional, one per line, up to 5)</label>'
        f'<textarea name="source_urls" rows="2" placeholder="https://...">{urls}</textarea>'
        '<p><button class="primary" type="submit">Start Discover</button></p></form>'
    )


def _brief_from_form(idea, skills, budget, time_available, target_market, values, source_urls) -> FounderBrief:
    urls = [u.strip() for u in source_urls.splitlines() if u.strip().startswith(("http://", "https://"))]
    return FounderBrief(
        idea=idea.strip(), skills=skills.strip(), budget=budget.strip(),
        time_available=time_available.strip(), target_market=target_market.strip(),
        values=values.strip(), source_urls=urls[:5],
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    wf = get_workflow()
    llm = LLMAdapter().runtime_status()
    e = html.escape
    if llm["is_mock"]:
        model = '<p class="box warn"><strong>MOCKED:</strong> the offline test provider is active, not a real model.</p>'
    elif not llm["ready"]:
        model = ('<p class="box warn">The local model is not ready yet (still downloading, or Ollama is not '
                 'reachable). Steps will fail until it is. See <a href="/status">Status</a>.</p>')
    else:
        model = f'<p><span class="b green">Local model ready</span> <code>{e(str(llm["configured_model"]))}</code></p>'
    rows = "".join(
        f'<tr><td><a href="/projects/{e(p.id)}">{e(p.name)}</a></td><td>{e(p.current_phase.value)}</td>'
        f"<td>{e(p.status.value)}</td><td>{e(wf.get_state(p.id).get('stage', ''))}</td></tr>"
        for p in wf.orch.list_projects()
    )
    projects = (
        f"<h2>Projects</h2><table><tr><th>Idea</th><th>Phase</th><th>Status</th><th>Stage</th></tr>{rows}</table>"
        if rows else ""
    )
    body = (
        "<h1>Foundry</h1><p>A team of AI agents that takes your brief toward a validated digital product, "
        "and tells you plainly when the evidence says stop. You decide at every step.</p>"
        f"{model}<h2>New founder brief</h2>{_brief_form()}"
        '<p class="box stub"><strong>STUBBED:</strong> Phases 2 to 6 (Validate, Build, Commercial setup, '
        "Launch, Operate) are designed but not built in this prototype. Phase 1 (Discover) runs for real.</p>"
        f"{projects}"
    )
    return _page("Foundry", body)


@app.post("/projects")
def create_project(
    idea: str = Form(...), skills: str = Form(""), budget: str = Form(""),
    time_available: str = Form(""), target_market: str = Form(""), values: str = Form(""),
    source_urls: str = Form(""),
):
    try:
        brief = _brief_from_form(idea, skills, budget, time_available, target_market, values, source_urls)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    project = get_workflow().start_project(brief)
    _start_step(project.id, "step1")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


def _read_output(name: str) -> str:
    path = outputs_dir() / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _choice_buttons(project_id: str, allowed: tuple[str, ...]) -> str:
    labels = {"continue": "Continue", "repeat": "Repeat this step", "go_back": "Go back", "stop": "Stop the project"}
    css = {"continue": "primary", "stop": "stop"}
    buttons = "".join(
        f'<button class="{css.get(c, "")}" name="choice" value="{c}" type="submit">{labels[c]}</button>'
        for c in STEP_CHOICES if c in allowed
    )
    return (
        f'<form method="post" action="/projects/{project_id}/decide">{buttons}</form>'
        f'<form method="post" action="/projects/{project_id}/decide"><label>Or type your choice</label>'
        '<input type="text" name="reply" placeholder="continue, repeat, go back or stop">'
        '<button type="submit">Send</button></form>'
    )


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(project_id: str, unclear: str = "") -> str:
    wf = get_workflow()
    project = wf.orch.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    state = wf.get_state(project_id)
    awaiting = state.get("awaiting")
    e = html.escape
    usage = state.get("usage") or {}
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    parts = [
        f"<h1>{e(project.name)}</h1>",
        f"<p>Phase <strong>{e(project.current_phase.value)}</strong> · project <strong>{e(project.status.value)}</strong>"
        f" · stage: {e(str(state.get('stage', '')))} · cost so far $0.00 ({tokens} tokens, local model)</p>",
    ]
    running = project_id in _running
    if state.get("error"):
        parts.append(f'<div class="box warn"><strong>The step failed.</strong> {e(state["error"])}<br>'
                     "Nothing was made up to cover for it. You can run the step again.</div>")
    if unclear:
        parts.append(f'<div class="box warn">Your reply "{e(unclear)}" is not a clear choice, so nothing was done. '
                     "Please choose one of: continue, repeat, go back, stop.</div>")
    if running:
        parts.append('<div class="box">The agents are working. This page refreshes by itself.</div>')
        return _page(project.name, "".join(parts), refresh=3)

    recommendation = (
        f'<p><strong>System recommendation: {e(str(state.get("recommendation", "")).upper())}.</strong> '
        f'{e(str(state.get("recommendation_reason", "")))}</p>'
    )
    if awaiting == "step1_gate":
        parts.append(f'<div class="box gate"><h2>Gate: step 1 of Discover is done</h2>{recommendation}'
                     "<p>Read the output below, then choose. Nothing continues until you do.</p>"
                     f"{_choice_buttons(project_id, STEP_CHOICES)}</div>")
        parts.append(f'<div class="box doc">{render_markdown(_read_output("phase1_step1_opportunities.md"))}</div>')
    elif awaiting == "phase_gate":
        parts.append(f'<div class="box gate"><h2>Gate: end of Phase 1 (Discover)</h2>{recommendation}'
                     "<p>Continue approves the sharpened idea and opens Phase 2.</p>"
                     f"{_choice_buttons(project_id, STEP_CHOICES)}</div>")
        parts.append(f'<div class="box doc">{render_markdown(_read_output("phase1_step2_interview_script.md"))}</div>')
        parts.append(f'<details class="box doc"><summary>Step 1 output</summary>{render_markdown(_read_output("phase1_step1_opportunities.md"))}</details>')
    elif awaiting == "excluded_gate":
        parts.append(f'<div class="box warn"><h2>This product type is not supported</h2>{recommendation}'
                     f"{_choice_buttons(project_id, ('stop',))}</div>")
    elif awaiting in {"run_step1", "run_step2"}:
        step = "step1" if awaiting == "run_step1" else "step2"
        label = "Run step 1 (find opportunities)" if step == "step1" else "Run step 2 (interview script)"
        parts.append(f'<form method="post" action="/projects/{project_id}/run/{step}">'
                     f'<button class="primary" type="submit">{label}</button></form>')
    elif awaiting == "revise_brief":
        parts.append("<h2>Revise the brief</h2>" + _brief_form(project.brief, f"/projects/{project_id}/brief"))
    elif project.status.value == "stopped":
        parts.append('<div class="box">This project was stopped by the founder.</div>'
                     f'<div class="box doc">{render_markdown(_read_output("phase1_gate_decision.md"))}</div>')
    elif state.get("discover_approved"):
        parts.append('<div class="box stub"><strong>STUBBED:</strong> Discover is approved. Phases 2 to 4 '
                     "(Validate, Build, Commercial setup) are designed but not built in this prototype. "
                     "The Launch drafting and approval step below works, and is based on the Discover option only.</div>")
        parts.append(_launch_section(wf, project_id, state))
    return _page(project.name, "".join(parts))


def _launch_section(wf: Workflow, project_id: str, state: dict[str, Any]) -> str:
    e = html.escape
    actions = wf.launch_actions(project_id)
    if not actions:
        return (f'<h2>Phase 5: Launch drafts</h2><form method="post" action="/projects/{project_id}/run/launch">'
                '<button class="primary" type="submit">Draft store page and launch posts</button></form>')
    colour = {"pending": "amber", "approved": "green", "executed": "green", "rejected": "red"}
    parts = ["<h2>Phase 5: approval queue</h2>"
             "<p>Nothing here has been published. Approve writes the exact text below to a copy-ready file "
             "for you to post yourself; Foundry cannot post, send or spend.</p>"]
    for blocked in state.get("launch_blocked") or []:
        parts.append(f'<div class="box warn"><strong>Blocked by guardrails, not queued:</strong> {e(blocked)}</div>')
    for a in actions:
        status = a.status.value
        parts.append(
            f'<div class="box"><p><span class="b {colour.get(status, "grey")}">{e(status.upper())}</span> '
            f"<strong>{e(a.title)}</strong> · type <code>{e(a.action_type.value)}</code> · target {e(a.target)}</p>"
            f'<pre style="white-space:pre-wrap;background:#f5f5f5;padding:.8rem">{e(a.body)}</pre>'
            f"<small>Content SHA-256: <code>{e(a.content_hash)}</code></small>"
        )
        if status == "pending":
            parts.append(
                f'<form method="post" action="/projects/{project_id}/actions/{e(a.id)}/approve">'
                f'<input type="hidden" name="seen_hash" value="{e(a.content_hash)}">'
                '<button class="primary" type="submit">Approve this exact text</button></form>'
                f'<form method="post" action="/projects/{project_id}/actions/{e(a.id)}/reject">'
                '<input type="text" name="reason" placeholder="Reason (optional)">'
                '<button class="stop" type="submit">Reject</button></form>'
            )
        elif a.rejection_reason:
            parts.append(f"<p><small>Reason: {e(a.rejection_reason)}</small></p>")
        parts.append("</div>")
    delivery = _read_output("payment_test_log.md")
    if delivery:
        parts.append('<details class="box doc"><summary>Delivery test log (<strong>MOCKED</strong>, no payment took place)'
                     f"</summary>{render_markdown(delivery)}</details>")
    parts.append(
        f'<div class="box gate"><h2>Gate: Launch</h2><p>Stage: {e(str(state.get("launch_stage", "")))}. '
        "Continue is only accepted when every draft is reviewed and at least one is approved.</p>"
        f'<form method="post" action="/projects/{project_id}/launch-gate">'
        '<button class="primary" name="choice" value="continue">Continue</button>'
        '<button name="choice" value="repeat">Repeat</button><button name="choice" value="go_back">Go back</button>'
        '<button class="stop" name="choice" value="stop">Stop</button></form></div>'
    )
    return "".join(parts)


@app.post("/projects/{project_id}/actions/{action_id}/approve")
def approve_action(project_id: str, action_id: str, seen_hash: str = Form(...)):
    """Founder approval. The form carries the hash of the text that was on screen."""
    try:
        get_workflow().approve_and_export(action_id, seen_hash=seen_hash)
    except (WorkflowError, OutboxError, PolicyViolationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/actions/{action_id}/reject")
def reject_action(project_id: str, action_id: str, reason: str = Form("")):
    try:
        get_workflow().reject(action_id, reason.strip())
    except OutboxError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/launch-gate")
def launch_gate(project_id: str, choice: str = Form(...)):
    try:
        get_workflow().decide_launch_gate(project_id, choice)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/decide")
def decide(project_id: str, choice: str = Form(""), reply: str = Form("")):
    """The only way a gate is passed: a founder action in this UI."""
    wf = get_workflow()
    selected = choice if choice in STEP_CHOICES else parse_reply(reply)
    if selected is None:
        return RedirectResponse(f"/projects/{project_id}?unclear={quote(reply[:60])}", status_code=303)
    try:
        state = wf.decide_step(project_id, selected)
    except (WorkflowError, OrchestratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state.get("awaiting") == "run_step2":
        _start_step(project_id, "step2")
    elif state.get("awaiting") == "run_step1":
        _start_step(project_id, "step1")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/run/{step}")
def run_step(project_id: str, step: str):
    wf = get_workflow()
    state = wf.get_state(project_id)
    if step == "launch":
        if not state.get("discover_approved") or wf.launch_actions(project_id):
            raise HTTPException(status_code=409, detail="Launch drafts cannot be started now.")
        _start_step(project_id, "launch")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    expected = {"step1": "run_step1", "step2": "run_step2"}.get(step)
    if expected is None or state.get("awaiting") != expected:
        raise HTTPException(status_code=409, detail="That step is not waiting to run.")
    _start_step(project_id, step)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/brief")
def revise_brief(
    project_id: str, idea: str = Form(...), skills: str = Form(""), budget: str = Form(""),
    time_available: str = Form(""), target_market: str = Form(""), values: str = Form(""),
    source_urls: str = Form(""),
):
    try:
        brief = _brief_from_form(idea, skills, budget, time_available, target_market, values, source_urls)
        get_workflow().revise_brief(project_id, brief)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _start_step(project_id, "step1")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
