"""SQLite-backed Evidence Ledger.

Every factual claim must have a source URL, or it is automatically tagged ASSUMPTION.
Agents reference claim IDs in phase reports so founders and judges can audit every statement.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


class ClaimType(str, Enum):
    FACT = "FACT"
    ASSUMPTION = "ASSUMPTION"


class ConfidenceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_valid_http_url(url: str | None) -> bool:
    if not url or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class Claim(BaseModel):
    """A single claim stored in the Evidence Ledger."""

    id: str = Field(default_factory=lambda: f"CLM-{uuid.uuid4().hex[:8].upper()}")
    project_id: str = Field(default="default")
    text: str = Field(min_length=1)
    claim_type: ClaimType = Field(default=ClaimType.ASSUMPTION)
    source_url: str | None = Field(default=None)
    retrieved_date: str | None = Field(default=None)
    confidence: ConfidenceLevel = Field(default=ConfidenceLevel.MEDIUM)
    linked_phase: str = Field(default="discover")
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def enforce_evidence_rule(self) -> "Claim":
        """Hard rule: any claim without a valid HTTP(S) source URL is auto-tagged ASSUMPTION."""
        if _is_valid_http_url(self.source_url):
            self.source_url = self.source_url.strip()  # type: ignore[union-attr]
            if not self.retrieved_date:
                self.retrieved_date = _utc_now_iso()
        else:
            self.source_url = None
            self.retrieved_date = None
            self.claim_type = ClaimType.ASSUMPTION
        return self

    def render_badge(self) -> str:
        """Render human-auditable citation or explicit ASSUMPTION label."""
        if self.claim_type == ClaimType.FACT and self.source_url:
            return f"[{self.id} | FACT | source: {self.source_url}]"
        return f"[{self.id} | ASSUMPTION]"


CLAIM_REF_PATTERN = re.compile(r"\[(CLM-[A-Z0-9]{8})\]")


class EvidenceLedger:
    """SQLite persistence and verification for project claims."""

    def __init__(self, db_path: str | Path = "data/foundry.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    claim_type TEXT NOT NULL CHECK (claim_type IN ('FACT', 'ASSUMPTION')),
                    source_url TEXT,
                    retrieved_date TEXT,
                    confidence TEXT NOT NULL,
                    linked_phase TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_project_phase ON claims(project_id, linked_phase)"
            )

    def add_claim(
        self,
        text: str,
        *,
        project_id: str = "default",
        linked_phase: str = "discover",
        source_url: str | None = None,
        claim_type: ClaimType | str = ClaimType.FACT,
        confidence: ConfidenceLevel | str = ConfidenceLevel.MEDIUM,
        retrieved_date: str | None = None,
        claim_id: str | None = None,
    ) -> Claim:
        """Record a claim, automatically downgrading un-sourced claims to ASSUMPTION."""
        normalized_type = ClaimType(claim_type) if isinstance(claim_type, str) else claim_type
        normalized_conf = (
            ConfidenceLevel(confidence) if isinstance(confidence, str) else confidence
        )
        kwargs: dict[str, object] = {
            "project_id": project_id,
            "text": text.strip(),
            "claim_type": normalized_type,
            "source_url": source_url,
            "retrieved_date": retrieved_date,
            "confidence": normalized_conf,
            "linked_phase": linked_phase.lower(),
        }
        if claim_id:
            kwargs["id"] = claim_id
        claim = Claim(**kwargs)  # type: ignore[arg-type]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO claims (
                    id, project_id, text, claim_type, source_url,
                    retrieved_date, confidence, linked_phase, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.id,
                    claim.project_id,
                    claim.text,
                    claim.claim_type.value,
                    claim.source_url,
                    claim.retrieved_date,
                    claim.confidence.value,
                    claim.linked_phase,
                    claim.created_at,
                ),
            )
        return claim

    def get_claim(self, claim_id: str) -> Claim | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if not row:
            return None
        return self._row_to_claim(row)

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        linked_phase: str | None = None,
        claim_type: ClaimType | None = None,
    ) -> list[Claim]:
        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if linked_phase is not None:
            clauses.append("linked_phase = ?")
            params.append(linked_phase.lower())
        if claim_type is not None:
            clauses.append("claim_type = ?")
            params.append(claim_type.value)

        query = "SELECT * FROM claims"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def validate_claim_ids(
        self, claim_ids: Iterable[str], *, project_id: str | None = None
    ) -> tuple[bool, list[str]]:
        """Check that all referenced claim IDs exist in the ledger (and belong to project_id if given)."""
        missing: list[str] = []
        for cid in claim_ids:
            claim = self.get_claim(cid)
            if claim is None or (project_id is not None and claim.project_id != project_id):
                missing.append(cid)
        return (len(missing) == 0, missing)

    def summary_stats(self, *, project_id: str | None = None) -> dict[str, int | float]:
        claims = self.list_claims(project_id=project_id)
        total = len(claims)
        facts = sum(1 for c in claims if c.claim_type == ClaimType.FACT)
        assumptions = total - facts
        sourced_pct = round((facts / total) * 100.0, 1) if total > 0 else 0.0
        return {
            "total": total,
            "facts": facts,
            "assumptions": assumptions,
            "sourced_percent": sourced_pct,
        }

    def export_markdown(self, *, project_id: str | None = None) -> str:
        """Export claims table as Markdown for phase reports and judge verification."""
        claims = self.list_claims(project_id=project_id)
        if not claims:
            return "_No claims recorded in Evidence Ledger._"
        lines = [
            "| Claim ID | Type | Phase | Confidence | Claim | Source |",
            "|---|---|---|---|---|---|",
        ]
        for c in claims:
            source_cell = f"[source]({c.source_url})" if c.source_url else "`ASSUMPTION`"
            safe_text = c.text.replace("|", "\\|")
            lines.append(
                f"| `{c.id}` | **{c.claim_type.value}** | `{c.linked_phase}` | "
                f"{c.confidence.value} | {safe_text} | {source_cell} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        return Claim(
            id=row["id"],
            project_id=row["project_id"],
            text=row["text"],
            claim_type=ClaimType(row["claim_type"]),
            source_url=row["source_url"],
            retrieved_date=row["retrieved_date"],
            confidence=ConfidenceLevel(row["confidence"]),
            linked_phase=row["linked_phase"],
            created_at=row["created_at"],
        )
