"""Gates: what each phase gate checks, and what the system recommends.

Every gate is one of three types: founder approval, an evidence target set in advance, or
passing tests. `evaluate_gate` checks the objective part and returns a recommendation
(advance, repeat, pivot_back or stop). The founder always makes the final decision in
`orchestrator.py`; the recommendation is advice.

When evidence is weak the system says so: if the Validate evidence is below
`STOP_BELOW_FRACTION` of the target that was set before the phase started, the recommendation
is STOP rather than a softened "keep going".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from approval import ActionStatus, OutboxService
from evidence import ClaimType, EvidenceLedger

# Below this share of the pre-set evidence target, the system recommends STOP.
STOP_BELOW_FRACTION = 0.5


class PhaseName(str, Enum):
    DISCOVER = "discover"
    VALIDATE = "validate"
    BUILD = "build"
    COMMERCIAL = "commercial"
    LAUNCH = "launch"
    OPERATE = "operate"


PHASE_ORDER: tuple[PhaseName, ...] = (
    PhaseName.DISCOVER,
    PhaseName.VALIDATE,
    PhaseName.BUILD,
    PhaseName.COMMERCIAL,
    PhaseName.LAUNCH,
    PhaseName.OPERATE,
)


class GateType(str, Enum):
    FOUNDER_APPROVAL = "founder_approval"
    EVIDENCE_TARGET = "evidence_target"
    TESTS_PASS = "tests_pass"


class GateOutcome(str, Enum):
    ADVANCE = "advance"
    REPEAT = "repeat"
    PIVOT_BACK = "pivot_back"
    STOP = "stop"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PhaseDefinition:
    name: PhaseName
    index: int
    title: str
    entry_criteria: str
    gate_type: GateType
    gate_description: str


PHASE_DEFINITIONS: dict[PhaseName, PhaseDefinition] = {
    PhaseName.DISCOVER: PhaseDefinition(
        name=PhaseName.DISCOVER,
        index=0,
        title="1. Discover",
        entry_criteria="Founder provides an initial digital product idea and target audience.",
        gate_type=GateType.FOUNDER_APPROVAL,
        gate_description="Discover artifact exists and founder approves the sharpened idea brief.",
    ),
    PhaseName.VALIDATE: PhaseDefinition(
        name=PhaseName.VALIDATE,
        index=1,
        title="2. Validate",
        entry_criteria="Approved idea brief + pre-committed evidence target set before validation.",
        gate_type=GateType.EVIDENCE_TARGET,
        gate_description="Sourced FACT claims + recorded demand signals >= validate_evidence_target, plus founder approval.",
    ),
    PhaseName.BUILD: PhaseDefinition(
        name=PhaseName.BUILD,
        index=2,
        title="3. Build",
        entry_criteria="Validated demand target met in Phase 2.",
        gate_type=GateType.TESTS_PASS,
        gate_description="Digital product scaffolded in sandbox with passing automated tests + founder review.",
    ),
    PhaseName.COMMERCIAL: PhaseDefinition(
        name=PhaseName.COMMERCIAL,
        index=3,
        title="4. Commercial Setup",
        entry_criteria="Working, tested product artifact from Phase 3.",
        gate_type=GateType.FOUNDER_APPROVAL,
        gate_description="Pricing options (ASSUMPTION-labeled) + Terms/Refund/Privacy drafts approved by founder (zero payment code).",
    ),
    PhaseName.LAUNCH: PhaseDefinition(
        name=PhaseName.LAUNCH,
        index=4,
        title="5. Launch",
        entry_criteria="Approved commercial pricing and policy drafts.",
        gate_type=GateType.FOUNDER_APPROVAL,
        gate_description="Launch drafts queued in Outbox; no unreviewed PENDING items remain and at least 1 action approved/executed.",
    ),
    PhaseName.OPERATE: PhaseDefinition(
        name=PhaseName.OPERATE,
        index=5,
        title="6. Operate & Learn",
        entry_criteria="Approved launch assets exported.",
        gate_type=GateType.FOUNDER_APPROVAL,
        gate_description="Founder reviews post-launch learnings and chooses continue (complete), repeat, pivot_back, or stop.",
    ),
}


class GateCheckResult(BaseModel):
    ready_to_advance: bool
    phase: PhaseName
    gate_type: GateType
    blockers: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    recommendation: GateOutcome = GateOutcome.REPEAT
    recommendation_reason: str = ""


def _recommend(
    phase: PhaseName, ready: bool, metrics: dict[str, Any]
) -> tuple[GateOutcome, str]:
    if ready:
        return GateOutcome.ADVANCE, "Gate criteria met."
    if phase == PhaseName.VALIDATE and metrics.get("target"):
        have = int(metrics.get("combined_evidence_count", 0))
        target = int(metrics["target"])
        if have < target * STOP_BELOW_FRACTION:
            return (
                GateOutcome.STOP,
                f"Evidence is weak: {have} of the {target} set in advance. "
                "Recommend stopping or pivoting rather than building on this.",
            )
        return GateOutcome.REPEAT, f"Evidence is {have} of {target}: close, gather more first."
    return GateOutcome.REPEAT, "Gate criteria not met yet."


def evaluate_gate(
    proj: Any, evidence_ledger: EvidenceLedger, approvals: OutboxService
) -> GateCheckResult:
    """Evaluate the objective gate prerequisites for the project's current phase."""
    project_id = proj.id
    phase = proj.current_phase
    defn = PHASE_DEFINITIONS[phase]
    blockers: list[str] = []
    metrics: dict[str, Any] = {}

    if phase.value not in proj.phase_artifacts:
        blockers.append(
            f"Phase '{phase.value}' has no generated/saved artifact yet."
        )

    if phase == PhaseName.VALIDATE:
        fact_claims = evidence_ledger.list_claims(
            project_id=project_id,
            claim_type=ClaimType.FACT,
        )
        evidence_score = len(fact_claims) + proj.recorded_demand_signals
        metrics = {
            "sourced_facts": len(fact_claims),
            "recorded_demand_signals": proj.recorded_demand_signals,
            "combined_evidence_count": evidence_score,
            "target": proj.validate_evidence_target,
        }
        if len(fact_claims) == 0:
            blockers.append(
                "Validate gate requires at least 1 sourced FACT claim in the Evidence Ledger."
            )
        if evidence_score < proj.validate_evidence_target:
            blockers.append(
                f"Evidence target not yet reached: have {evidence_score} "
                f"({len(fact_claims)} sourced facts + {proj.recorded_demand_signals} signals), "
                f"need >= {proj.validate_evidence_target}."
            )

    elif phase == PhaseName.BUILD:
        metrics = {"build_tests_passed": proj.build_tests_passed}
        if not proj.build_tests_passed:
            blockers.append(
                "Build gate requires automated sandbox tests to pass (`build_tests_passed=True`)."
            )

    elif phase == PhaseName.LAUNCH:
        actions = approvals.list_actions(project_id=project_id)
        launch_actions = [a for a in actions if a.phase == PhaseName.LAUNCH.value]
        pending = [a for a in launch_actions if a.status == ActionStatus.PENDING]
        approved_or_executed = [
            a
            for a in launch_actions
            if a.status in {ActionStatus.APPROVED, ActionStatus.EXECUTED}
        ]
        metrics = {
            "total_launch_actions": len(launch_actions),
            "pending_actions": len(pending),
            "approved_or_executed_actions": len(approved_or_executed),
        }
        if not launch_actions:
            blockers.append("Launch gate requires at least 1 proposed action in the Outbox.")
        if pending:
            blockers.append(
                f"{len(pending)} Outbox item(s) are still PENDING founder approval/rejection."
            )
        if not approved_or_executed:
            blockers.append(
                "At least 1 Launch Outbox action must be approved by the founder to advance."
            )

    ready = len(blockers) == 0
    recommendation, reason = _recommend(phase, ready, metrics)
    return GateCheckResult(
        ready_to_advance=ready,
        phase=phase,
        gate_type=defn.gate_type,
        blockers=blockers,
        metrics=metrics,
        recommendation=recommendation,
        recommendation_reason=reason,
    )
