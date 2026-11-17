"""Outbox and Founder Approval Queue with HMAC-SHA256 content-bound single-use tokens.

Security invariants:
1. Agents can only call `propose_action(...)`. Proposed actions pass through `PolicyEngine` first
   (blocking fake reviews, income guarantees, impersonation, card storage, or spend actions).
2. `spend` actions cannot even be approved/executed because Foundry never moves or spends money.
3. Founder approval (`approve_action`) signs an HMAC-SHA256 token over:
      `jti | action_id | action_type | content_hash`
4. Editing a proposed action (`update_action_content`) recomputes `content_hash`, resets status to
   `PENDING`, and revokes any previously issued token — invalidating prior approvals on any edit.
5. `execute_action` verifies:
   - token signature (constant-time `hmac.compare_digest`),
   - token belongs to `action_id`,
   - token `content_hash` matches current `action.content_hash`,
   - token has never been used or revoked (`single-use`),
   - PolicyEngine tool check passes with `has_valid_approval_token=True`.
6. The executor writes an auditable copy-ready text file to `outputs/`. It never posts to any
   external service; the founder posts the approved text themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from decision_log import DecisionLog
from guardrails import PolicyContext, PolicyEngine, PolicyViolationError


class ActionType(str, Enum):
    POST = "post"
    EMAIL = "email"
    LISTING = "listing"
    DEPLOY = "deploy"
    SUPPORT_REPLY = "support_reply"
    SIGN = "sign"  # accepting terms / publishing policies: always needs founder approval
    SPEND = "spend"  # Explicitly blocked by Policy Engine ("never hold or move money")


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


_PHASE_NUMBER = {
    "discover": 1,
    "validate": 2,
    "build": 3,
    "commercial": 4,
    "launch": 5,
    "operate": 6,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_content_hash(action_type: str, target: str, title: str, body: str) -> str:
    """Compute canonical SHA-256 content hash over everything the founder reviews."""
    canonical = json.dumps(
        {
            "action_type": action_type,
            "target": target,
            "title": title,
            "body": body,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProposedAction(BaseModel):
    id: str
    project_id: str
    phase: str
    step: int = 1
    action_type: ActionType
    target: str
    title: str
    body: str
    content_hash: str
    status: ActionStatus
    evidence_ids: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    created_at: str
    updated_at: str


class ApprovalToken(BaseModel):
    """Single-use signed token bound to an action ID and its exact SHA-256 content hash."""

    jti: str
    action_id: str
    content_hash: str
    issued_at: str
    signature: str

    def encode(self) -> str:
        return f"{self.jti}.{self.action_id}.{self.content_hash}.{self.signature}"

    @classmethod
    def decode(cls, raw_token: str, issued_at: str = "") -> "ApprovalToken":
        parts = raw_token.strip().split(".")
        if len(parts) != 4 or not all(parts):
            raise OutboxError("Malformed approval token.")
        jti, action_id, content_hash, signature = parts
        return cls(
            jti=jti,
            action_id=action_id,
            content_hash=content_hash,
            issued_at=issued_at,
            signature=signature,
        )


class ExecutionReceipt(BaseModel):
    action_id: str
    token_jti: str
    executor_mode: str = "export_file_and_copy_text"
    export_path: str
    copy_ready_text: str
    executed_at: str


class OutboxError(RuntimeError):
    """Raised when an Outbox operation violates approval, hash-binding, or single-use rules."""


class OutboxService:
    """SQLite-backed Outbox queue, token issuer/verifier, and export executor."""

    def __init__(
        self,
        db_path: str | Path = "data/foundry.sqlite3",
        *,
        export_dir: str | Path = "outputs",
        decision_log: DecisionLog | None = None,
        policy_engine: PolicyEngine | None = None,
        signing_secret: str | bytes | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.decision_log = decision_log
        self.policy_engine = policy_engine or PolicyEngine(decision_log=decision_log)

        env_secret = os.environ.get("FOUNDRY_APPROVAL_SECRET")
        raw_secret = signing_secret or env_secret or self._load_or_create_secret_file()
        self._secret: bytes = (
            raw_secret.encode("utf-8") if isinstance(raw_secret, str) else raw_secret
        )
        self._init_db()

    def _load_or_create_secret_file(self) -> str:
        """Persist the approval signing key next to the DB so approvals survive a restart.

        The key file lives in the git-ignored data directory with owner-only permissions.
        """
        key_path = self.db_path.parent / "approval_signing.key"
        if key_path.exists():
            existing = key_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        new_secret = secrets.token_hex(32)
        key_path.write_text(new_secret, encoding="utf-8")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return new_secret

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_actions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    step INTEGER NOT NULL DEFAULT 1,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_tokens (
                    jti TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    used_at TEXT,
                    revoked_at TEXT,
                    FOREIGN KEY (action_id) REFERENCES outbox_actions(id)
                )
                """
            )

    def _sign(self, jti: str, action_id: str, action_type: str, content_hash: str) -> str:
        msg = f"{jti}|{action_id}|{action_type}|{content_hash}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def propose_action(
        self,
        *,
        project_id: str = "default",
        phase: str = "launch",
        step: int = 1,
        action_type: ActionType | str,
        target: str,
        title: str,
        body: str,
        evidence_ids: list[str] | None = None,
    ) -> ProposedAction:
        """Create a proposed external action in the Outbox (never executes it)."""
        normalized_type = (
            ActionType(action_type) if isinstance(action_type, str) else action_type
        )
        if normalized_type == ActionType.SPEND:
            # Delegate to PolicyEngine so the block is uniformly logged
            self.policy_engine.enforce_text(
                f"pay for ads / spend money: {title} {body}",
                context=PolicyContext(
                    project_id=project_id,
                    phase=phase,
                    surface="tool_call",
                ),
            )
            raise OutboxError("Spend actions are prohibited in Foundry.")

        # Enforce safety rules on the draft title + body before entering the Outbox
        self.policy_engine.enforce_text(
            f"{title}\n{body}",
            context=PolicyContext(
                project_id=project_id,
                phase=phase,
                surface="agent_output",
            ),
        )

        now = _utc_now_iso()
        action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
        c_hash = compute_content_hash(normalized_type.value, target.strip(), title.strip(), body)
        ev_ids = list(evidence_ids or [])

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbox_actions (
                    id, project_id, phase, step, action_type, target, title, body,
                    content_hash, status, evidence_ids_json, rejection_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    action_id,
                    project_id,
                    phase.lower(),
                    int(step),
                    normalized_type.value,
                    target.strip(),
                    title.strip(),
                    body,
                    c_hash,
                    ActionStatus.PENDING.value,
                    json.dumps(ev_ids),
                    now,
                    now,
                ),
            )

        if self.decision_log is not None:
            self._log_decision(
                project_id=project_id,
                phase=phase,
                decision=f"Queued proposed {normalized_type.value} '{title.strip()}' ({action_id}) in Outbox",
                action=f"propose_{normalized_type.value}",
                actor="agent_proposal",
                outcome="proposed",
                evidence_ids=ev_ids,
                metadata={"action_id": action_id, "content_hash": c_hash, "target": target.strip()},
            )

        action = self.get_action(action_id)
        assert action is not None
        return action

    def update_action_content(
        self,
        action_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        target: str | None = None,
    ) -> ProposedAction:
        """Edit a proposed action. Any content change invalidates all existing approval tokens."""
        current = self.get_action(action_id)
        if current is None:
            raise OutboxError(f"Action not found: {action_id}")
        if current.status == ActionStatus.EXECUTED:
            raise OutboxError(f"Cannot edit already-executed action {action_id}.")

        new_title = title.strip() if title is not None else current.title
        new_body = body if body is not None else current.body
        new_target = target.strip() if target is not None else current.target

        self.policy_engine.enforce_text(
            f"{new_title}\n{new_body}",
            context=PolicyContext(
                project_id=current.project_id,
                phase=current.phase,
                surface="agent_output",
            ),
        )

        new_hash = compute_content_hash(
            current.action_type.value, new_target, new_title, new_body
        )
        now = _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox_actions
                SET target = ?, title = ?, body = ?, content_hash = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_target,
                    new_title,
                    new_body,
                    new_hash,
                    ActionStatus.PENDING.value,
                    now,
                    action_id,
                ),
            )
            conn.execute(
                """
                UPDATE approval_tokens
                SET revoked_at = ?
                WHERE action_id = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (now, action_id),
            )

        updated = self.get_action(action_id)
        assert updated is not None
        return updated

    def approve_action(self, action_id: str) -> ApprovalToken:
        """Record founder approval and mint a signed single-use token tied to the content hash."""
        action = self.get_action(action_id)
        if action is None:
            raise OutboxError(f"Action not found: {action_id}")
        if action.status == ActionStatus.EXECUTED:
            raise OutboxError(f"Action {action_id} was already executed.")
        if action.action_type == ActionType.SPEND:
            raise OutboxError("Spend actions can never be approved or executed.")

        now = _utc_now_iso()
        jti = f"TOK-{uuid.uuid4().hex[:12].upper()}"
        signature = self._sign(jti, action.id, action.action_type.value, action.content_hash)

        with self._connect() as conn:
            # Revoke any earlier unused tokens for this action so only the newest approval is active
            conn.execute(
                """
                UPDATE approval_tokens
                SET revoked_at = ?
                WHERE action_id = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (now, action.id),
            )
            conn.execute(
                """
                INSERT INTO approval_tokens (
                    jti, action_id, content_hash, signature, issued_at, used_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                (jti, action.id, action.content_hash, signature, now),
            )
            conn.execute(
                "UPDATE outbox_actions SET status = ?, rejection_reason = NULL, updated_at = ? WHERE id = ?",
                (ActionStatus.APPROVED.value, now, action.id),
            )

        if self.decision_log is not None:
            self._log_decision(
                project_id=action.project_id,
                phase=action.phase,
                decision=f"Founder approved Outbox action {action.id} ('{action.title}')",
                action="approve_action",
                actor="founder_approval",
                outcome="approved",
                evidence_ids=action.evidence_ids,
                metadata={
                    "action_id": action.id,
                    "token_jti": jti,
                    "content_hash": action.content_hash,
                },
            )

        return ApprovalToken(
            jti=jti,
            action_id=action.id,
            content_hash=action.content_hash,
            issued_at=now,
            signature=signature,
        )

    def reject_action(self, action_id: str, *, reason: str = "Rejected by founder") -> ProposedAction:
        """Record founder rejection and revoke any outstanding tokens for this action."""
        action = self.get_action(action_id)
        if action is None:
            raise OutboxError(f"Action not found: {action_id}")
        if action.status == ActionStatus.EXECUTED:
            raise OutboxError(f"Action {action_id} was already executed.")

        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE approval_tokens
                SET revoked_at = ?
                WHERE action_id = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (now, action.id),
            )
            conn.execute(
                "UPDATE outbox_actions SET status = ?, rejection_reason = ?, updated_at = ? WHERE id = ?",
                (ActionStatus.REJECTED.value, reason.strip(), now, action.id),
            )

        if self.decision_log is not None:
            self._log_decision(
                project_id=action.project_id,
                phase=action.phase,
                decision=f"Founder rejected Outbox action {action.id}: {reason.strip()}",
                action="reject_action",
                actor="founder_rejection",
                outcome="rejected",
                evidence_ids=action.evidence_ids,
                metadata={"action_id": action.id, "reason": reason.strip()},
            )

        updated = self.get_action(action_id)
        assert updated is not None
        return updated

    def execute_action(
        self, action_id: str, approval_token: ApprovalToken | str | None
    ) -> ExecutionReceipt:
        """Execute an Outbox action ONLY if given a valid, unrevoked, unused token matching content_hash."""
        action = self.get_action(action_id)
        if action is None:
            raise OutboxError(f"Action not found: {action_id}")

        if approval_token is None or (isinstance(approval_token, str) and not approval_token.strip()):
            # Trigger PolicyEngine block + DecisionLog entry
            decision = self.policy_engine.evaluate_tool_call(
                "execute_outbox_action",
                {"action_id": action_id},
                project_id=action.project_id,
                phase=action.phase,
                has_valid_approval_token=False,
            )
            raise PolicyViolationError(decision)

        parsed_tok = (
            ApprovalToken.decode(approval_token)
            if isinstance(approval_token, str)
            else approval_token
        )

        if parsed_tok.action_id != action.id:
            self._record_token_failure(action, "Approval token action_id mismatch.")
            raise OutboxError("Approval token does not belong to this action.")

        if parsed_tok.content_hash != action.content_hash:
            self._record_token_failure(
                action, "Content was modified after approval; token content_hash invalidated."
            )
            raise OutboxError("Approval token invalidated: action content changed since approval.")

        expected_sig = self._sign(
            parsed_tok.jti, action.id, action.action_type.value, action.content_hash
        )
        if not hmac.compare_digest(parsed_tok.signature, expected_sig):
            self._record_token_failure(action, "Invalid HMAC signature on approval token.")
            raise OutboxError("Invalid approval token signature.")

        with self._connect() as conn:
            tok_row = conn.execute(
                "SELECT * FROM approval_tokens WHERE jti = ?", (parsed_tok.jti,)
            ).fetchone()
            if tok_row is None:
                self._record_token_failure(action, "Unknown approval token JTI.")
                raise OutboxError("Unknown approval token.")
            if tok_row["revoked_at"] is not None:
                self._record_token_failure(action, "Approval token has been revoked.")
                raise OutboxError("Approval token has been revoked.")
            if tok_row["used_at"] is not None:
                self._record_token_failure(action, "Single-use approval token was already used.")
                raise OutboxError("Approval token has already been used (single-use enforcement).")
            if action.status != ActionStatus.APPROVED:
                self._record_token_failure(
                    action, f"Action is in status '{action.status.value}', expected 'approved'."
                )
                raise OutboxError(f"Action {action.id} is not in approved state.")

            # Run PolicyEngine tool-call verification with valid token flag
            pol = self.policy_engine.evaluate_tool_call(
                "execute_outbox_action",
                {"action_id": action.id, "title": action.title, "body": action.body},
                project_id=action.project_id,
                phase=action.phase,
                has_valid_approval_token=True,
            )
            if not pol.allowed:
                raise PolicyViolationError(pol)

            now = _utc_now_iso()
            conn.execute(
                "UPDATE approval_tokens SET used_at = ? WHERE jti = ?",
                (now, parsed_tok.jti),
            )
            conn.execute(
                "UPDATE outbox_actions SET status = ?, updated_at = ? WHERE id = ?",
                (ActionStatus.EXECUTED.value, now, action.id),
            )

        # Prototype executor: export copy-ready file bundle (honestly labeled)
        copy_ready = (
            f"=== FOUNDRY APPROVED EXPORT - NOT POSTED ANYWHERE; COPY-READY TEXT FOR THE FOUNDER ({action.action_type.value.upper()}) ===\n"
            f"Action ID: {action.id}\n"
            f"Target Channel: {action.target}\n"
            f"Title / Subject: {action.title}\n"
            f"Content SHA-256: {action.content_hash}\n"
            f"Approval Token JTI: {parsed_tok.jti}\n"
            f"Executed At: {now}\n"
            f"------------------------------------------------------------\n"
            f"{action.body}\n"
        )
        phase_no = _PHASE_NUMBER.get(action.phase, 0)
        export_file = self.export_dir / (
            f"phase{phase_no}_step{action.step}_approved_{action.action_type.value}"
            f"_{action.id.lower()}.txt"
        )
        export_file.write_text(copy_ready, encoding="utf-8")

        if self.decision_log is not None:
            self._log_decision(
                project_id=action.project_id,
                phase=action.phase,
                decision=f"Executed approved Outbox action {action.id} to export bundle {export_file.name}",
                action="export_approved_action",
                actor="system",
                outcome="recorded",
                evidence_ids=action.evidence_ids,
                metadata={
                    "action_id": action.id,
                    "token_jti": parsed_tok.jti,
                    "export_path": str(export_file),
                },
            )

        return ExecutionReceipt(
            action_id=action.id,
            token_jti=parsed_tok.jti,
            export_path=str(export_file),
            copy_ready_text=copy_ready,
            executed_at=now,
        )

    def get_action(self, action_id: str) -> ProposedAction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_action(row)

    def list_actions(
        self,
        *,
        project_id: str | None = None,
        status: ActionStatus | None = None,
    ) -> list[ProposedAction]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)

        query = "SELECT * FROM outbox_actions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_action(r) for r in rows]

    def _record_token_failure(self, action: ProposedAction, reason: str) -> None:
        if self.decision_log is not None:
            self._log_decision(
                project_id=action.project_id,
                phase=action.phase,
                decision=f"[OUTBOX-TOKEN-BLOCK] Blocked execution of {action.id}: {reason}",
                action="block_execution",
                actor="policy_engine",
                outcome="blocked",
                evidence_ids=action.evidence_ids,
                metadata={"action_id": action.id, "reason": reason},
            )

    def _log_decision(
        self,
        *,
        project_id: str,
        phase: str,
        action: str,
        decision: str,
        actor: str,
        outcome: str,
        evidence_ids: list[str],
        metadata: dict[str, Any],
    ) -> None:
        assert self.decision_log is not None
        valid_phases = {
            "system",
            "discover",
            "validate",
            "build",
            "commercial",
            "launch",
            "operate",
        }
        safe_phase = phase.lower() if phase.lower() in valid_phases else "system"
        self.decision_log.append(
            project_id=project_id,
            phase=safe_phase,
            action=action,
            decision=decision,
            actor=actor,
            outcome=outcome,
            evidence_ids=evidence_ids,
            metadata=metadata,
        )

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> ProposedAction:
        return ProposedAction(
            id=row["id"],
            project_id=row["project_id"],
            phase=row["phase"],
            step=int(row["step"]),
            action_type=ActionType(row["action_type"]),
            target=row["target"],
            title=row["title"],
            body=row["body"],
            content_hash=row["content_hash"],
            status=ActionStatus(row["status"]),
            evidence_ids=json.loads(row["evidence_ids_json"]),
            rejection_reason=row["rejection_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
