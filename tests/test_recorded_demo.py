"""Committed demo playback never calls models or mutates project state."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    (root / "run_index.json").write_text(json.dumps({"runs": [{
        "project_id": "MOCKED-demo-fixture",
        "folder": ".",
        "idea": "MOCKED <script>alert('idea')</script>",
        "started_at": "2026-11-17T12:00:00Z",
    }]}))
    (root / "phase1_gate_decision.md").write_text(
        "# MOCKED fixture gate\n\n**System recommendation: STOP.**\n"
        "<script>alert('artifact')</script>"
    )
    (root / "phase1_step1_opportunities.md").write_text(
        "# MOCKED fixture research\n\nA claim remains `ASSUMPTION`."
    )
    (root / "phase2_step2_approved_post_private.txt").write_text("PRIVATE-APPROVAL-TOKEN")
    monkeypatch.setattr(app_module, "ROOT", tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded playback must not initialize a workflow or query a model")

    monkeypatch.setattr(app_module, "get_workflow", forbidden)
    monkeypatch.setattr(app_module.LLMAdapter, "runtime_status", forbidden)
    return TestClient(app_module.app), root


def test_recorded_playback_is_escaped_read_only_and_excludes_approval_exports(archive):
    client, root = archive
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    response = client.get("/demo")
    assert response.status_code == 200
    assert "System recommendation: STOP." in response.text
    assert "not a completed demand experiment" in response.text
    assert "ASSUMPTION" in response.text
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text
    assert "PRIVATE-APPROVAL-TOKEN" not in response.text
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()} == before


def test_recorded_playback_refuses_paths_outside_the_archive(archive):
    client, root = archive
    (root / "run_index.json").write_text(json.dumps({"runs": [{"folder": ".."}]}))
    assert client.get("/demo").status_code == 503


def test_recorded_playback_reports_missing_artifacts_explicitly(archive):
    client, root = archive
    (root / "phase1_gate_decision.md").unlink()
    (root / "phase1_step1_opportunities.md").unlink()
    response = client.get("/demo")
    assert response.status_code == 503
    assert "no readable artifacts" in response.text
