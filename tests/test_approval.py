"""Stage 2 Outbox, Founder Approval Queue, and Content-Hash Single-Use Token tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_log import DecisionLog
from approval import ActionStatus, ActionType, ApprovalToken, OutboxError, OutboxService
from guardrails import PolicyCategory, PolicyViolationError


def test_outbox_blocks_unapproved_execution_and_logs_violation(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    outbox = OutboxService(
        tmp_path / "foundry.sqlite3",
        export_dir=tmp_path / "outputs",
        decision_log=log,
        signing_secret="test-secret-key",
    )

    action = outbox.propose_action(
        project_id="proj-1",
        phase="launch",
        action_type=ActionType.POST,
        target="reddit:r/Notion",
        title="AUC Semester Planner Template (Beta)",
        body="Built a lightweight Notion tracker for engineering finals [CLM-ABCD1234]. Feedback welcome!",
        evidence_ids=["CLM-ABCD1234"],
    )
    assert action.status == ActionStatus.PENDING

    # Attempting to execute without a founder approval token MUST fail and log a policy block
    with pytest.raises(PolicyViolationError) as exc_info:
        outbox.execute_action(action.id, approval_token=None)

    assert exc_info.value.decision.category == PolicyCategory.UNAPPROVED_EXTERNAL_ACTION
    assert log.verify().valid is True
    assert any(e.outcome == "blocked" for e in log.read_all())


def test_outbox_approve_execute_and_single_use_replay_protection(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    outbox = OutboxService(
        tmp_path / "foundry.sqlite3",
        export_dir=tmp_path / "outputs",
        decision_log=log,
        signing_secret="test-secret-key",
    )

    action = outbox.propose_action(
        project_id="proj-1",
        phase="launch",
        action_type=ActionType.EMAIL,
        target="waitlist_export",
        title="Early beta invite: Student Notion Pack",
        body="Hi! Thanks for opting into our interview list. Here is the preview link.",
    )

    token = outbox.approve_action(action.id)
    encoded_token = token.encode()

    receipt = outbox.execute_action(action.id, encoded_token)
    assert Path(receipt.export_path).exists()
    assert "Early beta invite: Student Notion Pack" in receipt.copy_ready_text

    executed_action = outbox.get_action(action.id)
    assert executed_action is not None
    assert executed_action.status == ActionStatus.EXECUTED

    # Replaying the exact same signed token a second time MUST fail (single-use)
    with pytest.raises(OutboxError, match="already been used"):
        outbox.execute_action(action.id, encoded_token)

    assert log.verify().valid is True


def test_any_edit_after_approval_invalidates_signed_token(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    outbox = OutboxService(
        tmp_path / "foundry.sqlite3",
        export_dir=tmp_path / "outputs",
        decision_log=log,
        signing_secret="test-secret-key",
    )

    action = outbox.propose_action(
        project_id="proj-1",
        phase="launch",
        action_type=ActionType.LISTING,
        target="gumroad_copy_draft",
        title="Notion Exam Planner",
        body="Original founder-reviewed product description.",
    )
    token = outbox.approve_action(action.id)

    # Agent or user modifies the body by even one word after approval
    edited = outbox.update_action_content(
        action.id,
        body="Original founder-reviewed product description. Plus an unapproved edit.",
    )
    assert edited.status == ActionStatus.PENDING
    assert edited.content_hash != token.content_hash

    # Old token MUST be rejected because content_hash no longer matches
    with pytest.raises(OutboxError, match="content changed since approval"):
        outbox.execute_action(action.id, token)

    # Founder re-approving the new content mints a fresh valid token
    fresh_token = outbox.approve_action(action.id)
    receipt = outbox.execute_action(action.id, fresh_token)
    assert Path(receipt.export_path).exists()
    assert log.verify().valid is True


def test_tampered_signature_and_rejected_actions_are_blocked(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    outbox = OutboxService(
        tmp_path / "foundry.sqlite3",
        export_dir=tmp_path / "outputs",
        decision_log=log,
        signing_secret="test-secret-key",
    )

    a1 = outbox.propose_action(
        project_id="proj-1",
        phase="launch",
        action_type=ActionType.POST,
        target="linkedin",
        title="Build in public update #1",
        body="Sharing what we learned from 12 student discovery interviews.",
    )
    tok = outbox.approve_action(a1.id)
    forged = ApprovalToken(
        jti=tok.jti,
        action_id=tok.action_id,
        content_hash=tok.content_hash,
        issued_at=tok.issued_at,
        signature="0" * 64,
    )
    with pytest.raises(OutboxError, match="Invalid approval token signature"):
        outbox.execute_action(a1.id, forged)

    # Founder subsequently rejects the draft -> even the genuine token is now revoked
    rejected = outbox.reject_action(a1.id, reason="Wait until Phase 2 interviews finish")
    assert rejected.status == ActionStatus.REJECTED
    with pytest.raises(OutboxError, match="revoked"):
        outbox.execute_action(a1.id, tok)

    # Proposing a SPEND action or unsafe content into Outbox is blocked at creation time
    with pytest.raises(PolicyViolationError):
        outbox.propose_action(
            project_id="proj-1",
            phase="launch",
            action_type=ActionType.SPEND,
            target="meta_ads",
            title="Run $500 campaign",
            body="Spend $500 on ads",
        )

    assert log.verify().valid is True
