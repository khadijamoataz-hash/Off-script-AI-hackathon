"""Finite State Machine over Foundry's 6 phases with verifiable Gates and SQLite Project Store.

Phases (in canonical order):
1. DISCOVER   — Gate: founder approves the idea brief (`founder_approval`).
2. VALIDATE   — Gate: pre-set `evidence_target` (sourced FACT claims + recorded demand signals)
                is reached AND founder approves (`evidence_target`).
3. BUILD      — Gate: sandboxed product tests pass (`build_tests_passed == True`) AND founder
                approves (`tests_pass`).
4. COMMERCIAL — Gate: founder approves pricing options & policy drafts (`founder_approval`).
5. LAUNCH     — Gate: Outbox items reviewed (no unreviewed items blocking, at least one draft
                created & reviewed) AND founder approves (`founder_approval`).
6. OPERATE    — Gate: founder reviews metrics/support summary and decides `advance` (complete
                cycle), `repeat`, `pivot_back`, or `stop` (`founder_approval`).

Gate outcomes at every phase:
- `advance`: verifies entry/exit gate criteria, moves to next phase (or marks `COMPLETED` after `operate`).
- `repeat`: increments iteration counter on current phase to refine hypotheses/tests.
- `pivot_back`: moves backward to an earlier phase (requires `target_phase` earlier than current),
                recording rationale and evidence IDs in `DecisionLog`.
- `stop`: halts the project cleanly as `STOPPED` (evidence-driven kill decision).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from decision_log import DecisionLog
from approval import OutboxService
from evidence import EvidenceLedger
from gates import (
    PHASE_DEFINITIONS,
    PHASE_ORDER,
    GateCheckResult,
    GateOutcome,
    GateType,
    PhaseDefinition,
    PhaseName,
    ProjectStatus,
    evaluate_gate,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectRecord(BaseModel):
    id: str
    name: str
    idea_summary: str
    target_customer: str = ""
    current_phase: PhaseName = PhaseName.DISCOVER
    status: ProjectStatus = ProjectStatus.ACTIVE
    phase_iterations: dict[str, int] = Field(
        default_factory=lambda: {p.value: 1 for p in PHASE_ORDER}
    )
    validate_evidence_target: int = Field(default=3, ge=1)
    recorded_demand_signals: int = Field(default=0, ge=0)
    build_tests_passed: bool = False
    phase_artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class OrchestratorError(RuntimeError):
    """Raised on invalid state transitions, unmet gate criteria, or inactive projects."""


class Orchestrator:
    """SQLite-backed 6-phase state machine coordinating gates, Evidence Ledger, Outbox, and Decision Log."""

    def __init__(
        self,
        db_path: str | Path = "data/foundry.sqlite3",
        *,
        evidence_ledger: EvidenceLedger | None = None,
        decision_log: DecisionLog | None = None,
        outbox: OutboxService | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.decision_log = decision_log or DecisionLog()
        self.evidence_ledger = evidence_ledger or EvidenceLedger(self.db_path)
        self.outbox = outbox or OutboxService(
            self.db_path,
            decision_log=self.decision_log,
        )
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    idea_summary TEXT NOT NULL,
                    target_customer TEXT NOT NULL,
                    current_phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase_iterations_json TEXT NOT NULL,
                    validate_evidence_target INTEGER NOT NULL,
                    recorded_demand_signals INTEGER NOT NULL,
                    build_tests_passed INTEGER NOT NULL,
                    phase_artifacts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_project(
        self,
        *,
        name: str,
        idea_summary: str,
        target_customer: str = "First-time founders / students",
        validate_evidence_target: int = 3,
        project_id: str | None = None,
    ) -> ProjectRecord:
        if validate_evidence_target < 1:
            raise OrchestratorError("validate_evidence_target must be >= 1.")

        pid = project_id or f"PRJ-{uuid.uuid4().hex[:8].upper()}"
        now = _utc_now_iso()
        iterations = {p.value: 1 for p in PHASE_ORDER}

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    id, name, idea_summary, target_customer, current_phase, status,
                    phase_iterations_json, validate_evidence_target, recorded_demand_signals,
                    build_tests_passed, phase_artifacts_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '{}', ?, ?)
                """,
                (
                    pid,
                    name.strip(),
                    idea_summary.strip(),
                    target_customer.strip(),
                    PhaseName.DISCOVER.value,
                    ProjectStatus.ACTIVE.value,
                    json.dumps(iterations),
                    validate_evidence_target,
                    now,
                    now,
                ),
            )

        self._log(
            project_id=pid,
            phase=PhaseName.DISCOVER.value,
            decision=(
                f"Created project '{name.strip()}' ({pid}) in Phase 1 (Discover) "
                f"with pre-set Validate evidence target = {validate_evidence_target}."
            ),
            action="create_project",
            actor="system",
            outcome="recorded",
            metadata={"validate_evidence_target": validate_evidence_target},
        )
        proj = self.get_project(pid)
        assert proj is not None
        return proj

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def list_projects(self) -> list[ProjectRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [self._row_to_project(r) for r in rows]

    def set_validate_evidence_target(self, project_id: str, target: int) -> ProjectRecord:
        """Set the validation evidence target. Allowed only before Validate starts."""
        proj = self._require_project(project_id)
        if target < 1:
            raise OrchestratorError("Evidence target must be at least 1.")
        if proj.current_phase != PhaseName.DISCOVER:
            raise OrchestratorError(
                "The Validate evidence target is fixed once Validate starts; it can only be set "
                "during Discover."
            )
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET validate_evidence_target = ?, updated_at = ? WHERE id = ?",
                (target, now, project_id),
            )
        self._log(
            project_id=project_id,
            phase=proj.current_phase.value,
            decision=f"Set Phase 2 (Validate) evidence target to {target}",
            action="set_evidence_target",
            actor="founder_approval",
            outcome="recorded",
            metadata={"validate_evidence_target": target},
        )
        return self._require_project(project_id)

    def save_phase_artifact(
        self,
        project_id: str,
        phase: PhaseName | str,
        artifact: dict[str, Any],
        *,
        evidence_ids: list[str] | None = None,
        agent: str = "agent",
        model: str | None = None,
        cost: float = 0,
    ) -> ProjectRecord:
        """Store structured phase agent output and verify referenced Evidence Ledger claim IDs."""
        proj = self._require_project(project_id)
        norm_phase = PhaseName(phase) if isinstance(phase, str) else phase

        ev_ids = list(evidence_ids or artifact.get("evidence_ids", []))
        if ev_ids:
            valid_ids, missing = self.evidence_ledger.validate_claim_ids(
                ev_ids, project_id=project_id
            )
            if not valid_ids:
                raise OrchestratorError(
                    f"Phase '{norm_phase.value}' artifact references unknown claim IDs: {missing}"
                )

        artifacts = dict(proj.phase_artifacts)
        artifacts[norm_phase.value] = artifact
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET phase_artifacts_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(artifacts), now, project_id),
            )

        self._log(
            project_id=project_id,
            phase=norm_phase.value,
            decision=f"Saved structured artifact for phase '{norm_phase.value}'",
            action="save_phase_artifact",
            agent=agent,
            model=model,
            cost=cost,
            actor="agent_proposal",
            outcome="proposed",
            evidence_ids=ev_ids,
        )
        return self._require_project(project_id)

    def record_demand_signals(
        self,
        project_id: str,
        signals_delta: int,
        *,
        note: str = "Recorded founder-verified demand signals",
        evidence_ids: list[str] | None = None,
    ) -> ProjectRecord:
        """Record real interview/survey/waitlist demand signals verified by the founder in Phase 2."""
        proj = self._require_project(project_id)
        if signals_delta < 0:
            raise OrchestratorError("signals_delta must be non-negative.")
        new_total = proj.recorded_demand_signals + signals_delta
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET recorded_demand_signals = ?, updated_at = ? WHERE id = ?",
                (new_total, now, project_id),
            )
        self._log(
            project_id=project_id,
            phase=PhaseName.VALIDATE.value,
            decision=f"{note} (+{signals_delta}, total={new_total})",
            action="record_demand_signals",
            actor="founder_approval",
            outcome="recorded",
            evidence_ids=list(evidence_ids or []),
            metadata={"recorded_demand_signals": new_total},
        )
        return self._require_project(project_id)

    def record_build_test_result(
        self,
        project_id: str,
        *,
        passed: bool,
        summary: str,
        evidence_ids: list[str] | None = None,
    ) -> ProjectRecord:
        """Record sandboxed test suite result for the Phase 3 Build gate."""
        self._require_project(project_id)
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET build_tests_passed = ?, updated_at = ? WHERE id = ?",
                (1 if passed else 0, now, project_id),
            )
        self._log(
            project_id=project_id,
            phase=PhaseName.BUILD.value,
            decision=f"Sandbox Build test run {'PASSED' if passed else 'FAILED'}: {summary}",
            action="build_tests",
            actor="gate_check",
            outcome="recorded",
            evidence_ids=list(evidence_ids or []),
            metadata={"build_tests_passed": passed, "summary": summary},
        )
        return self._require_project(project_id)

    def check_gate(self, project_id: str) -> GateCheckResult:
        """Evaluate the current phase's gate (see `gates.py`)."""
        return evaluate_gate(self._require_project(project_id), self.evidence_ledger, self.outbox)

    def decide_gate(
        self,
        project_id: str,
        outcome: GateOutcome | str,
        *,
        founder_approved: bool = False,
        rationale: str = "",
        target_phase: PhaseName | str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> ProjectRecord:
        """Apply a founder gate decision (`advance`, `repeat`, `pivot_back`, or `stop`)."""
        proj = self._require_project(project_id)
        if proj.status != ProjectStatus.ACTIVE:
            raise OrchestratorError(
                f"Project {project_id} is '{proj.status.value}' and cannot transition."
            )

        norm_outcome = GateOutcome(outcome) if isinstance(outcome, str) else outcome
        current = proj.current_phase
        ev_ids = list(evidence_ids or [])
        now = _utc_now_iso()

        if norm_outcome == GateOutcome.ADVANCE:
            if not founder_approved:
                raise OrchestratorError(
                    f"Cannot advance past '{current.value}' without explicit founder approval (`founder_approved=True`)."
                )
            gate_check = self.check_gate(project_id)
            if not gate_check.ready_to_advance:
                joined = "; ".join(gate_check.blockers)
                self._log(
                    project_id=project_id,
                    phase=current.value,
                    decision=f"Gate advance blocked on '{current.value}': {joined}",
                    action=f"{current.value}_gate",
                    agent="gate",
                    actor="gate_check",
                    outcome="blocked",
                    evidence_ids=ev_ids,
                    metadata={
                        "blockers": gate_check.blockers,
                        "metrics": gate_check.metrics,
                        "system_recommendation": gate_check.recommendation.value,
                        "recommendation_reason": gate_check.recommendation_reason,
                    },
                )
                raise OrchestratorError(f"Gate check failed for '{current.value}': {joined}")

            curr_idx = PHASE_DEFINITIONS[current].index
            if curr_idx + 1 < len(PHASE_ORDER):
                next_phase = PHASE_ORDER[curr_idx + 1]
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE projects SET current_phase = ?, updated_at = ? WHERE id = ?",
                        (next_phase.value, now, project_id),
                    )
                self._log(
                    project_id=project_id,
                    phase=current.value,
                    decision=(
                        f"Gate PASSED on '{current.value}' -> advancing to '{next_phase.value}'. "
                        f"{rationale}".strip()
                    ),
                    action=f"{current.value}_gate",
                    agent="gate",
                    actor="founder_approval",
                    outcome=GateOutcome.ADVANCE.value,
                    evidence_ids=ev_ids,
                    metadata={"next_phase": next_phase.value, "metrics": gate_check.metrics},
                )
            else:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                        (ProjectStatus.COMPLETED.value, now, project_id),
                    )
                self._log(
                    project_id=project_id,
                    phase=current.value,
                    decision=f"Completed Phase 6 ('{current.value}'). {rationale}".strip(),
                    action=f"{current.value}_gate",
                    agent="gate",
                    actor="founder_approval",
                    outcome=GateOutcome.ADVANCE.value,
                    evidence_ids=ev_ids,
                    metadata={"project_status": ProjectStatus.COMPLETED.value},
                )
            return self._require_project(project_id)

        if norm_outcome == GateOutcome.REPEAT:
            iterations = dict(proj.phase_iterations)
            iterations[current.value] = iterations.get(current.value, 1) + 1
            reset_tests = 0 if current == PhaseName.BUILD else int(proj.build_tests_passed)
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET phase_iterations_json = ?, build_tests_passed = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(iterations), reset_tests, now, project_id),
                )
            self._log(
                project_id=project_id,
                phase=current.value,
                decision=(
                    f"Founder chose REPEAT on '{current.value}' "
                    f"(iteration {iterations[current.value]}). {rationale}".strip()
                ),
                action=f"{current.value}_gate",
                agent="gate",
                actor="founder_approval",
                outcome=GateOutcome.REPEAT.value,
                evidence_ids=ev_ids,
                metadata={"iteration": iterations[current.value]},
            )
            return self._require_project(project_id)

        if norm_outcome == GateOutcome.PIVOT_BACK:
            if target_phase is None:
                raise OrchestratorError("pivot_back requires an explicit earlier `target_phase`.")
            dest = PhaseName(target_phase) if isinstance(target_phase, str) else target_phase
            if PHASE_DEFINITIONS[dest].index >= PHASE_DEFINITIONS[current].index:
                raise OrchestratorError(
                    f"pivot_back target '{dest.value}' must be an earlier phase than '{current.value}'."
                )
            iterations = dict(proj.phase_iterations)
            iterations[dest.value] = iterations.get(dest.value, 1) + 1
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET current_phase = ?, phase_iterations_json = ?, build_tests_passed = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        dest.value,
                        json.dumps(iterations),
                        0 if PHASE_DEFINITIONS[dest].index <= PHASE_DEFINITIONS[PhaseName.BUILD].index else int(proj.build_tests_passed),
                        now,
                        project_id,
                    ),
                )
            self._log(
                project_id=project_id,
                phase=current.value,
                decision=(
                    f"Founder chose PIVOT BACK from '{current.value}' -> '{dest.value}'. "
                    f"{rationale}".strip()
                ),
                action=f"{current.value}_gate",
                agent="gate",
                actor="founder_approval",
                outcome=GateOutcome.PIVOT_BACK.value,
                evidence_ids=ev_ids,
                metadata={"from_phase": current.value, "target_phase": dest.value},
            )
            return self._require_project(project_id)

        if norm_outcome == GateOutcome.STOP:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                    (ProjectStatus.STOPPED.value, now, project_id),
                )
            self._log(
                project_id=project_id,
                phase=current.value,
                decision=f"Founder STOPPED project at '{current.value}'. {rationale}".strip(),
                action=f"{current.value}_gate",
                agent="gate",
                actor="founder_approval",
                outcome=GateOutcome.STOP.value,
                evidence_ids=ev_ids,
            )
            return self._require_project(project_id)

        raise OrchestratorError(f"Unsupported gate outcome: {norm_outcome}")

    def _log(self, **fields: Any) -> None:
        """Append to the decision log, filling `sources` with the URLs behind the evidence IDs."""
        if "sources" not in fields:
            urls: list[str] = []
            for cid in fields.get("evidence_ids") or []:
                claim = self.evidence_ledger.get_claim(cid)
                if claim is not None and claim.source_url and claim.source_url not in urls:
                    urls.append(claim.source_url)
            fields["sources"] = urls
        self.decision_log.append(**fields)

    def _require_project(self, project_id: str) -> ProjectRecord:
        proj = self.get_project(project_id)
        if proj is None:
            raise OrchestratorError(f"Project not found: {project_id}")
        return proj

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            idea_summary=row["idea_summary"],
            target_customer=row["target_customer"],
            current_phase=PhaseName(row["current_phase"]),
            status=ProjectStatus(row["status"]),
            phase_iterations=json.loads(row["phase_iterations_json"]),
            validate_evidence_target=int(row["validate_evidence_target"]),
            recorded_demand_signals=int(row["recorded_demand_signals"]),
            build_tests_passed=bool(row["build_tests_passed"]),
            phase_artifacts=json.loads(row["phase_artifacts_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
