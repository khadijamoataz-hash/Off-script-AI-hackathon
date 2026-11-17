"""Project evidence and on-demand reports are isolated, escaped, and read-only."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app as app_module
from decision_log import DecisionLog
from llm import LLMAdapter, OfflineDeterministicProvider
from orchestrator import Orchestrator
from tests.test_workflow_discover import CLEAN
from workflow import Workflow


@pytest.fixture
def workspace(monkeypatch):
    root = Path(f".test-project-workspace-{uuid4().hex}")
    root.mkdir()
    monkeypatch.setenv("FOUNDRY_APPROVAL_SECRET", "workspace-test-signing-secret")
    try:
        wf = Workflow(
            Orchestrator(root / "db.sqlite3", decision_log=DecisionLog(root / "log.json")),
            LLMAdapter(provider=OfflineDeterministicProvider()),
            out_dir=root / "outputs", classifier=CLEAN,
        )
        monkeypatch.setattr(app_module, "_workflow", wf)
        monkeypatch.setattr(app_module, "DECISION_LOG", root / "log.json")
        monkeypatch.setenv("FOUNDRY_DB_PATH", str(root / "db.sqlite3"))
        first = wf.orch.create_project(
            project_id="project-one", name='First <script>alert("name")</script>',
            idea_summary="A digital planner",
        )
        second = wf.orch.create_project(
            project_id="project-one-extra", name="Other private project", idea_summary="A digital guide",
        )
        url = 'https://example.org/research?x="quoted"&y=1'
        quote = "Researchers found five recurring planning problems <img src=x onerror=alert(1)>"
        wf.ledger.record_fetch(project_id=first.id, url=url, text=quote)
        wf.ledger.add_claim(
            "Verified planning observation <script>alert('claim')</script>", project_id=first.id,
            source_url=url, quote=quote,
        )
        wf.ledger.add_claim("Demand is still an assumption <b>not proven</b>", project_id=first.id)
        wf.ledger.add_claim("OTHER-PRIVATE-CLAIM", project_id=second.id)
        wf.log.append(
            project_id=first.id, action="review_evidence", phase="discover", outcome="repeat",
            decision="Review <svg onload=alert('decision')>", sources=[url],
            metadata={"signature": "SECRET-SIGNATURE", "token": "SECRET-TOKEN"},
        )
        wf.log.append(
            project_id=second.id, action="OTHER-PRIVATE-ACTION", decision="OTHER-PRIVATE-DECISION",
        )
        wf._set_state(
            first.id, awaiting=None, stage="research complete", discover_approved=True,
            validate_stage="results_recorded",
            validate_target={"target": 20, "what": "real planner sign-ups <em>only</em>"},
            validate_results=[{"count": 0, "kind": "sign-ups", "note": "<script>result</script>"}],
        )
        tokens = []
        for status in ("pending", "approved", "rejected", "executed"):
            action = wf.orch.outbox.propose_action(
                project_id=first.id, action_type="post", target="draft",
                title=f"Planner draft {status}", body=f"PRIVATE-DRAFT-BODY-{status}",
            )
            if status in ("approved", "executed"):
                token = wf.orch.outbox.approve_action(action.id)
                tokens.extend((token.encode(), token.signature, token.jti))
                if status == "executed":
                    wf.orch.outbox.execute_action(action.id, token)
            elif status == "rejected":
                wf.orch.outbox.reject_action(action.id)
        wf.orch.outbox.propose_action(
            project_id=second.id, action_type="post", target="draft",
            title="Other private draft", body="OTHER-PRIVATE-DRAFT",
        )
        yield TestClient(app_module.app), wf, root, tokens
    finally:
        shutil.rmtree(root)


def _snapshot(root: Path):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def test_workspace_isolates_exact_project_and_shows_persisted_evidence(workspace):
    client, _, _, _ = workspace
    response = client.get("/projects/project-one/workspace")
    assert response.status_code == 200
    text = response.text
    assert "Evidence &amp; activity" in text
    assert "Verified FACT: 1 | ASSUMPTION: 1" in text
    assert "Verified planning observation" in text and "Demand is still an assumption" in text
    assert "Supporting quote: Researchers found five recurring planning problems" in text
    assert "https://example.org/research?x=&quot;quoted&quot;&amp;y=1" in text
    assert "review_evidence" in text and "Outcome: repeat" in text
    assert "Fixed demand target: 20" in text and "Recorded demand signals: 0" in text
    for status in ("pending", "approved", "rejected", "executed"):
        assert f"{status}: 1" in text
    assert "Demand results recorded; waiting for your decision" in text
    assert "Waiting for your decision on the opportunities" not in text
    assert "Build: STUBBED" in text and "Operate: recorded-data review" in text and "Delivery: MOCKED" in text
    assert "OTHER-PRIVATE" not in text and "Other private" not in text


def test_workspace_escapes_all_user_and_model_fields(workspace):
    client, _, _, _ = workspace
    text = client.get("/projects/project-one/workspace").text
    for injected in ("<script>", "<img src=x", "<svg onload=", "<b>not proven", "<em>only"):
        assert injected not in text
    for escaped in ("&lt;script&gt;", "&lt;img ", "&lt;svg ", "&lt;b&gt;not proven", "&lt;em&gt;only"):
        assert escaped in text
    assert 'href="/projects/project-one/report"' in text
    assert "Download project report" in text
    assert '<blockquote class="quote">' in text and "<article" in text
    assert 'href="https://example.org/research?x=&quot;quoted&quot;&amp;y=1"' in text
    assert 'rel="noopener noreferrer"' in text
    assert 'href="#evidence"' in text and 'href="#decisions"' in text
    assert "<pre" not in text
    assert 'href="/projects/project-one/workspace"' in client.get("/projects/project-one").text


def test_report_is_plain_text_attachment_without_tokens_or_private_records(workspace):
    client, _, _, tokens = workspace
    response = client.get("/projects/project-one/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == 'attachment; filename="project-report.md"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    text = response.text
    assert "# Project report" in text and "DRAFT" in text and "internal research only" in text
    assert "not founder approval or publishing" in text
    assert "Verified FACT: 1 | ASSUMPTION: 1" in text
    assert "Supporting quote:" in text and 'https://example.org/research?x="quoted"&y=1' in text
    assert "Outcome: repeat" in text and "Approved by: founder" in text
    workspace_text = client.get("/projects/project-one/workspace").text
    for private in [*tokens, "SECRET-SIGNATURE", "SECRET-TOKEN", "workspace-test-signing-secret",
                    "OTHER-PRIVATE", "Other private", "PRIVATE-DRAFT-BODY", "content_hash"]:
        assert private not in text
        assert private not in workspace_text


@pytest.mark.parametrize("cold", [False, True])
def test_workspace_and_report_gets_are_inert_and_live(workspace, monkeypatch, cold):
    client, wf, root, _ = workspace

    def forbidden(*args, **kwargs):
        pytest.fail("A read-only workspace GET invoked a write-capable or external operation")

    if cold:
        monkeypatch.setattr(app_module, "_workflow", None)
    monkeypatch.setattr(app_module, "get_workflow", forbidden)
    monkeypatch.setattr(DecisionLog, "append", forbidden)
    monkeypatch.setattr(LLMAdapter, "generate_structured", forbidden)
    monkeypatch.setattr(LLMAdapter, "runtime_status", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(wf.orch.outbox, "approve_action", forbidden)
    monkeypatch.setattr(wf.orch.outbox, "execute_action", forbidden)
    monkeypatch.setattr(wf.orch.outbox, "reject_action", forbidden)
    monkeypatch.setattr(wf, "search", forbidden)
    monkeypatch.setattr(wf, "fetch", forbidden)
    monkeypatch.setattr(wf, "_set_state", forbidden)
    before = _snapshot(root)
    for route in ("workspace", "report"):
        assert client.get(f"/projects/project-one/{route}").status_code == 200
    assert _snapshot(root) == before
    # No cached report: a later persisted claim is immediately reflected.
    wf.ledger.add_claim("A newly recorded assumption", project_id="project-one")
    before = _snapshot(root)
    response = client.get("/projects/project-one/report")
    assert "A newly recorded assumption" in response.text
    assert "Verified FACT: 1 | ASSUMPTION: 2" in response.text
    assert _snapshot(root) == before


def test_unknown_projects_return_404_without_creating_files(workspace, monkeypatch):
    client, _, root, _ = workspace
    before = _snapshot(root)
    for route in ("workspace", "report"):
        assert client.get(f"/projects/project-on/{route}").status_code == 404
    assert _snapshot(root) == before
    monkeypatch.setattr(app_module, "_workflow", None)
    monkeypatch.setenv("FOUNDRY_DB_PATH", str(root / "missing" / "db.sqlite3"))
    assert client.get("/projects/unknown/report").status_code == 404
    assert not (root / "missing").exists()


def test_unrecorded_results_and_invalidated_quotes_are_not_presented_as_evidence(workspace):
    client, wf, _, _ = workspace
    wf._set_state("project-one", validate_target=None, validate_results=[])
    wf.ledger.record_fetch(
        project_id="project-one-extra", url='https://example.org/research?x="quoted"&y=1',
        text="Researchers found five recurring planning problems <img src=x onerror=alert(1)>",
    )
    wf.ledger.record_fetch(
        project_id="project-one", url='https://example.org/research?x="quoted"&y=1',
        text="The current persisted page no longer supports the old quote.",
    )
    text = client.get("/projects/project-one/report").text
    assert "Fixed demand target: not set." in text
    assert "No demand test results recorded." in text
    assert "Verified FACT: 0 | ASSUMPTION: 2" in text
    assert "Verified planning observation" in text


def test_homepage_prototype_notice_matches_working_commercial_status(workspace, monkeypatch):
    client, _, _, _ = workspace
    monkeypatch.setattr(LLMAdapter, "runtime_status", lambda self: {
        "is_mock": True, "ready": True, "configured_model": "offline-test",
    })
    text = client.get("/").text
    assert "Commercial setup (price and policy drafts)" in text
    assert "(designed, not built): Build and live operating integrations." in text
    assert "(designed, not built): Build, Commercial setup" not in text
    assert "Local model ready:" not in text
