"""MOCKED workflow fixtures exercise learning review without inventing production traction."""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from gates import PhaseName, ProjectStatus
from guardrails import ClassifierVerdict, PolicyCategory
from tests.test_app_gates import _wait
from tests.test_workflow_discover import (
    BRIEF, LAUNCH, PRICING, RESEARCH, SCREEN_OK, SCRIPT, STRATEGY,
    _commercial_passed, _critic, _discover_approved, _workflow,
)
from workflow import WorkflowError


OPERATE_REPLY = {
    "working_hypothesis": "Designers may need a repeatable planning checklist.",
    "recommendation": "repeat",
    "rationale": "Test past preparation problems before committing to a product.",
    "confidence": "low",
    "traction_status": "not_established",
    "risks": ["Existing free checklists may already solve the problem."],
    "learning_limits": ["Founder records do not establish independently verified demand."],
    "experiments": [{
        "hypothesis": "Designers recently struggled with interview preparation.",
        "method": "After separate founder approval, interview five distinct target-market designers; "
                  "record consented notes and provenance, counting independently checked problems only.",
        "metric": "independently_verified_problem_interviews",
        "sample_size": 5,
        "success_threshold": 3,
        "duration_days": 7,
        "cost_usd": 0,
        "if_below_threshold": "pivot",
    }],
}


@pytest.fixture
def ready_review(tmp_path):
    wf, log = _workflow(tmp_path, [
        SCREEN_OK, RESEARCH, _critic("go"), SCRIPT, STRATEGY, PRICING, LAUNCH, OPERATE_REPLY,
    ])
    pid = _discover_approved(wf, target=2)
    wf.run_validate_step1(pid)
    wf.record_validate_results(pid, 2, "MOCKED test-fixture sign-ups", confirmed_real=True)
    wf.decide_validate(pid, "continue")
    _commercial_passed(wf, pid)
    wf.run_launch_drafts(pid)
    first, *rest = wf.launch_actions(pid)
    wf.approve_and_export(first.id, seen_hash=first.content_hash)
    for action in rest:
        wf.reject(action.id, "MOCKED test-fixture decision")
    wf.decide_launch_gate(pid, "continue")
    return wf, pid, log, tmp_path


def test_learning_review_needs_an_explicit_launch_decision(tmp_path):
    wf, _ = _workflow(tmp_path, [])
    pid = wf.start_project(BRIEF).id
    with pytest.raises(WorkflowError, match="continued Launch"):
        wf.run_operate_review(pid)
    assert not list((tmp_path / "outputs").glob("phase6*"))


def test_review_uses_recorded_metrics_without_approving_or_publishing(ready_review):
    wf, pid, log, root = ready_review
    actions_before = wf.orch.outbox.list_actions(project_id=pid)
    state = wf.run_operate_review(pid)
    assert state["operate_stage"] == "awaiting decision"
    assert state["operate_metrics"]["recorded_demand_count"] == 2
    assert state["operate_metrics"]["exported_drafts"] == 4
    assert state["operate_metrics"]["pending_drafts"] == 2
    assert state["operate_metrics"]["delivery_label"] == "MOCKED"
    assert wf.orch.outbox.list_actions(project_id=pid) == actions_before
    assert wf.orch.get_project(pid).current_phase == PhaseName.BUILD
    memo = (root / "outputs" / "phase6_step1_pivot_memo.md").read_text()
    assert "MOCKED" in memo and "STUBBED" in memo
    entry = next(e for e in log.read_all() if e.action == "review_operations")
    assert entry.phase == "operate" and entry.approved_by is None
    assert entry.metadata["recorded_metrics"] == state["operate_metrics"]


@pytest.mark.parametrize("choice", ["continue", "repeat", "go_back", "stop"])
def test_review_waits_for_founder_and_respects_the_four_choices(ready_review, choice):
    wf, pid, log, root = ready_review
    wf.run_operate_review(pid)
    with pytest.raises(WorkflowError, match="existing learning memo"):
        wf.run_operate_review(pid)
    state = wf.decide_operate_review(pid, choice)
    project = wf.orch.get_project(pid)
    if choice == "repeat":
        assert state["operate_stage"] == "ready"
    elif choice == "go_back":
        assert state["launch_stage"] == "awaiting approvals" and state["operate_stage"] is None
        with pytest.raises(WorkflowError, match="continued Launch"):
            wf.run_operate_review(pid)
    elif choice == "stop":
        assert project.status == ProjectStatus.STOPPED
    else:
        assert project.status == ProjectStatus.ACTIVE and project.current_phase == PhaseName.BUILD
    assert f"**Founder decision:** {choice.upper()}" in (
        root / "outputs" / "phase6_gate_decision.md"
    ).read_text()
    assert any(e.action == "operate_gate" and e.approved_by == "founder" for e in log.read_all())
    assert log.verify().valid
    with pytest.raises(WorkflowError, match="no active learning review"):
        wf.decide_operate_review(pid, "continue")


def test_unsafe_operator_output_is_never_exported(ready_review):
    wf, pid, _, root = ready_review
    wf.engine.classifier = lambda text, context: ClassifierVerdict(
        reason="MOCKED unsafe output verdict", safe=False, category=PolicyCategory.INCOME_GUARANTEE,
    )
    with pytest.raises(WorkflowError, match="operator output refused"):
        wf.run_operate_review(pid)
    assert not (root / "outputs" / "phase6_step1_pivot_memo.md").exists()
    assert PhaseName.OPERATE.value not in wf.orch.get_project(pid).phase_artifacts


def test_browser_review_waits_and_acknowledgment_does_not_finish_build(ready_review, monkeypatch):
    wf, pid, _, _ = ready_review
    monkeypatch.setattr(app_module, "_workflow", wf)
    client = TestClient(app_module.app, follow_redirects=False)
    url = f"/projects/{pid}"
    assert "Review what we learned" in client.get(url).text
    assert client.post(f"{url}/operate-gate", data={"choice": "continue"}).status_code == 409
    assert client.post(f"{url}/run/operate").status_code == 303
    _wait(pid)
    page = client.get(url).text
    assert "Your decision: learning review" in page and "MOCKED" in page
    assert client.post(f"{url}/run/operate").status_code == 409
    assert client.post(f"{url}/operate-gate", data={"choice": "ok"}).status_code == 409
    assert client.post(f"{url}/operate-gate", data={"choice": "continue"}).status_code == 303
    assert "learning review is complete" in client.get(url).text
    assert wf.orch.get_project(pid).current_phase == PhaseName.BUILD


def test_interrupted_review_is_retryable_not_presented_as_complete(ready_review, monkeypatch):
    wf, pid, _, _ = ready_review
    wf._set_state(pid, operate_stage="drafting")
    monkeypatch.setattr(app_module, "_workflow", wf)
    client = TestClient(app_module.app, follow_redirects=False)
    url = f"/projects/{pid}"
    page = client.get(url).text
    assert "previous review was interrupted" in page
    assert "learning review is complete" not in page
    assert client.post(f"{url}/run/operate").status_code == 303
    _wait(pid)
    assert wf.get_state(pid)["operate_stage"] == "awaiting decision"
