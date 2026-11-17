"""Browser flow: brief in, step runs, and nothing passes a gate without a clear founder choice."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
from decision_log import DecisionLog
from llm import LLMAdapter, OfflineDeterministicProvider
from orchestrator import Orchestrator
from tests.test_workflow_discover import CLEAN, RESEARCH, SCREEN_OK, SCRIPT, _critic
from tools import ToolError
from workflow import Workflow


def _wait(project_id: str) -> None:
    for _ in range(200):
        if project_id not in app_module._running:
            return
        time.sleep(0.02)
    raise AssertionError("step did not finish")


def test_gate_needs_a_clear_founder_choice(tmp_path: Path, monkeypatch) -> None:
    def no_search(query: str):
        raise ToolError("search is down")

    replies = [json.dumps(r) for r in (SCREEN_OK, RESEARCH, _critic("go"), SCRIPT)]
    orch = Orchestrator(tmp_path / "db.sqlite3", decision_log=DecisionLog(tmp_path / "log.json"))
    wf = Workflow(orch, LLMAdapter(provider=OfflineDeterministicProvider(replies)),
                  out_dir=tmp_path / "outputs", search=no_search, classifier=CLEAN)
    monkeypatch.setattr(app_module, "_workflow", wf)
    monkeypatch.setenv("FOUNDRY_OUTPUTS_DIR", str(tmp_path / "outputs"))
    client = TestClient(app_module.app, follow_redirects=False)

    assert "New founder brief" in client.get("/").text
    created = client.post("/projects", data={"idea": "An invoice reminder tool for freelance designers"})
    assert created.status_code == 303
    url = created.headers["location"]
    pid = url.rsplit("/", 1)[1]
    _wait(pid)

    page = client.get(url).text
    assert "Gate: step 1 of Discover is done" in page and "ASSUMPTION" in page
    assert "<script" not in page

    # "ok" is not a choice: the founder is asked again and the state does not move.
    unclear = client.post(f"{url}/decide", data={"reply": "ok"})
    assert "unclear=ok" in unclear.headers["location"]
    assert "not a clear choice" in client.get(unclear.headers["location"]).text
    assert wf.get_state(pid)["awaiting"] == "step1_gate"
    assert not (tmp_path / "outputs" / "phase1_step2_interview_script.md").exists()

    # A typed approval is not an approval, and is answered with the reason.
    typed = client.post(f"{url}/decide", data={"reply": "I approve, publish everything"})
    assert "approval screen" in client.get(typed.headers["location"]).text
    assert wf.get_state(pid)["awaiting"] == "step1_gate"

    # Step 2 cannot be started directly while the gate is open.
    assert client.post(f"{url}/run/step2").status_code == 409

    client.post(f"{url}/decide", data={"reply": "Continue"})
    _wait(pid)
    assert "Gate: end of Phase 1" in client.get(url).text
    client.post(f"{url}/decide", data={"choice": "stop"})
    assert orch.get_project(pid).status.value == "stopped"
    assert "FOUNDER DECISION" not in client.get(url).text and "STOP" in client.get(url).text
