"""Foundry web app.

- `/`: the founder submits a brief and sees their projects.
- `/projects/<id>`: the current step, its output, the system's recommendation and the four
  choices. A step runs in the background; nothing moves past a gate without a founder choice.
- `/status`: live checks (model, search, decision log, guardrail evals) and the status table.

Working: brief intake and Phase 1 (Discover). Later phases are STUBBED and labelled so.
Page layout, styles and result views live in `ui.py`; this file holds the routes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import threading
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import ui
from agents.intake import FounderBrief
from approval import OutboxError
from evidence import ClaimType
from guardrails import PolicyContext, PolicyViolationError
from orchestrator import Orchestrator, OrchestratorError, ProjectRecord
from tools import search_status
from workflow import STEP_CHOICES, Workflow, WorkflowError, outputs_dir

from decision_log import DecisionLog
from llm import LLMAdapter, LLMConfig
from evals.redteam import run_redteam_suite

ROOT = Path(__file__).resolve().parent
DECISION_LOG = Path(os.environ.get("FOUNDRY_DECISION_LOG", ROOT / "decision_log.json"))

# Single source for status labels shown in the app. Keep in step with the README table.
STATUS_ROWS: list[tuple[str, str, str]] = [
    ("Evidence store (sourced claim or ASSUMPTION)", "Working", "evidence.py"),
    ("Decision log with hash chain", "Working", "decision_log.py"),
    ("Approval queue with signed single-use tokens", "Working", "approval.py"),
    ("Six-phase state machine and gates", "Working", "orchestrator.py, gates.py"),
    ("Guardrails: keyword backstop, secrets detector, tool allowlist", "Working", "guardrails.py"),
    ("Guardrails: model classifier on founder requests and outbound drafts, fails closed", "Working", "guardrails.py, llm.py"),
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
e = ui.esc


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
                    "expect": r.expect,
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


# ---------------------------------------------------------------------- shared page helpers


def _page(title: str, body: str, *, refresh: int | None = None, model_ready: bool | None = None) -> str:
    """Every founder page shows the configured model in the header (read from settings, no network call)."""
    return ui.page(title, body, refresh=refresh, model=LLMConfig().model, model_ready=model_ready)


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith(("/api", "/healthz"))


def _error_page(request: Request, status: int, detail: Any) -> HTMLResponse:
    titles = {
        404: "We could not find that page",
        409: "That action is not available right now",
        422: "Please check what you entered",
    }
    if isinstance(detail, list):
        names = {"idea": "Your idea", "target_market": "Who is it for", "skills": "Your skills", "budget": "Budget",
                 "time_available": "Time you have", "values": "Your values", "source_urls": "Source URLs"}
        lines = []
        for err in detail:
            loc = [str(part) for part in err.get("loc", []) if part not in ("body", "query", "path")]
            field = names.get(loc[0], loc[0]) if loc else "Form"
            lines.append(f"<li><strong>{e(field)}:</strong> {e(err.get('msg', 'invalid value'))}</li>")
        body = f'<ul class="points">{"".join(lines)}</ul>'
    else:
        body = f"<p>{e(detail)}</p>"
    parts = request.url.path.split("/")
    back = (f'<a class="btn btn-primary" href="/projects/{e(parts[2])}">Back to your project</a>'
            if len(parts) > 2 and parts[1] == "projects" and parts[2] else "")
    content = (
        f'<section class="decision warn" style="max-width:720px;margin:40px auto 0"><p class="kicker">Error {status}</p>'
        f'<h2>{e(titles.get(status, "Something went wrong"))}</h2><div style="margin-top:14px">{body}</div>'
        f'<div class="row" style="margin-top:22px">{back}<a class="btn" href="/">Go to the home page</a></div></section>'
    )
    return HTMLResponse(_page(titles.get(status, "Error"), content), status_code=status)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    return _error_page(request, exc.status_code, exc.detail)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return await request_validation_exception_handler(request, exc)
    return _error_page(request, 422, list(exc.errors()))


# ---------------------------------------------------------------------- status page


def _badge(ok: bool | None, yes: str, no: str, unknown: str = "n/a") -> str:
    if ok is None:
        return ui.pill(unknown, "muted")
    return ui.pill(yes if ok else no, "purple" if ok else "orange")


@app.get("/status", response_class=HTMLResponse)
def status_page() -> str:
    st = collect_status()
    llm = st["llm"]

    if llm["is_mock"]:
        llm_line = ui.pill("MOCKED: offline test provider, not a real model", "orange")
    elif not llm["ollama_reachable"]:
        llm_line = ui.pill("Ollama not reachable - model calls will fail (no mock fallback)", "orange")
    elif not llm["model_downloaded"]:
        llm_line = ui.pill("Model still downloading - model calls will fail until it finishes", "orange")
    else:
        llm_line = ui.pill("Real local model ready", "solid")

    info = st["decision_log"]
    errs = "<br>".join(e(x) for x in info["errors"])
    log_rows = (
        f"<tr><td>decision_log.json</td><td>{_badge(info['valid'], 'VALID', 'INVALID', 'not created yet')}</td>"
        f"<td>{info['entries']}</td><td><code>{e(info['head'][:16])}</code></td><td><code>{e(info['path'])}</code>"
        f"{'<br>' + errs if errs else ''}</td></tr>"
    )
    rt = st["redteam"]
    rt_rows = "".join(
        f"<tr><td><code>{e(c['case_id'])}</code></td><td>{e(c['name'])}</td><td>{e(c['prompt'])}</td>"
        f"<td>{e(c['expect'])}</td><td>{'yes' if c['blocked'] else 'no'}</td>"
        f"<td><code>{e(c['rule_id'] or '')}</code></td><td>{_badge(c['passed'], 'PASS', 'FAIL')}</td></tr>"
        for c in rt["cases"]
    )
    tone = {"Working": "purple", "STUBBED": "orange", "MOCKED": "orange", "None": "muted"}
    caps = "".join(
        f"<tr><td>{e(p)}</td><td>{ui.pill(s, tone[s])}</td><td>{e(w)}</td></tr>" for p, s, w in STATUS_ROWS
    )
    search_ok = st["search"]["reachable"]
    body = (
        '<p class="kicker">System status</p><h1>Foundry status</h1>'
        '<div class="notice" style="margin-top:16px"><span class="icon orange">i</span><p>Everything on this page is '
        "computed live. Parts marked <strong>STUBBED</strong> are designed but not built; parts marked "
        "<strong>MOCKED</strong> are simulated.</p></div>"
        '<div class="tiles" style="grid-template-columns:repeat(3,1fr)">'
        f'<div class="tile"><p class="label">Local model</p><div class="v" style="font-size:20px">{e(llm["configured_model"])}</div>'
        f"<p>{llm_line}</p><p class=\"small muted\" style=\"margin-top:8px\">Provider <code>{e(llm['active_provider'])}</code>, "
        f"Ollama at <code>{e(llm['ollama_url'])}</code></p></div>"
        f'<div class="tile"><p class="label">Web search</p><div class="v" style="font-size:20px">SearXNG</div>'
        f"<p>{_badge(search_ok, 'SearXNG reachable', 'SearXNG not reachable - research uses pasted URLs only')}</p>"
        f'<p class="small muted" style="margin-top:8px"><code>{e(st["search"]["searxng_url"])}</code></p></div>'
        f'<div class="tile"><p class="label">Guardrail evals, rules only</p><div class="v" style="font-size:20px">'
        f"{rt['passed']}/{rt['total']} passed</div><p>{_badge(rt['passed'] == rt['total'], 'ALL PASS', 'FAILURES')}</p></div>"
        "</div>"
        '<section class="block"><div class="block-head"><h2>Decision log (required keys and hash chain verified now)</h2></div>'
        '<div class="table-wrap"><table><tr><th>Log</th><th>Status</th><th>Entries</th><th>Head hash</th><th>Path</th></tr>'
        f"{log_rows}</table></div></section>"
        f'<section class="block"><div class="block-head"><h2>Guardrail evals, rules only (run now): {rt["passed"]}/{rt["total"]} passed</h2></div>'
        '<p class="muted">These run without the model: the keyword backstop, secrets detector and tool allowlist. Rewordings and '
        "other languages are checked by the model classifier; see <code>evals/results.md</code> for the full run.</p>"
        '<div class="table-wrap" style="margin-top:12px"><table><tr><th>Case</th><th>What it tries</th><th>Text</th><th>Expected</th>'
        f"<th>Blocked</th><th>Rule</th><th>Result</th></tr>{rt_rows}</table></div></section>"
        '<section class="block"><div class="block-head"><h2>What works and what is mocked</h2></div>'
        f'<div class="table-wrap"><table><tr><th>Part</th><th>Status</th><th>Where</th></tr>{caps}</table></div></section>'
    )
    return _page("Status", body, model_ready=bool(llm["ready"]))


# ---------------------------------------------------------------------- workflow plumbing

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


def render_markdown(text: str) -> str:
    """Model output is escaped first, so it can never inject HTML into the page."""
    return ui.doc_html(text)


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


def _brief_form(brief: dict[str, Any] | None = None, action: str = "/projects", submit: str = "Challenge my idea") -> str:
    return ui.brief_form(brief, action, submit)


def _brief_from_form(idea, skills, budget, time_available, target_market, values, source_urls) -> FounderBrief:
    urls = [u.strip() for u in source_urls.splitlines() if u.strip().startswith(("http://", "https://"))]
    return FounderBrief(
        idea=idea.strip(), skills=skills.strip(), budget=budget.strip(),
        time_available=time_available.strip(), target_market=target_market.strip(),
        values=values.strip(), source_urls=urls[:5],
    )


# ---------------------------------------------------------------------- landing page


def _stage_tone(label: str) -> str:
    lowered = label.lower()
    if "decision" in lowered or "revised brief" in lowered:
        return "solid"
    if lowered.startswith("rejected") or "not supported" in lowered or "failed" in lowered:
        return "orange"
    if "stopped" in lowered:
        return "muted"
    return "purple"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    wf = get_workflow()
    llm = LLMAdapter().runtime_status()
    if llm["is_mock"]:
        model = ui.alert("MOCKED: the offline test provider is active, not a real model.", "")
    elif not llm["ready"]:
        model = ui.alert("The local model is not ready yet",
                         "<p>It is still downloading, or Ollama is not reachable. Steps will fail until it is. "
                         'See <a href="/status">System status</a>.</p>')
    else:
        model = ""
    cards = "".join(
        f'<a class="proj" href="/projects/{e(p.id)}"><strong>{e(p.name)}</strong>'
        f'<span>{ui.pill(label, _stage_tone(label))}</span><span class="id">{e(p.id)}</span></a>'
        for p in wf.orch.list_projects()
        for label in [_stage_label(wf.get_state(p.id), p.status.value)]
    )
    projects = (
        '<div class="section-head" id="projects"><div><p class="kicker">Your work</p><h2>Your projects</h2></div></div>'
        f'<div class="proj-list">{cards}</div>'
        if cards else ""
    )
    ready = (f'<span class="eyebrow"><span class="dot"></span>Your AI co-founder &middot; running locally on '
             f'<strong>{e(llm["configured_model"])}</strong></span>' if llm["ready"] and not llm["is_mock"]
             else '<span class="eyebrow">Your AI co-founder, running on your machine</span>')
    how = "".join(
        f'<div class="step"><div class="n">{n}</div><h3>{e(t)}</h3><p>{e(d)}</p></div>'
        for n, t, d in (
            (1, "Screen and research", "Checks the product type, searches the web and reads real pages."),
            (2, "Score the options", "Sharpens your idea and scores it against two alternatives on the same four criteria."),
            (3, "Argue against it", "A critic lists the risks, kill criteria and the cheapest test that could prove it wrong."),
            (4, "You decide", "Continue, repeat, go back or stop. Nothing is published without your approval."),
        )
    )
    body = (
        f'<section class="hero">{ready}'
        '<h1>Built to challenge your idea, <span class="hl">not flatter it.</span></h1>'
        '<p class="lead">Foundry researches your idea against real, fetched sources, scores it next to sharper '
        "alternatives and has a critic argue against it. When the evidence is weak, it says stop. "
        "You make every decision.</p></section>"
        f'{model}<section class="composer" id="new"><h2>Tell us about your idea</h2>'
        '<p class="sub">Start with one or two sentences. The more context you add, the sharper the critique.</p>'
        f"{_brief_form()}</section>"
        f'<section class="how" aria-label="How Foundry works">{how}</section>'
        f"{projects}"
        '<div class="notice" style="margin-top:40px"><span class="icon orange">i</span><p><strong>What this prototype does.</strong> '
        "Working: Discover (research, critic review, interview script) and Launch drafts with your approval. "
        "<strong>STUBBED</strong> (designed, not built): Validate, Build, Commercial setup, Operate. "
        "Delivery is <strong>MOCKED</strong>. There are no payments.</p></div>"
    )
    return _page("Challenge your idea", body, model_ready=bool(llm["ready"]))


def _stage_label(state: dict[str, Any], status: str) -> str:
    if state.get("rejected"):
        return "Rejected: not a digital product"
    if status == "stopped":
        return "Stopped by you"
    awaiting = state.get("awaiting")
    labels = {
        "step1_gate": "Waiting for your decision on the opportunities",
        "phase_gate": "Waiting for your decision on Discover",
        "excluded_gate": "Product type not supported",
        "revise_brief": "Waiting for a revised brief",
        "run_step1": "Ready to research", "run_step2": "Ready to draft the interview script",
    }
    if awaiting in labels:
        return labels[awaiting]
    if str(state.get("launch_stage", "")).startswith("gate decided"):
        return "Launch reviewed"
    if state.get("discover_approved"):
        return "Discover approved; launch drafts"
    return str(state.get("stage", "")) or "New"


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
    try:
        project = get_workflow().start_project(brief)
    except WorkflowError as exc:
        return HTMLResponse(_page("Brief refused", (
            '<div style="max-width:720px;margin:40px auto 0">'
            + ui.alert("This brief was not saved", f"<p>{e(exc)}</p>")
            + '<p style="margin-top:18px"><a class="btn" href="/">Back</a></p></div>')), status_code=400)
    _start_step(project.id, "step1")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


# ---------------------------------------------------------------------- project page


def _read_output(name: str, project_id: str | None = None) -> str:
    """Read an output file. With a project id, read it from that project's own folder
    (see `Workflow.project_out_dir`); every run after the first has its own folder."""
    folder = get_workflow().project_out_dir(project_id) if project_id else outputs_dir()
    path = folder / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _own_output(project_id: str, name: str) -> str:
    """The text of an output file only if it was written for this project.

    Output files have fixed names, so a newer project's run replaces them; showing that file
    on an older project's page would show the wrong project's results."""
    text = _read_output(name, project_id)
    return text if f"`{project_id}`" in text else ""


def _doc(project_id: str, name: str, title: str = "", open_: bool = True) -> str:
    text = _own_output(project_id, name)
    if not text:
        if _read_output(name, project_id) and title:
            return (f'<details class="fold"><summary>{e(title)}</summary><div class="fold-body"><p class="muted">'
                    f"<code>outputs/{e(name)}</code> now holds a newer project's results, so it is not shown here."
                    "</p></div></details>")
        return ""
    if title:
        return (f'<details class="fold"{" open" if open_ else ""}><summary>{e(title)}</summary>'
                f'<div class="fold-body">{render_markdown(text)}</div></details>')
    return f'<div class="results">{render_markdown(text)}</div>'


def _opportunities(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    """Step 1 results built from the data the agents saved for this project (the same data the
    markdown file was written from). Falls back to the markdown file if anything is missing."""
    fallback = _doc(project.id, "phase1_step1_opportunities.md")
    artifact = project.phase_artifacts.get("discover") or {}
    research, critic = artifact.get("research"), artifact.get("critic")
    if not research or not critic:
        return fallback
    try:
        drafts = research.get("claims") or []
        stored = wf.ledger.list_claims(project_id=project.id, linked_phase="discover")
        claims = stored[-len(drafts):] if drafts else []
        if len(claims) != len(drafts) or any(c.text != d["text"].strip() for c, d in zip(claims, drafts)):
            return fallback
        cited = {}
        for claim in claims:
            if claim.claim_type == ClaimType.FACT and claim.source_url:
                page_ = wf.ledger.get_fetch(project_id=project.id, url=claim.source_url)
                if page_ is not None:
                    cited[claim.source_url] = page_
        note = ""
        for line in _own_output(project.id, "phase1_step1_opportunities.md").splitlines():
            if line.startswith("Research notes: "):
                note = line[len("Research notes: "):]
        return ui.opportunities_view(
            project_id=project.id, research=research, critic=critic,
            picked_title=(artifact.get("recommended_option") or {}).get("title", ""),
            claims=claims, cited_pages=cited, all_pages=wf.ledger.list_fetches(project_id=project.id),
            recommendation=str(state.get("recommendation", "")), reason=str(state.get("recommendation_reason", "")),
            search_note=note,
        )
    except (KeyError, TypeError, ValueError):
        return fallback


def _choice_buttons(project_id: str, allowed: tuple[str, ...], recommended: str = "",
                    action: str = "decide", typed: bool = True) -> str:
    return ui.choice_buttons(project_id, STEP_CHOICES, allowed, recommended, action, typed)


def _stepper(state: dict[str, Any], status: str) -> str:
    awaiting = state.get("awaiting")
    approved = bool(state.get("discover_approved"))
    launch_done = str(state.get("launch_stage", "")).startswith("gate decided")
    step1_done = approved or awaiting in {"run_step2", "phase_gate"} or state.get("stage") == "drafting the interview script"
    items = [
        ("Brief", "done"),
        ("1 Discover: opportunities", "done" if step1_done else "now"),
        ("1 Discover: interview script", "done" if approved else ("now" if step1_done else "")),
        ("2 Validate", "stub"), ("3 Build", "stub"), ("4 Commercial setup", "stub"),
        ("5 Launch: drafts and approval", "done" if launch_done else ("now" if approved else "")),
        ("6 Operate", "stub"),
    ]
    if status == "stopped":
        items = [(name, "done" if css == "done" else ("stub" if css == "stub" else "")) for name, css in items]
    return ui.stepper(items)


def _running_kind(state: dict[str, Any]) -> str:
    if state.get("discover_approved"):
        return "launch"
    if state.get("stage") == "drafting the interview script":
        return "step2"
    return "step1"


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(project_id: str, unclear: str = "") -> str:
    wf = get_workflow()
    project = wf.orch.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    state = wf.get_state(project_id)
    awaiting = state.get("awaiting")
    usage = state.get("usage") or {}
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    brief = project.brief or {}
    brief_rows = "".join(
        f"<tr><th>{label}</th><td>{e(brief.get(key) or 'not given')}</td></tr>"
        for key, label in (("idea", "Idea"), ("target_market", "For"), ("skills", "Skills"),
                           ("budget", "Budget"), ("time_available", "Time"), ("values", "Values"))
    )
    label = _stage_label(state, project.status.value)
    parts = [
        '<a class="back" href="/#projects">&larr; All projects</a>',
        f'<div class="proj-head"><div><p class="kicker">Your project</p><h1>{e(project.name)}</h1></div>'
        f"{ui.pill(label, _stage_tone(label))}</div>",
        _stepper(state, project.status.value),
        f'<details class="fold"><summary>Your brief <span class="muted" style="font-weight:500">(what you entered)</span></summary>'
        f'<div class="fold-body"><table class="brief-table">{brief_rows}</table></div></details>',
    ]
    footer = (f'<p class="page-foot"><span>Cost so far: $0.00 ({tokens} tokens on the local model).</span>'
              f"<span>Project id {e(project.id)}.</span></p>")
    notices: list[str] = []
    if state.get("error"):
        headline, advice = ui.friendly_error(str(state["error"]))
        notices.append(ui.alert(
            "The step failed.",
            f"<p>{e(headline)} {e(advice)} Nothing was made up to cover for it. You can run the step again.</p>"
            f'<details><summary>Technical details</summary><pre>{e(state["error"])}</pre></details>',
        ))
    if unclear and state.get("reply_refusal"):
        notices.append(ui.alert("Foundry will not do this", f'<p>{e(state["reply_refusal"])}</p>'))
    if unclear:
        notices.append(ui.alert(
            "Please choose one option",
            f'<p>Your reply "{e(unclear)}" is not a clear choice, so nothing was done. '
            "Please choose one of: continue, repeat, go back, stop.</p>",
        ))
    if notices:
        parts.append('<div class="stack" style="margin-bottom:16px">' + "".join(notices) + "</div>")

    if project_id in _running:
        stage = state.get("launch_stage") if state.get("discover_approved") else state.get("stage")
        parts.append(ui.working_card(str(stage or "starting"), _running_kind(state)))
        return _page(project.name, "".join(parts) + footer, refresh=3)

    rec = str(state.get("recommendation", ""))
    if awaiting == "step1_gate":
        parts.append(ui.decision_panel(
            "Your decision: the opportunities", state,
            "Read the research below. Foundry recommends; you decide. Nothing continues until you choose.",
            _choice_buttons(project_id, STEP_CHOICES, rec), kicker="Gate · Discover, step 1",
        ))
        parts.append(_opportunities(wf, project, state))
    elif awaiting == "phase_gate":
        parts.append(ui.decision_panel(
            "Your decision: end of Discover", state,
            "Continue approves the sharpened idea. The interview script below is for you to use with real people.",
            _choice_buttons(project_id, STEP_CHOICES, rec), kicker="Gate · End of Discover",
        ))
        parts.append(_doc(project_id, "phase1_step2_interview_script.md"))
        parts.append(_research_fold(wf, project, state))
    elif awaiting == "excluded_gate":
        parts.append(
            '<section class="decision warn"><p class="kicker">Not supported</p><h2>This product type is not supported</h2>'
            f"{ui.recommendation(state)}{_choice_buttons(project_id, ('stop',), typed=False)}"
            '<p style="margin-top:14px"><a href="/#new">Start a different brief</a></p></section>'
        )
    elif awaiting in {"run_step1", "run_step2"}:
        step = "step1" if awaiting == "run_step1" else "step2"
        label_ = "Run the research" if step == "step1" else "Draft the interview script"
        what = ("Foundry will screen your brief, search and read sources, score the options and run the critic."
                if step == "step1" else "Foundry will draft a Mom Test interview script for the option you approved.")
        parts.append(
            f'<section class="decision"><p class="kicker">Next step</p><h2>{e(label_)}</h2><p class="hint">{e(what)}</p>'
            f'<form method="post" action="/projects/{e(project_id)}/run/{step}" style="margin-top:18px">'
            f'<button class="btn btn-primary btn-lg" type="submit">{e(label_)} <span class="arrow">&rarr;</span></button></form></section>'
        )
    elif awaiting == "revise_brief":
        if state.get("refusal"):
            parts.append(
                '<section class="decision warn"><p class="kicker">Refused</p><h2>Foundry will not do this</h2>'
                f'<p style="margin-top:12px">{e(state["refusal"])}</p>'
                '<p class="hint">The refusal is recorded in the decision log. Change the brief below, or stop.</p>'
                f"{_choice_buttons(project_id, ('stop',), typed=False)}</section>"
            )
        parts.append('<section class="composer" style="margin-top:20px;max-width:none"><h2>Revise your brief</h2>'
                     '<p class="sub">Change anything, or paste source URLs for the agents to read, then run the research again.</p>'
                     + _brief_form(project.brief, f"/projects/{project_id}/brief", "Research again") + "</section>")
    elif state.get("rejected"):
        parts.append(
            '<section class="decision warn"><p class="kicker">Rejected</p><h2>This idea was rejected</h2>'
            f'<p style="margin-top:12px">{e(state.get("recommendation_reason", ""))}</p>'
            '<p class="hint">No research was run. The rejection is recorded in the decision log.</p>'
            '<a class="btn btn-primary" href="/#new" style="margin-top:18px">Try a digital product idea</a></section>'
        )
    elif project.status.value == "stopped":
        parts.append('<section class="decision"><p class="kicker">Stopped</p><h2>You stopped this project.</h2>'
                     '<p class="hint">Stopping on weak evidence is a result, not a failure.</p>'
                     '<a class="btn btn-primary" href="/#new" style="margin-top:18px">Start a new brief</a></section>')
        parts.append(_doc(project_id, "phase1_gate_decision.md"))
    elif state.get("discover_approved"):
        parts.append(_launch_section(wf, project, state))
    return _page(project.name, "".join(parts) + footer)


def _research_fold(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    view = _opportunities(wf, project, state)
    if not view:
        return ""
    return f'<details class="fold" style="margin-top:20px"><summary>Research from step 1</summary><div class="fold-body">{view}</div></details>'


def _launch_section(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    project_id = project.id
    actions = wf.launch_actions(project_id)
    stub = ('<div class="notice" style="margin-bottom:16px"><span class="icon orange">i</span><p><strong>STUBBED:</strong> '
            "Validate, Build and Commercial setup are designed but not built in this prototype, so launch drafts are "
            "based on the Discover result only.</p></div>")
    if not actions:
        return (stub + '<section class="decision"><p class="kicker">Next step</p><h2>Discover is approved</h2>'
                '<p class="hint">Next, the launch writer drafts a store page and launch posts. Each draft is checked by the '
                "guardrails and then waits for your approval. Nothing is published by Foundry.</p>"
                f'<form method="post" action="/projects/{e(project_id)}/run/launch" style="margin-top:18px">'
                '<button class="btn btn-primary btn-lg" type="submit">Draft store page and launch posts '
                '<span class="arrow">&rarr;</span></button></form></section>'
                + _doc(project_id, "phase1_step2_interview_script.md", "Interview script", open_=False)
                + _research_fold(wf, project, state))
    tone = {"pending": "solid-orange", "approved": "solid", "executed": "solid", "rejected": "muted"}
    names = {"pending": "WAITING FOR YOU", "approved": "APPROVED", "executed": "APPROVED AND EXPORTED", "rejected": "REJECTED"}
    pending = sum(1 for a in actions if a.status.value == "pending")
    decided = str(state.get("launch_stage", "")).startswith("gate decided")
    parts = [stub, '<div class="block-head"><div><p class="kicker">Approval screen</p><h2>Launch drafts: your approval</h2></div></div>'
             '<p class="muted">Nothing here has been published. Approving writes the exact text shown to a copy-ready '
             "file for you to post yourself. Foundry cannot post, send or spend.</p>"]
    for blocked in state.get("launch_blocked") or []:
        parts.append('<div style="margin-top:14px">'
                     + ui.alert("One draft was blocked by the guardrails and was not queued.", f"<p>{e(blocked)}</p>") + "</div>")
    for a in actions:
        status = a.status.value
        card = (
            '<article class="card draft-card" style="margin-top:16px"><div class="head">'
            f"<h3>{e(a.title)}</h3>{ui.pill(names.get(status, status.upper()), tone.get(status, 'muted'))}</div>"
            f'<pre class="draft">{e(a.body)}</pre>'
            f'<p class="fingerprint">Fingerprint of this exact text: <code>{e(a.content_hash[:16])}</code></p>'
        )
        if status == "pending":
            card += (
                '<div class="row" style="margin-top:14px">'
                f'<form method="post" action="/projects/{e(project_id)}/actions/{e(a.id)}/approve">'
                f'<input type="hidden" name="seen_hash" value="{e(a.content_hash)}">'
                '<button class="btn btn-primary" type="submit">Approve this exact text</button></form>'
                f'<form class="row" method="post" action="/projects/{e(project_id)}/actions/{e(a.id)}/reject">'
                '<input type="text" name="reason" style="width:15rem" placeholder="Reason (optional)">'
                '<button class="btn btn-stop" type="submit">Reject</button></form></div>'
            )
        elif a.rejection_reason:
            card += f'<p class="muted small">Reason: {e(a.rejection_reason)}</p>'
        parts.append(card + "</article>")
    if decided:
        choice = str(state.get("launch_stage", "")).split(":")[-1].strip()
        parts.append('<section class="decision" style="margin-top:20px"><p class="kicker">Done</p><h2>Launch review finished</h2>'
                     f'<p style="margin-top:12px">Your decision: {ui.pill(choice.upper(), "solid")}. Approved drafts are saved as '
                     "copy-ready text files in the <code>outputs</code> folder for you to post yourself.</p>"
                     '<p class="hint">This is as far as the prototype goes: Operate is STUBBED.</p>'
                     '<a class="btn btn-primary" href="/#new" style="margin-top:18px">Start a new brief</a></section>')
    elif pending:
        parts.append(f'<div class="notice" style="margin-top:16px"><span class="icon purple">{pending}</span><p>'
                     f'<strong>{pending} draft{"s" if pending != 1 else ""} still waiting for you.</strong> '
                     '<span class="muted">Approve or reject each one to finish this step.</span></p></div>')
    else:
        parts.append('<section class="decision" style="margin-top:20px"><p class="kicker">Your decision</p><h2>Your decision: Launch</h2>'
                     '<p class="hint">Every draft is reviewed. Continue is accepted only if at least one was approved.</p>'
                     f"{_choice_buttons(project_id, STEP_CHOICES, action='launch-gate', typed=False)}</section>")
    parts.append('<div class="stack" style="margin-top:20px">'
                 + _doc(project_id, "payment_test_log.md", "Delivery test log (MOCKED, no payment took place)", open_=False)
                 + _doc(project_id, "phase1_step2_interview_script.md", "Interview script", open_=False)
                 + _research_fold(wf, project, state) + "</div>")
    return "".join(parts)


# ---------------------------------------------------------------------- founder actions


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
    if selected is None and reply.strip():
        # A typed message is never an approval. If it asks for something forbidden, say so.
        check = wf.engine.evaluate_text(
            reply, PolicyContext(project_id=project_id, surface="founder_request"), use_classifier=False
        )
        if not check.allowed:
            wf._set_state(project_id, reply_refusal=check.founder_prompt)
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
