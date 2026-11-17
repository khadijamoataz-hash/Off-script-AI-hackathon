"""Foundry web app.

- `/`: the founder submits a brief and sees their projects.
- `/projects/<id>`: the current step, its output, the system's recommendation and the four
  choices. A step runs in the background; nothing moves past a gate without a founder choice.
- `/status`: live checks (model, search, decision log, guardrail evals) and the status table.

Working: Discover, Validate, Commercial setup, Launch drafts and a recorded-data learning review.
Build and live operating integrations are STUBBED.
Page layout, styles and result views live in `ui.py`; this file holds the routes.
"""

from __future__ import annotations

import os
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import threading
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import ui
from agents.intake import FounderBrief
from approval import OutboxError
from evidence import ClaimType, EvidenceLedger, quote_in_text
from guardrails import PolicyContext, PolicyViolationError
from orchestrator import Orchestrator, OrchestratorError, ProjectRecord
from tools import search_status
from workflow import STEP_CHOICES, Workflow, WorkflowError, outputs_dir

from decision_log import DecisionEntry, DecisionLog
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
    ("Guardrails: model checks founder requests, Discover outputs, learning memos and outbound drafts; fails closed", "Working", "guardrails.py, llm.py"),
    ("Local model adapter (Ollama)", "Working", "llm.py"),
    ("Founder brief intake, with excluded product types refused", "Working", "agents/intake.py, app.py"),
    ("Web search (self-hosted SearXNG) and page fetch with quote check", "Working", "tools.py"),
    ("Phase 1 Discover: opportunities, critic review, interview script", "Working", "agents/researcher.py, agents/critic.py, workflow.py"),
    ("Step gates in the browser: continue, repeat, go back, stop", "Working", "app.py, workflow.py"),
    ("Phase 2 Validate: Lean Canvas, landing page, demand target fixed in advance, STOP on weak demand", "Working", "agents/strategist.py, workflow.py"),
    ("Phase 4 Commercial setup: price options with sourced comparables, terms, privacy and refund policy drafts approved as sign actions", "Working", "agents/pricing.py, workflow.py"),
    ("Phase 3 Build", "STUBBED", "agent not built"),
    ("Phase 6 Operate: recorded-data learning review, no live analytics", "Working", "agents/operator.py, workflow.py"),
    ("Phase 5 Launch: store page and launch post drafts into the approval queue", "Working", "agents/launcher.py, workflow.py"),
    ("Approving or rejecting the exact text in the browser, export as copy-ready text", "Working", "app.py, approval.py"),
    ("Delivery to buyers", "MOCKED", "delivery.py: simulated order, idempotent delivery, refund revokes access"),
    ("Posting to social, email or store sites", "STUBBED", "by design: copy-ready text only"),
    ("Payments", "None", "no payment code exists"),
]

app = FastAPI(title="Foundry", version="0.1.0")
# Brand images (logo). Served from the repository, so Docker includes them through `COPY . .`.
app.mount("/static", StaticFiles(directory=ROOT / "static", check_dir=False), name="static")
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
    return _page("Status", body, model_ready=None if llm["is_mock"] else bool(llm["ready"]))


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
            elif step == "validate":
                wf.run_validate_step1(project_id)
            elif step == "commercial":
                wf.run_commercial_setup(project_id)
            elif step == "launch":
                wf.run_launch_drafts(project_id)
            elif step == "operate":
                wf.run_operate_review(project_id)
            else:
                raise WorkflowError("Unknown workflow step.")
        except Exception as exc:  # noqa: BLE001 - shown to the founder with a retry button
            failure = f"{type(exc).__name__}: {exc}"
            if step == "operate":
                wf._set_state(project_id, operate_stage="failed", error=failure)
            elif step == "launch":
                wf._set_state(project_id, launch_stage="failed", error=failure)
            elif step == "commercial":
                wf._set_state(project_id, commercial_stage="failed", error=failure)
            elif step == "validate":
                wf._set_state(project_id, validate_stage="ready", error=failure)
            else:
                wf._set_state(
                    project_id, stage="failed", error=failure,
                    awaiting="run_step1" if step == "step1" else "run_step2",
                )
        finally:
            with _workflow_lock:
                _running.discard(project_id)

    if step == "operate":
        wf._set_state(project_id, operate_stage="drafting", error=None)
    elif step == "launch":
        wf._set_state(project_id, launch_stage="starting", error=None)
    elif step == "commercial":
        wf._set_state(project_id, commercial_stage="drafting", error=None)
    elif step == "validate":
        wf._set_state(project_id, validate_stage="drafting", error=None)
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
        "You make every decision.</p>"
        '<p><a class="btn" href="/demo">Explore the recorded demo &rarr;</a></p></section>'
        f'{model}<section class="composer" id="new"><h2>Tell us about your idea</h2>'
        '<p class="sub">Start with one or two sentences. The more context you add, the sharper the critique.</p>'
        f"{_brief_form()}</section>"
        f'<section class="how" aria-label="How Foundry works">{how}</section>'
        f"{projects}"
        '<div class="notice" style="margin-top:40px"><span class="icon orange">i</span><p><strong>What this prototype does.</strong> '
        "Working: Discover (research, critic review, interview script), Validate (Lean Canvas, landing page, "
        "demand test against a target you fix in advance), Commercial setup (price and policy drafts) "
        "and Launch drafts with your approval, followed by a recorded-data learning review. "
        "<strong>STUBBED</strong> (designed, not built): Build and live operating integrations. "
        "Delivery is <strong>MOCKED</strong>. There are no payments.</p></div>"
    )
    return _page("Challenge your idea", body, model_ready=None if llm["is_mock"] else bool(llm["ready"]))


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
    if state.get("operate_stage") == "awaiting decision":
        return "Waiting for your decision on the learning review"
    if state.get("operate_stage") == "drafting":
        return "Drafting the learning review"
    if state.get("operate_stage") == "failed":
        return "Learning review failed; ready to retry"
    if str(state.get("operate_stage", "")).startswith("gate decided"):
        return "Learning review finished"
    if state.get("launch_stage") == "gate decided: continue":
        return "Launch reviewed; learning review next"
    if str(state.get("launch_stage", "")).startswith("gate decided"):
        return "Launch reviewed"
    if state.get("commercial_passed"):
        return "Policies approved; launch drafts"
    if state.get("commercial_stage") == "awaiting approvals":
        return "Waiting for you to review the policies"
    if str(state.get("commercial_stage", "")).startswith("gate decided"):
        return "Commercial setup reviewed"
    if state.get("validate_passed"):
        return "Validate passed; price and policies next"
    if state.get("validate_stage") in ("awaiting_results", "results_recorded"):
        return "Waiting for your demand test results"
    if state.get("discover_approved"):
        return "Discover approved; ready to validate"
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


def _workspace_records(project_id: str) -> dict[str, Any]:
    """Read persisted records without initializing a workflow or its write-capable services."""
    wf = _workflow
    db_path = wf.orch.db_path if wf else Path(os.environ.get("FOUNDRY_DB_PATH", "data/foundry.sqlite3"))
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Project not found")
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project = Orchestrator._row_to_project(row)
        row = conn.execute("SELECT state_json FROM workflow_state WHERE project_id = ?", (project_id,)).fetchone()
        state = json.loads(row["state_json"]) if row else {}
        claims = [
            EvidenceLedger._row_to_claim(row) for row in conn.execute(
                "SELECT * FROM claims WHERE project_id = ? ORDER BY created_at, id", (project_id,)
            )
        ]
        pages = {row["url"]: row for row in conn.execute(
            "SELECT url, text FROM fetched_pages WHERE project_id = ?", (project_id,)
        )}
        approvals = {row["status"]: row["count"] for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM outbox_actions WHERE project_id = ? GROUP BY status",
            (project_id,),
        )}
    # Recheck stored FACTs against this project's fetched page, never a live network request.
    for claim in claims:
        page = pages.get(claim.source_url)
        if claim.claim_type == ClaimType.FACT and (
            page is None or not quote_in_text(claim.quote, page["text"])
        ):
            claim.claim_type = ClaimType.ASSUMPTION
    if wf:
        entries = wf.log.read_all(project_id=project_id)
    elif DECISION_LOG.is_file():
        entries = [
            DecisionEntry(**record) for record in json.loads(DECISION_LOG.read_text(encoding="utf-8"))
            if record.get("project_id") == project_id
        ]
    else:
        entries = []
    return {"project": project, "state": state, "claims": claims, "approvals": approvals, "entries": entries}


@app.get("/demo", response_class=HTMLResponse)
def recorded_demo() -> str:
    """Read committed pipeline artifacts without creating a project or calling a model."""
    root = (ROOT / "outputs").resolve()
    try:
        manifest = json.loads((root / "run_index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="The recorded demo index is unavailable.") from exc
    runs = manifest.get("runs") if isinstance(manifest, dict) else None
    if not isinstance(runs, list) or not runs:
        raise HTTPException(status_code=503, detail="No recorded pipeline runs are available.")
    parts = [
        '<section class="hero"><p class="kicker">Recorded demo · read only</p>'
        '<h1>See the evidence and the decision</h1>'
        '<p class="lead">These are actual committed pipeline outputs, not a new live run. '
        'Opening this page does not call a model, create a project, approve anything or publish.</p></section>',
        ui.alert(
            "What these recordings do and do not show",
            '<p>The first run demonstrates gate mechanics: the landing page was not published, so '
            'its recorded zero is not a completed demand experiment or proof of zero demand. '
            'The second run shows the critic recommending STOP on an idea it judged weak.</p>'
            '<p>Historical outputs are preserved as generated. Some contain mistakes that newer '
            'validators reject; quote provenance alone does not establish a correct interpretation. '
            'Build remains STUBBED, delivery is MOCKED, and no payment took place.</p>',
            tone="info",
        ),
    ]
    documents = (
        ("phase1_gate_decision.md", "Discover gate"),
        ("phase2_gate_decision.md", "Validate gate"),
        ("phase1_step1_opportunities.md", "Research, alternatives and critic review"),
        ("phase1_step2_interview_script.md", "Interview script"),
        ("phase2_step1_lean_canvas.md", "Lean Canvas and demand-test plan"),
    )
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("folder"), str):
            raise HTTPException(status_code=503, detail="The recorded demo index is invalid.")
        directory = (root / run["folder"]).resolve()
        if not directory.is_relative_to(root):
            raise HTTPException(status_code=503, detail="The recorded demo folder is invalid.")
        parts.append(
            '<section class="card" style="margin-top:24px">'
            f'<p class="kicker">Recorded project {e(run.get("project_id", ""))}</p>'
            f'<h2>{e(run.get("idea", "Recorded pipeline run"))}</h2>'
            f'<p class="muted small">Started: {e(run.get("started_at", "not recorded"))}</p></section>'
        )
        found = False
        for name, title in documents:
            path = (directory / name).resolve()
            if not path.is_relative_to(root):
                raise HTTPException(status_code=503, detail="A recorded demo document is invalid.")
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise HTTPException(status_code=503, detail="A recorded demo document is unavailable.") from exc
            found = True
            open_ = " open" if "_gate_" in name else ""
            parts.append(
                f'<details class="fold"{open_}><summary>{e(title)}</summary>'
                f'<div class="fold-body doc">{render_markdown(text)}</div></details>'
            )
        if not found:
            raise HTTPException(status_code=503, detail="A recorded run has no readable artifacts.")
    parts.append('<p style="margin-top:24px"><a class="btn btn-primary" href="/#new">Try your own brief</a></p>')
    return _page("Recorded demo", "".join(parts))


_REPORT_NOTICE = (
    "DRAFT — internal research only. This report is not founder approval or publishing, "
    "does not execute any action, and does not establish market validation or promise success."
)


def _workspace_sections(records: dict[str, Any]) -> list[tuple[str, str]]:
    project, state = records["project"], records["state"]
    stages = "\n".join(
        f"{label}: {state[key]}" for key, label in (
            ("validate_stage", "Validate stage"), ("commercial_stage", "Commercial setup stage"),
            ("launch_stage", "Launch stage"), ("operate_stage", "Learning review stage"),
            ("awaiting", "Waiting for"),
        ) if state.get(key)
    )
    facts = sum(c.claim_type == ClaimType.FACT for c in records["claims"])
    target = state.get("validate_target")
    demand = (
        f"Fixed demand target: {target.get('target')} {target.get('what', '')}"
        if target else "Fixed demand target: not set."
    )
    demand += f"\nRecorded demand signals: {project.recorded_demand_signals}"
    results = state.get("validate_results") or []
    demand += "\n" + ("\n".join(
        f"- {r.get('count')} {r.get('kind', '')}: {r.get('note', '')}" for r in results
    ) if results else "No demand test results recorded.")
    counts = records["approvals"]
    claims = "\n\n".join(
        f"{c.claim_type.value} | {c.id} | {c.linked_phase} | confidence: {c.confidence.value}\n{c.text}\n"
        + (f"Source: {c.source_url}\nSupporting quote: {c.quote}\nRetrieved: {c.retrieved_date}"
           if c.claim_type == ClaimType.FACT else "ASSUMPTION — not verified by a fetched source quote.")
        for c in records["claims"]
    ) or "No claims recorded."
    # Deliberately allowlist fields: approval tokens and internal log metadata are not report content.
    activity = "\n\n".join(
        f"{entry.timestamp} | {entry.phase} | {entry.agent} | {entry.action}\n"
        f"Outcome: {entry.outcome or 'not recorded'} | Approved by: {entry.approved_by or 'not approved'}\n"
        f"{entry.detail}\nSources: {', '.join(entry.sources) or 'none'}\n"
        f"Model: {entry.model or 'none'} | Cost: ${entry.cost:g}"
        for entry in records["entries"]
    ) or "No project actions or decisions recorded."
    return [
        ("Current stage", f"Project: {project.name}\nProject id: {project.id}\n"
         f"Phase: {project.current_phase.value} | Status: {project.status.value}\n"
         f"Stage: {_stage_label(state, project.status.value)}\n"
         f"Persisted workflow stage: {state.get('stage') or 'not recorded'}\n{stages}\n"
         "Build: STUBBED. Operate: recorded-data review; live analytics STUBBED. Delivery: MOCKED. No payments.\n"
         "Commercial setup: Working (price and policy drafts). No live publishing connectors."),
        ("Demand target and results", demand + "\nOnly recorded real demand counts; MOCKED data is not evidence."),
        ("Approval status", "\n".join(
            f"{status}: {counts.get(status, 0)}" for status in ("pending", "approved", "rejected", "executed")
        ) + "\nExecuted means copy-ready export only, not live publishing."),
        ("Evidence", f"Verified FACT: {facts} | ASSUMPTION: {len(records['claims']) - facts}\n"
         "FACT needs a supporting quote on a fetched page. Discover also reviews claim support; "
         "other phases remain provenance-only. Neither is independent proof.\n\n" + claims),
        ("Activity and decisions", activity),
    ]


def _workspace_source(url: str) -> str:
    """Only HTTP(S) sources become links, including when a log contains an untrusted URL."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return e(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return e(url)
    return (f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer" '
            f'style="overflow-wrap:anywhere">{e(url)}</a>')


def _workspace_stage(project: ProjectRecord, state: dict[str, Any]) -> str:
    if project.status.value == "stopped" or state.get("rejected"):
        return _stage_label(state, project.status.value)
    if project.status.value != "active":
        return f"{project.current_phase.value.title()}: {project.status.value}"
    for key, phase in (("operate_stage", "Learning review"), ("launch_stage", "Launch"), ("commercial_stage", "Commercial setup"),
                       ("validate_stage", "Validate")):
        stage = str(state.get(key) or "")
        if not stage:
            continue
        if stage.startswith("gate decided"):
            return f"{phase} reviewed"
        if phase == "Validate" and state.get("validate_passed"):
            return "Validate passed; Commercial setup next"
        if phase == "Commercial setup" and state.get("commercial_passed"):
            return "Policies approved; Launch next"
        labels = {
            "results_recorded": "Demand results recorded; waiting for your decision",
            "awaiting_results": "Waiting for your demand test results",
            "awaiting approvals": f"{phase}: waiting for your approval",
            "drafting": f"{phase}: drafting",
            "starting": f"{phase}: starting",
            "failed": f"{phase}: failed; review the project before retrying",
            "ready": f"{phase}: ready to start",
        }
        return labels.get(stage, f"{phase}: {stage}")
    return _stage_label(state, project.status.value)


@app.get("/projects/{project_id}/workspace", response_class=HTMLResponse)
def project_workspace(project_id: str) -> str:
    records = _workspace_records(project_id)
    project, state = records["project"], records["state"]
    facts = sum(c.claim_type == ClaimType.FACT for c in records["claims"])
    assumptions = len(records["claims"]) - facts
    counts = records["approvals"]
    target, results = state.get("validate_target"), state.get("validate_results") or []
    url = quote(project_id, safe="")
    tiles = "".join(
        f'<div class="tile"><p class="label">{e(label)}</p><div class="v">{e(value)}</div>'
        f'<p class="small muted">{e(note)}</p></div>'
        for label, value, note in (
            ("Verified FACT", facts, "Backed by a fetched source quote"),
            ("ASSUMPTION", assumptions, "Still needs supporting evidence"),
            ("Pending approvals", counts.get("pending", 0), "No action taken by viewing this page"),
            ("Demand recorded", project.recorded_demand_signals if results else "Not recorded",
             f"Fixed target: {target.get('target')}" if target else "Target not set"),
        )
    )
    claims = ""
    for claim in records["claims"]:
        fact = claim.claim_type == ClaimType.FACT
        citation = (
            f'<blockquote class="quote">Supporting quote: {e(claim.quote)}</blockquote>'
            f'<p class="small">Source: {_workspace_source(claim.source_url or "")}</p>'
            f'<p class="small muted">Retrieved: {e(claim.retrieved_date)}</p>'
            if fact else '<p class="small muted">ASSUMPTION — not verified by a fetched source quote.</p>'
        )
        claims += (
            '<li style="list-style:none;margin:0 0 18px">'
            f'<article class="tile">{ui.pill(claim.claim_type.value, "purple" if fact else "orange")}'
            f'<p style="margin-top:12px">{e(claim.text)}</p>{citation}'
            f'<p class="small muted">{e(claim.id)} &middot; {e(claim.linked_phase)} '
            f'&middot; confidence: {e(claim.confidence.value)}</p></article></li>'
        )
    activity = ""
    for entry in reversed(records["entries"]):
        sources = ", ".join(_workspace_source(source) for source in entry.sources) or "none"
        activity += (
            '<li style="list-style:none"><details class="fold"><summary>'
            f'{e(entry.action)} &middot; {e(entry.outcome or "recorded")}'
            f'<span class="small muted"> &mdash; {e(entry.timestamp)}</span></summary>'
            f'<div class="fold-body"><p>{e(entry.detail)}</p>'
            f'<p class="small">Outcome: {e(entry.outcome or "not recorded")} &middot; '
            f'Approved by: {e(entry.approved_by or "not approved")}</p>'
            f'<p class="small muted">{e(entry.phase)} &middot; {e(entry.agent)} &middot; '
            f'Model: {e(entry.model or "none")} &middot; Cost: ${e(f"{entry.cost:g}")}</p>'
            f'<p class="small">Sources: {sources}</p></div></details></li>'
        )
    demand = (
        f'<p>Fixed demand target: {e(target.get("target"))} {e(target.get("what", ""))}</p>'
        if target else "<p>Fixed demand target: not set.</p>"
    )
    if results:
        demand += f'<p>Recorded demand signals: {project.recorded_demand_signals}</p><ul class="points">'
        demand += "".join(
            f'<li>{e(result.get("count"))} {e(result.get("kind", ""))}'
            f'{": " + e(result["note"]) if result.get("note") else ""}</li>' for result in results
        ) + "</ul>"
    else:
        demand += "<p>No demand test results recorded.</p>"
    approvals = " ".join(
        ui.pill(f"{status}: {counts.get(status, 0)}", "orange" if status == "pending" else "muted")
        for status in ("pending", "approved", "rejected", "executed")
    )
    body = (
        f'<a class="back" href="/projects/{e(url)}">&larr; Back to project</a>'
        '<p class="kicker">Evidence &amp; activity</p>'
        f'<div class="proj-head"><h1>{e(project.name)}</h1>'
        f'{ui.pill(_workspace_stage(project, state), "purple")}</div>'
        f'<p class="muted">{e(_REPORT_NOTICE)}</p>'
        '<nav class="row" aria-label="Workspace sections" style="margin-top:20px">'
        '<a class="btn" href="#evidence">Evidence</a><a class="btn" href="#decisions">Decisions</a>'
        f'<a class="btn btn-primary" href="/projects/{e(url)}/report">Download project report (.md)</a></nav>'
        f'<div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))">{tiles}</div>'
        '<section class="block" id="demand"><div class="block-head"><h2>Demand target and results</h2></div>'
        f'{demand}<p class="small muted">Only recorded real demand counts; MOCKED data is not evidence.</p></section>'
        '<section class="block"><div class="block-head"><h2>Approval status</h2></div>'
        f'<div class="row">{approvals}</div>'
        '<p class="small muted" style="margin-top:12px">Executed means copy-ready export only, not live publishing.</p></section>'
        '<section class="block" id="evidence"><div class="block-head"><h2>Evidence</h2></div>'
        f'<p>Verified FACT: {facts} | ASSUMPTION: {assumptions}</p>'
        '<p class="small muted">FACT means a supporting quote matches a page fetched for this project, '
        'not independent proof.</p>'
        + (f'<ul style="padding:0">{claims}</ul>' if claims else '<p class="muted">No claims recorded.</p>')
        + '</section><section class="block" id="decisions"><div class="block-head"><h2>Activity and decisions</h2></div>'
        '<p class="small muted">Most recent first. Open an event to see its recorded decision and sources.</p>'
        + (f'<ul style="padding:0">{activity}</ul>' if activity else '<p class="muted">No project actions or decisions recorded.</p>')
        + '</section><div class="notice"><span class="icon orange">i</span><p>'
        'Build: STUBBED. Operate: recorded-data review; live analytics STUBBED. Delivery: MOCKED. No payments. '
        'Commercial setup: Working (price and policy drafts). No live publishing connectors.</p></div>'
    )
    return _page("Evidence & activity", body)


@app.get("/projects/{project_id}/report", response_class=PlainTextResponse)
def project_report(project_id: str) -> PlainTextResponse:
    records = _workspace_records(project_id)
    text = "# Project report\n\n" + _REPORT_NOTICE + "\n\n" + "\n\n".join(
        f"## {title}\n\n{body}" for title, body in _workspace_sections(records)
    ) + "\n"
    return PlainTextResponse(text, headers={
        "Content-Disposition": 'attachment; filename="project-report.md"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


def _read_output(name: str, project_id: str | None = None) -> str:
    """Read an output file. With a project id, read it from that project's own folder
    (see `Workflow.project_out_dir`); every run after the first has its own folder."""
    folder = get_workflow().existing_out_dir(project_id) if project_id else outputs_dir()
    if folder is None:
        return ""
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
    operate_done = str(state.get("operate_stage", "")).startswith("gate decided")
    passed = bool(state.get("validate_passed"))
    commercial = bool(state.get("commercial_passed"))
    step1_done = approved or awaiting in {"run_step2", "phase_gate"} or state.get("stage") == "drafting the interview script"
    items = [
        ("Brief", "done"),
        ("1 Discover: opportunities", "done" if step1_done else "now"),
        ("1 Discover: interview script", "done" if approved else ("now" if step1_done else "")),
        ("2 Validate: demand test", "done" if passed else ("now" if approved else "")),
        ("3 Build", "stub"),
        ("4 Commercial setup: price and policies", "done" if commercial else ("now" if passed else "")),
        ("5 Launch: drafts and approval", "done" if launch_done else ("now" if commercial else "")),
        ("6 Operate: learning review", "done" if operate_done else
         ("now" if state.get("launch_stage") == "gate decided: continue" else "")),
    ]
    if status == "stopped":
        items = [(name, "done" if css == "done" else ("stub" if css == "stub" else "")) for name, css in items]
    return ui.stepper(items)


def _running_kind(state: dict[str, Any]) -> str:
    if state.get("launch_stage") == "gate decided: continue":
        return "operate"
    if state.get("commercial_passed"):
        return "launch"
    if state.get("validate_passed"):
        return "commercial"
    if state.get("discover_approved"):
        return "validate"
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
        f'<p><a class="btn" href="/projects/{e(quote(project.id, safe=""))}/workspace">Evidence &amp; activity</a></p>',
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
        kind = _running_kind(state)
        stage = {"operate": state.get("operate_stage"), "launch": state.get("launch_stage"), "validate": state.get("validate_stage"),
                 "commercial": state.get("commercial_stage")}.get(kind, state.get("stage"))
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
        parts.append(_target_card(project_id, state))
        parts.append(ui.decision_panel(
            "Your decision: end of Discover", state,
            "Continue approves the sharpened idea and starts Validate with the demand target above. "
            "The interview script below is for you to use with real people.",
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
        latest = next(
            (name for name in ("phase6_gate_decision.md", "phase2_gate_decision.md", "phase1_gate_decision.md")
             if _own_output(project_id, name)),
            "phase1_gate_decision.md",
        )
        parts.append(_doc(project_id, latest))
    elif state.get("launch_stage") == "gate decided: continue":
        parts.append(_operate_section(wf, project, state))
    elif state.get("commercial_passed"):
        parts.append(_launch_section(wf, project, state))
    elif state.get("validate_passed"):
        parts.append(_commercial_section(wf, project, state))
    elif state.get("discover_approved"):
        parts.append(_validate_section(wf, project, state))
    return _page(project.name, "".join(parts) + footer)


def _research_fold(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    view = _opportunities(wf, project, state)
    if not view:
        return ""
    return f'<details class="fold" style="margin-top:20px"><summary>Research from step 1</summary><div class="fold-body">{view}</div></details>'


def _operate_section(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    project_id = project.id
    stage = state.get("operate_stage")
    parts = [
        '<section class="decision"><p class="kicker">Operate and learn</p>'
        '<h2>Learn from recorded evidence</h2>'
        '<p class="hint">Review the demand you recorded, sourced claims and draft approvals. '
        'Exports are not published posts or sales. Delivery is MOCKED; Build and live analytics '
        'remain STUBBED. This review cannot establish product usage, revenue or retention.</p>',
    ]
    if stage in (None, "ready", "failed", "drafting"):
        if stage == "drafting":
            parts.append(
                '<p>The previous review was interrupted. You can explicitly retry it below.</p>'
            )
        parts.append(
            '<p>The operator will identify gaps and propose measurable next experiments. '
            'It cannot execute them or change your project without your decision.</p>'
            f'<form method="post" action="/projects/{e(project_id)}/run/operate" style="margin-top:18px">'
            '<button class="btn btn-primary btn-lg" type="submit">Review what we learned</button>'
            '</form>'
        )
    elif stage == "awaiting decision":
        parts.append(
            '<h3>Your decision: learning review</h3>'
            f'<p>Recommendation: <strong>{e(str(state.get("operate_recommendation", "")).upper())}</strong>. '
            f'{e(state.get("operate_reason", ""))}</p>'
            '<p class="hint">Continue acknowledges this review only; it does not pass Build or publish anything. '
            'Repeat drafts a new review. Go back reopens the Launch review. Stop halts the project.</p>'
            + _choice_buttons(project_id, STEP_CHOICES, action="operate-gate", typed=False)
        )
    elif stage == "gate decided: continue":
        parts.append(
            '<p>Your learning review is complete. No experiments were run automatically, '
            'and the project has not been marked as a completed six-phase venture.</p>'
            '<a class="btn" href="/#new">Start a new brief</a>'
        )
    else:
        parts.append(ui.alert(
            "The learning review state is not recognized.",
            '<p>No step was started. Check the Evidence &amp; activity page before continuing.</p>',
        ))
    parts.append('</section>')
    parts.append(_doc(project_id, "phase6_step1_pivot_memo.md", "Learning memo"))
    return "".join(parts)


def _launch_section(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    project_id = project.id
    actions = wf.launch_actions(project_id)
    stub = ('<div class="notice" style="margin-bottom:16px"><span class="icon orange">i</span><p><strong>STUBBED:</strong> '
            "Build is designed but not built in this prototype, so launch drafts are "
            "based on the Discover option, the Validate result and your approved policies.</p></div>")
    if not actions:
        return (stub + '<section class="decision"><p class="kicker">Next step</p><h2>Price and policies approved</h2>'
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
    parts.append(_approval_cards(project_id, actions))
    if decided:
        choice = str(state.get("launch_stage", "")).split(":")[-1].strip()
        parts.append('<section class="decision" style="margin-top:20px"><p class="kicker">Done</p><h2>Launch review finished</h2>'
                     f'<p style="margin-top:12px">Your decision: {ui.pill(choice.upper(), "solid")}. Approved drafts are saved as '
                     "copy-ready text files in the <code>outputs</code> folder for you to post yourself.</p>"
                     '<p class="hint">Only Continue opens the recorded-data learning review. '
                     'Build and live analytics remain STUBBED.</p>'
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


def _commercial_section(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    project_id = project.id
    stage = str(state.get("commercial_stage") or "")
    stub = ('<div class="notice" style="margin-bottom:16px"><span class="icon orange">i</span><p><strong>STUBBED:</strong> '
            "Phase 3 Build is designed but not built in this prototype. Commercial setup works from the "
            "Discover option and your Validate result. Foundry has no payment code: you sell through your own "
            "payment provider.</p></div>")
    if stage.startswith("gate decided"):
        choice = stage.split(":")[-1].strip()
        return (stub + '<section class="decision"><p class="kicker">Done</p><h2>Commercial setup finished</h2>'
                f'<p style="margin-top:12px">Your decision: {ui.pill(choice.upper(), "solid")}.</p>'
                '<a class="btn btn-primary" href="/#new" style="margin-top:18px">Start a new brief</a></section>'
                + _doc(project_id, "phase4_gate_decision.md", "Gate decision"))
    actions = wf.commercial_actions(project_id)
    if not actions and stage != "awaiting approvals":
        error = state.get("error") if stage == "failed" else None
        return (stub + (ui.alert("The step failed.", f"<p>{e(error)}</p>") if error else "")
                + '<section class="decision"><p class="kicker">Next step</p><h2>Validate passed: set the price and policies</h2>'
                '<p class="hint">The pricing agent looks up what similar products charge, proposes two or three price '
                "options, and drafts your terms of service, privacy policy and refund policy. Each policy is checked by "
                "the guardrails and then waits for your approval. Nothing is published or charged by Foundry.</p>"
                f'<form method="post" action="/projects/{e(project_id)}/run/commercial" style="margin-top:18px">'
                '<button class="btn btn-primary btn-lg" type="submit">Draft price options and policies '
                '<span class="arrow">&rarr;</span></button></form></section>'
                + _doc(project_id, "phase2_gate_decision.md", "Validate gate decision", open_=False))
    ready, reason = wf.commercial_check(project_id)
    pending = sum(1 for a in actions if a.status.value == "pending")
    parts = [stub, _doc(project_id, "phase4_step1_pricing.md", "Price options"),
             '<div class="block-head" style="margin-top:20px"><div><p class="kicker">Approval screen</p>'
             "<h2>Policy drafts: your approval</h2></div></div>"
             '<p class="muted">Approving a policy is a <code>sign</code> action: it writes the exact text shown to a '
             "copy-ready file for you to publish yourself. These drafts are not legal advice.</p>"]
    for blocked in state.get("commercial_blocked") or []:
        parts.append('<div style="margin-top:14px">'
                     + ui.alert("One policy was blocked by the guardrails and was not queued.", f"<p>{e(blocked)}</p>") + "</div>")
    parts.append(_approval_cards(project_id, actions))
    if pending:
        parts.append(f'<div class="notice" style="margin-top:16px"><span class="icon purple">{pending}</span><p>'
                     f'<strong>{pending} polic{"ies" if pending != 1 else "y"} still waiting for you.</strong> '
                     '<span class="muted">Approve or reject each one to finish this step.</span></p></div>')
    else:
        rec = "continue" if ready else "repeat"
        parts.append('<section class="decision" style="margin-top:20px"><p class="kicker">Your decision</p>'
                     "<h2>Your decision: Commercial setup</h2>"
                     f'<p style="margin-top:12px">Foundry recommends {ui.pill(rec.upper(), "solid")} {e(reason)}</p>'
                     '<p class="hint">Continue is accepted only when all three policies are approved. Repeat drafts new policies.</p>'
                     f"{_choice_buttons(project_id, STEP_CHOICES, rec, action='commercial-gate', typed=False)}</section>")
    return "".join(parts)


def _approval_cards(project_id: str, actions: list[Any], *, preview_landing: bool = False) -> str:
    tone = {"pending": "solid-orange", "approved": "solid", "executed": "solid", "rejected": "muted"}
    names = {"pending": "WAITING FOR YOU", "approved": "APPROVED", "executed": "APPROVED AND EXPORTED", "rejected": "REJECTED"}
    out = []
    for a in actions:
        status = a.status.value
        body = f'<pre class="draft">{e(a.body)}</pre>'
        if preview_landing and a.action_type.value == "deploy":
            # The draft page shown as a page, in a sandbox with scripts off. The exact source
            # that would be approved is one click away.
            body = (f'<iframe title="Landing page preview" sandbox="" srcdoc="{e(a.body)}" '
                    'style="width:100%;height:520px;border:1px solid var(--line-2);border-radius:12px;background:#fff"></iframe>'
                    f'<details class="typed"><summary>Show the exact HTML that would be approved</summary>{body}</details>')
        card = (
            '<article class="card draft-card" style="margin-top:16px"><div class="head">'
            f"<h3>{e(a.title)}</h3>{ui.pill(names.get(status, status.upper()), tone.get(status, 'muted'))}</div>"
            f"{body}"
            f'<p class="fingerprint">Fingerprint of this exact text: <code>{e(a.content_hash[:16])}</code></p>'
        )
        if status == "approved":
            # Approved but the export did not finish (for example a step failed): let the
            # founder finish it. A fresh single-use token is issued for the same content.
            card += (
                '<p class="muted small" style="margin-top:12px">Approved, but the copy-ready file was not written yet.</p>'
                f'<form method="post" action="/projects/{e(project_id)}/actions/{e(a.id)}/approve">'
                f'<input type="hidden" name="seen_hash" value="{e(a.content_hash)}">'
                '<button class="btn btn-primary" type="submit">Finish the export</button></form>'
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
        out.append(card + "</article>")
    return "".join(out)


def _target_card(project_id: str, state: dict[str, Any]) -> str:
    target = state.get("validate_target")
    if target:
        return ('<div class="notice" style="margin-bottom:16px"><span class="icon purple">&#10003;</span><p>'
                f'<strong>Demand target fixed: {e(target["target"])} {e(target["what"])}.</strong> '
                '<span class="muted">It is recorded in the decision log and cannot be changed once set.</span></p></div>')
    return (
        '<section class="card" style="margin-bottom:16px"><p class="kicker">Before Validate</p>'
        "<h2>Fix your demand target</h2>"
        '<p class="hint">Decide now, before any test runs, how much real demand would convince you. Foundry records it '
        "and will not let it change later. Below half of it, Foundry will recommend stopping.</p>"
        f'<form method="post" action="/projects/{e(project_id)}/target" class="row" style="margin-top:14px;align-items:flex-end">'
        '<div><label class="small muted" for="t-n">Target</label><input id="t-n" type="text" name="target" inputmode="numeric" '
        'pattern="[0-9]+" required value="20" style="width:7rem"></div>'
        '<div style="flex:1;min-width:16rem"><label class="small muted" for="t-w">What counts as one</label>'
        '<input id="t-w" type="text" name="what" value="waitlist sign-ups from people in the target market"></div>'
        '<button class="btn btn-primary" type="submit">Fix the target</button></form></section>'
    )


def _validate_section(wf: Workflow, project: ProjectRecord, state: dict[str, Any]) -> str:
    project_id = project.id
    stage = state.get("validate_stage")
    target = state.get("validate_target") or {}
    target_line = f'{e(target.get("target", "?"))} {e(target.get("what", ""))}'
    if stage in (None, "ready"):
        return (
            '<section class="decision"><p class="kicker">Phase 2 · Validate</p><h2>Design the business and a demand test</h2>'
            f'<p class="hint">Your demand target is fixed: <strong>{target_line}</strong>. The strategist drafts a Lean Canvas '
            "(each box sourced or labelled ASSUMPTION), a plan for a landing-page test, the landing page and an invite post. "
            "The page and the post wait for your approval; Foundry publishes nothing.</p>"
            f'<form method="post" action="/projects/{e(project_id)}/run/validate" style="margin-top:18px">'
            '<button class="btn btn-primary btn-lg" type="submit">Draft the Lean Canvas and landing page '
            '<span class="arrow">&rarr;</span></button></form></section>'
            + _doc(project_id, "phase1_step2_interview_script.md", "Interview script", open_=False)
            + _research_fold(wf, project, state)
        )
    parts = ['<div class="block-head"><div><p class="kicker">Phase 2 · Validate</p><h2>Run the demand test</h2></div></div>'
             f'<p class="muted">Target, fixed before the test: <strong>{target_line}</strong>. '
             "Approve the landing page and the post, publish them yourself, then record what really happened.</p>"]
    for blocked in state.get("validate_blocked") or []:
        parts.append('<div style="margin-top:14px">'
                     + ui.alert("One draft was blocked by the guardrails and was not queued.", f"<p>{e(blocked)}</p>") + "</div>")
    parts.append(_approval_cards(project_id, wf.validate_actions(project_id), preview_landing=True))

    results = state.get("validate_results") or []
    recorded = "".join(f"<li>{e(r['count'])} {e(r['kind'])}{(': ' + e(r['note'])) if r['note'] else ''}</li>" for r in results)
    parts.append(
        '<section class="card" style="margin-top:20px"><p class="kicker">Real results only</p><h2>Record what happened</h2>'
        '<p class="hint">Count only real people in your target market: sign-ups, interviews where they described the problem, '
        "or pre-orders. Foundry never creates sign-ups, and test entries do not count.</p>"
        + (f'<ul style="margin:12px 0 0 18px">{recorded}</ul>' if recorded else "")
        + f'<form method="post" action="/projects/{e(project_id)}/validate-results" style="margin-top:14px">'
        '<div class="row" style="align-items:flex-end">'
        '<div><label class="small muted" for="r-n">How many</label><input id="r-n" type="text" name="count" inputmode="numeric" '
        'pattern="[0-9]+" required style="width:7rem"></div>'
        '<div><label class="small muted" for="r-k">Of what</label><input id="r-k" type="text" name="kind" value="waitlist sign-ups" style="width:14rem"></div>'
        '<div style="flex:1;min-width:14rem"><label class="small muted" for="r-note">Where from (optional)</label>'
        '<input id="r-note" type="text" name="note" placeholder="For example: shared in two design groups for 7 days"></div></div>'
        '<label class="small" style="display:flex;gap:8px;align-items:center;margin-top:12px">'
        '<input type="checkbox" name="confirmed" value="yes" required> These are real people, not estimates or test entries.</label>'
        '<button class="btn btn-primary" type="submit" style="margin-top:12px">Record results</button></form></section>'
    )

    if stage == "results_recorded":
        check = wf.validate_check(project_id)
        rec = {"advance": "continue"}.get(check.recommendation.value, check.recommendation.value)
        view = {"recommendation": rec, "recommendation_reason": check.recommendation_reason}
        allowed = STEP_CHOICES if check.ready_to_advance else tuple(c for c in STEP_CHOICES if c != "continue")
        hint = ("The gate is met. Continue opens Launch drafts." if check.ready_to_advance else
                f"The gate is not met ({e(check.metrics.get('recorded_demand_signals', 0))} of "
                f"{e(check.metrics.get('target', 0))}). Foundry will not continue past a failed evidence gate: "
                "repeat the test, go back to Discover, or stop.")
        parts.append('<div style="margin-top:20px">' + ui.decision_panel(
            "Your decision: Validate", view, hint,
            _choice_buttons(project_id, allowed, rec, action="validate-gate", typed=False), kicker="Gate · Validate",
        ) + "</div>")
    else:
        parts.append('<div class="notice" style="margin-top:16px"><span class="icon purple">i</span><p>'
                     "<strong>Waiting for your results.</strong> <span class=\"muted\">The Validate decision appears "
                     "once you record what happened, even if the answer is zero.</span></p></div>")
    parts.append('<div class="stack" style="margin-top:20px">'
                 + _doc(project_id, "phase2_step1_lean_canvas.md", "Lean Canvas and demand-test plan", open_=True)
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


@app.post("/projects/{project_id}/target")
def set_target(project_id: str, target: str = Form(...), what: str = Form("")):
    try:
        get_workflow().set_validate_target(project_id, int(target.strip() or 0), what)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The target must be a whole number.") from exc
    except (WorkflowError, OrchestratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/validate-results")
def validate_results(project_id: str, count: str = Form(...), kind: str = Form(""), note: str = Form(""),
                     confirmed: str = Form("")):
    try:
        get_workflow().record_validate_results(
            project_id, int(count.strip() or -1), kind, note, confirmed_real=confirmed == "yes"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Enter a whole number, 0 or more.") from exc
    except (WorkflowError, OrchestratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/validate-gate")
def validate_gate(project_id: str, choice: str = Form(...)):
    try:
        get_workflow().decide_validate(project_id, choice)
    except (WorkflowError, OrchestratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/commercial-gate")
def commercial_gate(project_id: str, choice: str = Form(...)):
    try:
        get_workflow().decide_commercial_gate(project_id, choice)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/launch-gate")
def launch_gate(project_id: str, choice: str = Form(...)):
    try:
        get_workflow().decide_launch_gate(project_id, choice)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/operate-gate")
def operate_gate(project_id: str, choice: str = Form(...)):
    try:
        get_workflow().decide_operate_review(project_id, choice)
    except (WorkflowError, OrchestratorError) as exc:
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
    if step == "operate":
        project = wf.orch.get_project(project_id)
        if (project is None or project.status.value != "active"
                or project_id in _running
                or state.get("launch_stage") != "gate decided: continue"
                or state.get("operate_stage") not in (None, "ready", "failed", "drafting")):
            raise HTTPException(status_code=409, detail="The learning review cannot be started now.")
        _start_step(project_id, "operate")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    if step == "commercial":
        if not state.get("validate_passed") or state.get("commercial_stage") not in (None, "ready", "failed"):
            raise HTTPException(status_code=409, detail="Commercial setup cannot be started now.")
        _start_step(project_id, "commercial")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    if step == "launch":
        if not state.get("commercial_passed") or wf.launch_actions(project_id):
            raise HTTPException(status_code=409, detail="Launch drafts cannot be started now.")
        _start_step(project_id, "launch")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    if step == "validate":
        if not state.get("discover_approved") or state.get("validate_stage") not in (None, "ready"):
            raise HTTPException(status_code=409, detail="Validate cannot be started now.")
        _start_step(project_id, "validate")
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
