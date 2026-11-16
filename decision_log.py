"""Decision log: `decision_log.json`, one entry per agent action, SHA-256 hash-chained.

File format: a JSON array. Every entry has the required keys

    timestamp, agent, action, sources, model, cost, approved_by

plus Foundry's own keys: seq, project_id, phase, outcome, detail, evidence_ids, metadata,
prev_hash, entry_hash. `entry_hash` is the SHA-256 of the entry without `entry_hash`, and
`prev_hash` is the previous entry's `entry_hash`, so any edit to an earlier entry is detectable.

Only code writes this file. Entries are never typed by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, Field

try:  # POSIX (Linux containers, macOS). Not available on native Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

GENESIS_HASH = "0" * 64
DEFAULT_LOG_PATH = "decision_log.json"
REQUIRED_KEYS = ("timestamp", "agent", "action", "sources", "model", "cost", "approved_by")

PHASES = ["system", "discover", "validate", "build", "commercial", "launch", "operate"]

# Internal actor roles (who produced the entry) mapped to the `agent` value written to the log
# when the caller does not name a specific agent.
ACTOR_TO_AGENT = {
    "system": "system",
    "agent_proposal": "agent",
    "founder_approval": "founder",
    "founder_rejection": "founder",
    "policy_engine": "guardrail",
    "gate_check": "gate",
}

DECISION_ENTRY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FoundryDecisionLogEntry",
    "type": "object",
    "required": [*REQUIRED_KEYS, "seq", "phase", "prev_hash", "entry_hash"],
    "properties": {
        "timestamp": {"type": "string", "minLength": 10},
        "agent": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "sources": {"type": "array", "items": {"type": "string", "pattern": "^https?://"}},
        "model": {"type": ["string", "null"]},
        "cost": {"type": "number", "minimum": 0},
        "approved_by": {"type": ["string", "null"]},
        "seq": {"type": "integer", "minimum": 1},
        "project_id": {"type": "string"},
        "phase": {"type": "string", "enum": PHASES},
        "outcome": {"type": ["string", "null"]},
        "detail": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "metadata": {"type": "object"},
        "prev_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "entry_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}


def compute_entry_hash(payload_without_hash: dict[str, Any]) -> str:
    """Canonical SHA-256 over the entry (sorted keys, compact separators), excluding entry_hash."""
    canonical = json.dumps(
        payload_without_hash, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize(entries: list[dict[str, Any]]) -> str:
    """One entry per line inside a JSON array, so diffs stay readable."""
    if not entries:
        return "[]\n"
    return "[\n" + ",\n".join("  " + json.dumps(e, ensure_ascii=True) for e in entries) + "\n]\n"


class DecisionEntry(BaseModel):
    timestamp: str
    agent: str
    action: str
    sources: list[str] = Field(default_factory=list)
    model: str | None = None
    cost: float = 0
    approved_by: str | None = None
    seq: int
    project_id: str = "default"
    phase: str = "system"
    outcome: str | None = None
    detail: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class DecisionLogVerificationResult:
    valid: bool
    entry_count: int
    head_hash: str
    errors: list[str]


class DecisionLog:
    """Append-only decision log stored as a hash-chained JSON array."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        self.log_path = Path(
            log_path or os.environ.get("FOUNDRY_DECISION_LOG") or DEFAULT_LOG_PATH
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("[]\n", encoding="utf-8")

    def append(
        self,
        *,
        action: str,
        phase: str = "system",
        actor: str = "system",
        agent: str | None = None,
        decision: str = "",
        outcome: str | None = None,
        sources: list[str] | None = None,
        model: str | None = None,
        cost: float = 0,
        approved_by: str | None = None,
        project_id: str = "default",
        evidence_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> DecisionEntry:
        """Append one schema-validated, hash-chained entry.

        `actor` is the internal role producing the entry. A founder approval sets
        `approved_by="founder"` automatically. `decision` is the human-readable detail.
        """
        if actor not in ACTOR_TO_AGENT:
            raise ValueError(f"Unknown actor '{actor}'. Expected one of {sorted(ACTOR_TO_AGENT)}.")
        if approved_by is None and actor == "founder_approval":
            approved_by = "founder"

        # Exclusive lock for the whole read-compute-write, so concurrent writers can never
        # produce the same seq / prev_hash. The file is rewritten in place (not replaced), which
        # also works when it is bind-mounted into a container as a single file.
        with self.log_path.open("r+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                raw = fh.read().strip()
                entries: list[dict[str, Any]] = json.loads(raw) if raw else []
                prev_hash = entries[-1]["entry_hash"] if entries else GENESIS_HASH

                unsigned: dict[str, Any] = {
                    "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
                    "agent": agent or ACTOR_TO_AGENT[actor],
                    "action": action,
                    "sources": list(sources or []),
                    "model": model,
                    "cost": cost,
                    "approved_by": approved_by,
                    "seq": len(entries) + 1,
                    "project_id": project_id,
                    "phase": phase.lower(),
                    "outcome": outcome,
                    "detail": decision.strip(),
                    "evidence_ids": list(evidence_ids or []),
                    "metadata": dict(metadata or {}),
                    "prev_hash": prev_hash,
                }
                record = {**unsigned, "entry_hash": compute_entry_hash(unsigned)}
                jsonschema.validate(instance=record, schema=DECISION_ENTRY_SCHEMA)

                entries.append(record)
                fh.seek(0)
                fh.truncate()
                fh.write(_serialize(entries))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        return DecisionEntry(**record)

    def _load(self) -> list[dict[str, Any]]:
        raw = self.log_path.read_text(encoding="utf-8").strip() if self.log_path.exists() else ""
        data = json.loads(raw) if raw else []
        if not isinstance(data, list):
            raise ValueError("decision log must be a JSON array")
        return data

    def read_all(self, *, project_id: str | None = None) -> list[DecisionEntry]:
        entries = [DecisionEntry(**item) for item in self._load()]
        if project_id is None:
            return entries
        return [e for e in entries if e.project_id == project_id]

    def verify(self) -> DecisionLogVerificationResult:
        """Check JSON syntax, the required keys and types, sequence continuity and the hash chain."""
        if not self.log_path.exists():
            return DecisionLogVerificationResult(
                False, 0, GENESIS_HASH, [f"Decision log file does not exist: {self.log_path}"]
            )
        try:
            data = self._load()
        except (json.JSONDecodeError, ValueError) as exc:
            return DecisionLogVerificationResult(False, 0, GENESIS_HASH, [f"Invalid JSON: {exc}"])

        errors: list[str] = []
        expected_prev = GENESIS_HASH
        for index, record in enumerate(data, start=1):
            if not isinstance(record, dict):
                errors.append(f"Entry {index}: not a JSON object")
                continue
            missing = [k for k in REQUIRED_KEYS if k not in record]
            if missing:
                errors.append(f"Entry {index}: missing required key(s) {missing}")
                continue
            try:
                jsonschema.validate(instance=record, schema=DECISION_ENTRY_SCHEMA)
            except jsonschema.ValidationError as exc:
                errors.append(f"Entry {index}: schema violation ({exc.message})")
                continue

            if record["seq"] != index:
                errors.append(f"Entry {index}: expected seq {index}, got {record['seq']}")
            if record["prev_hash"] != expected_prev:
                errors.append(
                    f"Entry {index}: broken hash chain (prev_hash={record['prev_hash'][:12]}... "
                    f"!= expected {expected_prev[:12]}...)"
                )
            recomputed = compute_entry_hash({k: v for k, v in record.items() if k != "entry_hash"})
            if record["entry_hash"] != recomputed:
                errors.append(
                    f"Entry {index}: entry_hash mismatch (stored={record['entry_hash'][:12]}... "
                    f"!= computed={recomputed[:12]}...)"
                )
            expected_prev = record["entry_hash"]

        return DecisionLogVerificationResult(
            valid=(len(errors) == 0),
            entry_count=len(data),
            head_hash=expected_prev,
            errors=errors,
        )
