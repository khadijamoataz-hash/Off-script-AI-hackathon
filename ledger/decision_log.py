"""Append-only, SHA-256 hash-chained Decision Log (JSONL) with JSON Schema validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, Field

GENESIS_HASH = "0" * 64

DECISION_ENTRY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FoundryDecisionLogEntry",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "seq",
        "project_id",
        "phase",
        "decision",
        "actor",
        "outcome",
        "evidence_ids",
        "metadata",
        "timestamp",
        "prev_hash",
        "entry_hash",
    ],
    "properties": {
        "seq": {"type": "integer", "minimum": 1},
        "project_id": {"type": "string", "minLength": 1},
        "phase": {
            "type": "string",
            "enum": [
                "system",
                "discover",
                "validate",
                "build",
                "commercial",
                "launch",
                "operate",
            ],
        },
        "decision": {"type": "string", "minLength": 1},
        "actor": {
            "type": "string",
            "enum": [
                "system",
                "agent_proposal",
                "founder_approval",
                "founder_rejection",
                "policy_engine",
                "gate_check",
            ],
        },
        "outcome": {
            "type": "string",
            "enum": [
                "recorded",
                "proposed",
                "approved",
                "rejected",
                "blocked",
                "advance",
                "repeat",
                "pivot_back",
                "stop",
            ],
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "metadata": {"type": "object"},
        "timestamp": {"type": "string", "minLength": 10},
        "prev_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "entry_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}


def compute_entry_hash(payload_without_hash: dict[str, Any]) -> str:
    """Compute canonical SHA-256 over the entry payload (excluding entry_hash)."""
    canonical = json.dumps(
        payload_without_hash, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DecisionEntry(BaseModel):
    seq: int
    project_id: str = "default"
    phase: str
    decision: str
    actor: str
    outcome: str = "recorded"
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: str
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class DecisionLogVerificationResult:
    valid: bool
    entry_count: int
    head_hash: str
    errors: list[str]


class DecisionLog:
    """Append-only JSONL decision log with cryptographic hash chaining."""

    def __init__(self, log_path: str | Path = "DECISIONS.jsonl") -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    def append(
        self,
        *,
        phase: str,
        decision: str,
        actor: str,
        outcome: str = "recorded",
        project_id: str = "default",
        evidence_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> DecisionEntry:
        """Append a schema-validated, hash-chained entry to the JSONL file."""
        entries = self.read_all()
        seq = len(entries) + 1
        prev_hash = entries[-1].entry_hash if entries else GENESIS_HASH
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        unsigned: dict[str, Any] = {
            "seq": seq,
            "project_id": project_id,
            "phase": phase.lower(),
            "decision": decision.strip(),
            "actor": actor,
            "outcome": outcome,
            "evidence_ids": list(evidence_ids or []),
            "metadata": dict(metadata or {}),
            "timestamp": ts,
            "prev_hash": prev_hash,
        }
        entry_hash = compute_entry_hash(unsigned)
        full_record = {**unsigned, "entry_hash": entry_hash}

        jsonschema.validate(instance=full_record, schema=DECISION_ENTRY_SCHEMA)

        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(full_record, sort_keys=True) + "\n")

        return DecisionEntry(**full_record)

    def read_all(self, *, project_id: str | None = None) -> list[DecisionEntry]:
        if not self.log_path.exists():
            return []
        entries: list[DecisionEntry] = []
        for raw_line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            data = json.loads(line)
            entry = DecisionEntry(**data)
            if project_id is None or entry.project_id == project_id:
                entries.append(entry)
        return entries

    def verify(self) -> DecisionLogVerificationResult:
        """Verify JSON syntax, JSON Schema conformance, sequence continuity, and SHA-256 chain."""
        if not self.log_path.exists():
            return DecisionLogVerificationResult(
                valid=False,
                entry_count=0,
                head_hash=GENESIS_HASH,
                errors=[f"Decision log file does not exist: {self.log_path}"],
            )

        errors: list[str] = []
        expected_prev = GENESIS_HASH
        expected_seq = 1
        count = 0

        raw_lines = self.log_path.read_text(encoding="utf-8").splitlines()
        for line_no, raw_line in enumerate(raw_lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            count += 1
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f"Line {line_no}: invalid JSON ({exc})")
                continue

            try:
                jsonschema.validate(instance=record, schema=DECISION_ENTRY_SCHEMA)
            except jsonschema.ValidationError as exc:
                errors.append(f"Line {line_no}: schema violation ({exc.message})")
                continue

            if record["seq"] != expected_seq:
                errors.append(
                    f"Line {line_no}: expected seq {expected_seq}, got {record['seq']}"
                )
            if record["prev_hash"] != expected_prev:
                errors.append(
                    f"Line {line_no}: broken hash chain (prev_hash={record['prev_hash'][:12]}... "
                    f"!= expected {expected_prev[:12]}...)"
                )

            unsigned = {k: v for k, v in record.items() if k != "entry_hash"}
            recomputed = compute_entry_hash(unsigned)
            if record["entry_hash"] != recomputed:
                errors.append(
                    f"Line {line_no}: entry_hash mismatch (stored={record['entry_hash'][:12]}... "
                    f"!= computed={recomputed[:12]}...)"
                )

            expected_prev = record["entry_hash"]
            expected_seq += 1

        return DecisionLogVerificationResult(
            valid=(len(errors) == 0),
            entry_count=count,
            head_hash=expected_prev,
            errors=errors,
        )
