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
from guardrails import PolicyContext, PolicyViolationError
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
        f"<td>{e(c['expect'])}</td><td>{'yes' if c['blocked'] else 'no'}</td>"
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
html{{color-scheme:light;background:#f6f7f9}}body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:1rem 1.5rem;color:#1a1a1a;background:#fff;border-radius:.6rem}}
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
<h2>Guardrail evals, rules only (run now): {rt['passed']}/{rt['total']} passed</h2>
<p>These run without the model: the keyword backstop, secrets detector and tool allowlist. Rewordings and
other languages are checked by the model classifier; see <code>evals/results.md</code> for the full run.</p>
<table><tr><th>Case</th><th>What it tries</th><th>Text</th><th>Expected</th><th>Blocked</th><th>Rule</th><th>Result</th></tr>{rt_rows}</table>
<h2>What works and what is mocked</h2>
<table><tr><th>Part</th><th>Status</th><th>Where</th></tr>{caps}</table>
</body></html>"""


# ---------------------------------------------------------------------- founder pages

PAGE_CSS = """
:root{color-scheme:light;--ink:#16181d;--muted:#5b6270;--line:#e2e5ea;--bg:#f6f7f9;--card:#fff;--accent:#2f4fd8;--accent-soft:#eef1fe;--ok:#177a3a;--warn:#b45309;--bad:#c0392b}
*{box-sizing:border-box}html{background:var(--bg)}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:900px;margin:0 auto;padding:0 1rem 4rem;color:var(--ink);background:var(--bg);line-height:1.5}
a{color:var(--accent)}h1{font-size:1.7rem;margin:.2rem 0 .4rem;line-height:1.25}h2{font-size:1.15rem;margin:1.2rem 0 .5rem}
header.top{display:flex;align-items:center;gap:1rem;padding:1rem 0;margin-bottom:.5rem}
header.top .brand{font-weight:700;font-size:1.15rem;color:var(--ink);text-decoration:none;margin-right:auto}
header.top a.nav{color:var(--muted);text-decoration:none;font-size:.92rem}
.btn,button{font:inherit;font-weight:600;padding:.55rem 1rem;border-radius:.5rem;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}
.btn.primary,button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.stop{background:#fff;color:var(--bad);border-color:#e6b8b2}
.card{background:var(--card);border:1px solid var(--line);border-radius:.8rem;padding:1.1rem 1.3rem;margin:1rem 0;box-shadow:0 1px 2px rgba(20,24,40,.04)}
.card.gate{border:2px solid var(--accent);background:var(--accent-soft)}.card.warn{border:1px solid #e6b8b2;background:#fdf1ef}
.card.stub{border:1px dashed #d9a441;background:#fff9ec;color:#5c4307;font-size:.93rem}
.lead{color:var(--muted);font-size:1.02rem;margin:.2rem 0 1rem}.muted{color:var(--muted);font-size:.9rem}
label{display:block;font-weight:600;margin-top:.9rem;font-size:.95rem}label span{font-weight:400;color:var(--muted)}
input[type=text],textarea{width:100%;padding:.6rem .7rem;font:inherit;color:var(--ink);background:#fff;border:1px solid #cfd4dc;border-radius:.5rem;margin-top:.25rem}
input[type=text]:focus,textarea:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 1rem}@media(max-width:640px){.grid2{grid-template-columns:1fr}}
.pill{display:inline-block;padding:.12rem .6rem;border-radius:1rem;font-size:.8rem;font-weight:700;color:#fff;vertical-align:middle}
.pill.green{background:var(--ok)}.pill.red{background:var(--bad)}.pill.amber{background:var(--warn)}.pill.grey{background:#7a8190}.pill.blue{background:var(--accent)}
.steps{display:flex;flex-wrap:wrap;gap:.4rem;margin:.8rem 0 1.2rem;padding:0;list-style:none}
.steps li{font-size:.82rem;padding:.3rem .65rem;border-radius:1rem;border:1px solid var(--line);background:var(--card);color:var(--muted)}
.steps li.done{background:#e8f5ec;border-color:#bfe1c9;color:var(--ok)}.steps li.now{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
.steps li.stub{border-style:dashed;opacity:.75}
.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem;margin:.8rem 0}
.choices button{text-align:left;padding:.7rem .9rem;height:100%;width:100%}.choices button small{display:block;font-weight:400;color:var(--muted);margin-top:.15rem}
.choices button.primary small{color:#dfe5ff}.choices .rec{outline:3px solid #f2c037;outline-offset:1px}
.doc{font-size:.95rem}.doc h1{font-size:1.25rem}.doc h2{font-size:1.05rem;border-top:1px solid var(--line);padding-top:.8rem}.doc h3{font-size:.98rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:.88rem}td,th{border:1px solid var(--line);padding:.4rem .5rem;text-align:left;vertical-align:top}th{background:#f1f3f6}
pre.draft{white-space:pre-wrap;background:#f6f7f9;border:1px solid var(--line);border-radius:.5rem;padding:.8rem;font:inherit;font-size:.95rem}
code{font-size:.85em;background:#f1f3f6;padding:.05rem .3rem;border-radius:.25rem;word-break:break-all}
details summary{cursor:pointer;font-weight:600}.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.6rem}
.spinner{width:1.1rem;height:1.1rem;border:3px solid #cfd6f7;border-top-color:var(--accent);border-radius:50%;display:inline-block;animation:spin 1s linear infinite;vertical-align:middle;margin-right:.5rem}
@keyframes spin{to{transform:rotate(360deg)}}
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
        '<header class="top"><a class="brand" href="/">Foundry</a>'
        '<a class="nav" href="/#projects">Projects</a><a class="nav" href="/status">Status and what is mocked</a>'
        '<a class="btn primary" href="/#new">New brief</a></header>'
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


def _brief_form(brief: dict[str, Any] | None = None, action: str = "/projects", submit: str = "Start") -> str:
    b = brief or {}
    e = html.escape

    def field(name: str, label: str, hint: str, rows: int = 1, required: bool = False) -> str:
        value = e(str(b.get(name, "")))
        req = " required minlength=\"10\"" if required else ""
        note = "" if required else " <span>(optional)</span>"
        control = (
            f'<textarea name="{name}" rows="{rows}" placeholder="{e(hint)}"{req}>{value}</textarea>'
            if rows > 1 else f'<input type="text" name="{name}" value="{value}" placeholder="{e(hint)}">'
        )
        return f"<div><label>{e(label)}{note}</label>{control}</div>"

    urls = e("\n".join(b.get("source_urls", [])))
    return (
        f'<form method="post" action="{action}">'
        + field("idea", "Your idea", "What digital product do you want to build, and for whom? One or two sentences is enough.", 3, True)
        + '<div class="grid2">'
        + field("target_market", "Who is it for", "For example: freelance designers in Egypt")
        + field("skills", "Your skills", "For example: Python, design, teaching")
        + field("budget", "Budget", "For example: $100")
        + field("time_available", "Time you have", "For example: 10 hours a week for 2 months")
        + "</div>"
        + field("values", "Your values", "What you will and will not do, for example: honest marketing")
        + '<details style="margin-top:.9rem"><summary>Add pages you want the agents to read</summary>'
        '<label>Source URLs <span>(one per line, up to 5)</span></label>'
        f'<textarea name="source_urls" rows="2" placeholder="https://...">{urls}</textarea></details>'
        f'<div class="row"><button class="primary" type="submit">{e(submit)}</button>'
        '<span class="muted">Takes about 2 minutes. You decide at every step.</span></div></form>'
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
        model = '<div class="card warn"><strong>MOCKED:</strong> the offline test provider is active, not a real model.</div>'
    elif not llm["ready"]:
        model = ('<div class="card warn">The local model is not ready yet (still downloading, or Ollama is not '
                 'reachable). Steps will fail until it is. See <a href="/status">Status</a>.</div>')
    else:
        model = (f'<p class="muted"><span class="pill green">Ready</span> Runs on your machine with the open model '
                 f'<code>{e(str(llm["configured_model"]))}</code>. No account, no cost.</p>')
    rows = "".join(
        f'<tr><td><a href="/projects/{e(p.id)}">{e(p.name)}</a></td>'
        f"<td>{e(_stage_label(wf.get_state(p.id), p.status.value))}</td></tr>"
        for p in wf.orch.list_projects()
    )
    projects = (
        f'<h2 id="projects">Your projects</h2><div class="card"><table><tr><th>Idea</th><th>Where it stands</th></tr>{rows}</table></div>'
        if rows else ""
    )
    body = (
        "<h1>From a rough idea to an honest go or no-go</h1>"
        '<p class="lead">Describe your idea. A team of AI agents researches it with real sources, argues against it, '
        "and tells you plainly when the evidence says stop. Nothing moves forward, and nothing is published, without you.</p>"
        f'{model}<div class="card" id="new"><h2 style="margin-top:0">Tell us about your idea</h2>{_brief_form(submit="Research my idea")}</div>'
        '<div class="card stub"><strong>What this prototype does.</strong> Working: Discover (research, critic review, '
        "interview script) and Launch drafts with your approval. <strong>STUBBED</strong> (designed, not built): Validate, "
        "Build, Commercial setup, Operate. Delivery is <strong>MOCKED</strong>. There are no payments.</div>"
        f"{projects}"
    )
    return _page("Foundry", body)


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
            f'<div class="box warn"><h2>This brief was not saved</h2><p>{html.escape(str(exc))}</p></div>'
            '<p><a href="/">Back</a></p>')), status_code=400)
    _start_step(project.id, "step1")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


def _read_output(name: str) -> str:
    path = outputs_dir() / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


CHOICE_TEXT = {
    "continue": ("Continue", "Accept this and move to the next step"),
    "repeat": ("Repeat", "Run this step again for a better result"),
    "go_back": ("Go back", "Return to the previous step or change the brief"),
    "stop": ("Stop", "End this project here"),
}


def _choice_buttons(project_id: str, allowed: tuple[str, ...], recommended: str = "",
                    action: str = "decide", typed: bool = True) -> str:
    buttons = ""
    for choice in STEP_CHOICES:
        if choice not in allowed:
            continue
        label, hint = CHOICE_TEXT[choice]
        css = {"continue": "primary", "stop": "stop"}.get(choice, "")
        rec = " rec" if choice == recommended else ""
        tag = " (recommended)" if choice == recommended else ""
        buttons += (f'<button class="{css}{rec}" name="choice" value="{choice}" type="submit">'
                    f"{label}{tag}<small>{hint}</small></button>")
    out = f'<form method="post" action="/projects/{project_id}/{action}"><div class="choices">{buttons}</div></form>'
    if typed:
        out += (f'<details><summary class="muted">Or type your choice</summary>'
                f'<form class="row" method="post" action="/projects/{project_id}/decide">'
                '<input type="text" name="reply" style="max-width:22rem" placeholder="continue, repeat, go back or stop">'
                '<button type="submit">Send</button></form></details>')
    return out


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
    cells = "".join(
        f'<li class="{css}">{html.escape(name)}{" · STUBBED" if css == "stub" else ""}</li>' for name, css in items
    )
    return f'<ol class="steps">{cells}</ol>'


def _recommendation_card(state: dict[str, Any]) -> str:
    rec = str(state.get("recommendation", ""))
    colour = {"continue": "green", "repeat": "amber", "stop": "red"}.get(rec, "grey")
    return (f'<p><span class="pill {colour}">Foundry recommends: {html.escape(rec.upper())}</span></p>'
            f'<p>{html.escape(str(state.get("recommendation_reason", "")))}</p>')


def _doc(name: str, title: str = "", open_: bool = True) -> str:
    text = _read_output(name)
    if not text:
        return ""
    if title:
        return (f'<details class="card doc"{" open" if open_ else ""}><summary>{html.escape(title)}</summary>'
                f"{render_markdown(text)}</details>")
    return f'<div class="card doc">{render_markdown(text)}</div>'


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
    brief = project.brief or {}
    brief_rows = "".join(
        f"<tr><th>{label}</th><td>{e(str(brief.get(key) or 'not given'))}</td></tr>"
        for key, label in (("idea", "Idea"), ("target_market", "For"), ("skills", "Skills"),
                           ("budget", "Budget"), ("time_available", "Time"), ("values", "Values"))
    )
    parts = [
        f'<p class="muted">Your project</p><h1>{e(project.name)}</h1>',
        _stepper(state, project.status.value),
        f'<details class="card"><summary>Your brief <span class="muted">(what you entered)</span></summary><table>{brief_rows}</table></details>',
    ]
    footer = f'<p class="muted">Cost so far: $0.00 ({tokens} tokens on the local model). Project id {e(project.id)}.</p>'
    running = project_id in _running
    if state.get("error"):
        parts.append(f'<div class="card warn"><strong>The step failed.</strong> {e(state["error"])}<br>'
                     "Nothing was made up to cover for it. You can run the step again.</div>")
    if unclear and state.get("reply_refusal"):
        parts.append(f'<div class="card warn">{e(state["reply_refusal"])}</div>')
    if unclear:
        parts.append(f'<div class="card warn">Your reply "{e(unclear)}" is not a clear choice, so nothing was done. '
                     "Please choose one of: continue, repeat, go back, stop.</div>")
    if running:
        stage = state.get("launch_stage") if state.get("discover_approved") else state.get("stage")
        parts.append(f'<div class="card"><p><span class="spinner"></span><strong>The agents are working: {e(str(stage or "starting"))}</strong></p>'
                     '<p class="muted">This usually takes one to two minutes. The page updates by itself; you can leave it open.</p></div>')
        return _page(project.name, "".join(parts) + footer, refresh=3)

    rec = str(state.get("recommendation", ""))
    if awaiting == "step1_gate":
        parts.append('<div class="card gate"><h2 style="margin-top:0">Your decision: the opportunities</h2>'
                     f"{_recommendation_card(state)}"
                     '<p class="muted">Read the research below. Foundry recommends; you decide. Nothing continues until you choose.</p>'
                     f"{_choice_buttons(project_id, STEP_CHOICES, rec)}</div>")
        parts.append(_doc("phase1_step1_opportunities.md"))
    elif awaiting == "phase_gate":
        parts.append('<div class="card gate"><h2 style="margin-top:0">Your decision: end of Discover</h2>'
                     f"{_recommendation_card(state)}"
                     '<p class="muted">Continue approves the sharpened idea. The interview script below is for you to use with real people.</p>'
                     f"{_choice_buttons(project_id, STEP_CHOICES, rec)}</div>")
        parts.append(_doc("phase1_step2_interview_script.md"))
        parts.append(_doc("phase1_step1_opportunities.md", "Research from step 1", open_=False))
    elif awaiting == "excluded_gate":
        parts.append('<div class="card warn"><h2 style="margin-top:0">This product type is not supported</h2>'
                     f"{_recommendation_card(state)}{_choice_buttons(project_id, ('stop',), typed=False)}"
                     '<p><a href="/#new">Start a different brief</a></p></div>')
    elif awaiting in {"run_step1", "run_step2"}:
        step = "step1" if awaiting == "run_step1" else "step2"
        label = "Run the research" if step == "step1" else "Draft the interview script"
        parts.append(f'<div class="card"><form method="post" action="/projects/{project_id}/run/{step}">'
                     f'<button class="primary" type="submit">{label}</button></form></div>')
    elif awaiting == "revise_brief":
        if state.get("refusal"):
            parts.append(f'<div class="card warn"><h2 style="margin-top:0">Foundry will not do this</h2><p>{e(state["refusal"])}</p>'
                         '<p class="muted">The refusal is recorded in the decision log. Change the brief below, or stop.</p>'
                         f"{_choice_buttons(project_id, ('stop',), typed=False)}</div>")
        parts.append('<div class="card"><h2 style="margin-top:0">Revise your brief</h2>'
                     + _brief_form(project.brief, f"/projects/{project_id}/brief", "Research again") + "</div>")
    elif state.get("rejected"):
        parts.append('<div class="card warn"><h2 style="margin-top:0">This idea was rejected</h2>'
                     f'<p>{e(str(state.get("recommendation_reason", "")))}</p>'
                     '<p class="muted">No research was run. The rejection is recorded in the decision log.</p>'
                     '<a class="btn primary" href="/#new">Try a digital product idea</a></div>')
    elif project.status.value == "stopped":
        parts.append('<div class="card"><strong>You stopped this project.</strong> '
                     '<a href="/#new">Start a new brief</a></div>')
        parts.append(_doc("phase1_gate_decision.md"))
    elif state.get("discover_approved"):
        parts.append(_launch_section(wf, project_id, state))
    return _page(project.name, "".join(parts) + footer)


def _launch_section(wf: Workflow, project_id: str, state: dict[str, Any]) -> str:
    e = html.escape
    actions = wf.launch_actions(project_id)
    stub = ('<div class="card stub"><strong>STUBBED:</strong> Validate, Build and Commercial setup are designed but not '
            "built in this prototype, so launch drafts are based on the Discover result only.</div>")
    if not actions:
        return (stub + '<div class="card gate"><h2 style="margin-top:0">Discover is approved</h2>'
                "<p>Next, the launch writer drafts a store page and launch posts. Each draft is checked by the "
                "guardrails and then waits for your approval. Nothing is published by Foundry.</p>"
                f'<form method="post" action="/projects/{project_id}/run/launch">'
                '<button class="primary" type="submit">Draft store page and launch posts</button></form></div>'
                + _doc("phase1_step2_interview_script.md", "Interview script", open_=False)
                + _doc("phase1_step1_opportunities.md", "Research from step 1", open_=False))
    colour = {"pending": "amber", "approved": "green", "executed": "green", "rejected": "red"}
    names = {"pending": "WAITING FOR YOU", "approved": "APPROVED", "executed": "APPROVED AND EXPORTED", "rejected": "REJECTED"}
    pending = sum(1 for a in actions if a.status.value == "pending")
    decided = str(state.get("launch_stage", "")).startswith("gate decided")
    parts = [stub, '<h2>Launch drafts: your approval</h2>'
             '<p class="muted">Nothing here has been published. Approving writes the exact text shown to a copy-ready '
             "file for you to post yourself. Foundry cannot post, send or spend.</p>"]
    for blocked in state.get("launch_blocked") or []:
        parts.append(f'<div class="card warn"><strong>One draft was blocked by the guardrails and was not queued.</strong><br>{e(blocked)}</div>')
    for a in actions:
        status = a.status.value
        parts.append(
            f'<div class="card"><p><span class="pill {colour.get(status, "grey")}">{names.get(status, status.upper())}</span> '
            f"<strong>{e(a.title)}</strong></p>"
            f'<pre class="draft">{e(a.body)}</pre>'
            f'<p class="muted">Fingerprint of this exact text: <code>{e(a.content_hash[:16])}</code></p>'
        )
        if status == "pending":
            parts.append(
                '<div class="row">'
                f'<form method="post" action="/projects/{project_id}/actions/{e(a.id)}/approve">'
                f'<input type="hidden" name="seen_hash" value="{e(a.content_hash)}">'
                '<button class="primary" type="submit">Approve this exact text</button></form>'
                f'<form class="row" style="margin-top:0" method="post" action="/projects/{project_id}/actions/{e(a.id)}/reject">'
                '<input type="text" name="reason" style="width:14rem;margin-top:0" placeholder="Reason (optional)">'
                '<button class="stop" type="submit">Reject</button></form></div>'
            )
        elif a.rejection_reason:
            parts.append(f'<p class="muted">Reason: {e(a.rejection_reason)}</p>')
        parts.append("</div>")
    if decided:
        choice = str(state.get("launch_stage", "")).split(":")[-1].strip()
        parts.append(f'<div class="card gate"><h2 style="margin-top:0">Launch review finished</h2>'
                     f'<p>Your decision: <span class="pill blue">{e(choice.upper())}</span>. Approved drafts are saved as '
                     "copy-ready text files in the <code>outputs</code> folder for you to post yourself.</p>"
                     '<p class="muted">This is as far as the prototype goes: Operate is STUBBED.</p>'
                     '<a class="btn primary" href="/#new">Start a new brief</a></div>')
    elif pending:
        parts.append(f'<div class="card"><strong>{pending} draft{"s" if pending != 1 else ""} still waiting for you.</strong> '
                     '<span class="muted">Approve or reject each one to finish this step.</span></div>')
    else:
        parts.append('<div class="card gate"><h2 style="margin-top:0">Your decision: Launch</h2>'
                     '<p class="muted">Every draft is reviewed. Continue is accepted only if at least one was approved.</p>'
                     f"{_choice_buttons(project_id, STEP_CHOICES, action='launch-gate', typed=False)}</div>")
    parts.append(_doc("payment_test_log.md", "Delivery test log (MOCKED, no payment took place)", open_=False))
    parts.append(_doc("phase1_step2_interview_script.md", "Interview script", open_=False))
    parts.append(_doc("phase1_step1_opportunities.md", "Research from step 1", open_=False))
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
