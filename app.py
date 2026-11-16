"""Foundry status page (read-only).

What this page really does, live, on every load:
- reports whether the local Ollama model is reachable and downloaded,
- verifies the hash chain of the repo decision log and the runtime decision log,
- runs the red-team suite against the Policy Engine and shows the pass/fail table,
- shows CAPABILITIES.md.

STUBBED (not built yet): creating projects, running phase agents, approving actions in the browser.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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
    ("This status page", "Working", "app.py"),
    ("Phase agents (all six phases)", "STUBBED", "agents/"),
    ("Approving actions in the browser", "STUBBED", "not built"),
    ("Delivery to buyers", "STUBBED", "will be MOCKED; not built"),
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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
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
<h1>Foundry status</h1>
<p class="stub"><strong>STUBBED:</strong> this page is read-only. Creating projects, running phase agents
and approving actions in the browser are <strong>not built yet</strong>. Everything shown below is computed live.</p>
<h2>Local model</h2>
<p>{llm_line}</p>
<p>Provider <code>{e(str(llm['active_provider']))}</code>, model <code>{e(str(llm['configured_model']))}</code>,
Ollama at <code>{e(str(llm['ollama_url']))}</code></p>
<h2>Decision log (required keys and hash chain verified now)</h2>
<table><tr><th>Log</th><th>Status</th><th>Entries</th><th>Head hash</th><th>Path</th></tr>{log_rows}</table>
<h2>Guardrail evals (run now): {rt['passed']}/{rt['total']} passed</h2>
<table><tr><th>Case</th><th>Challenge</th><th>Prompt</th><th>Blocked</th><th>Logged</th><th>Rule</th><th>Result</th></tr>{rt_rows}</table>
<h2>What works and what is mocked</h2>
<table><tr><th>Part</th><th>Status</th><th>Where</th></tr>{caps}</table>
</body></html>"""
