"""Stage 1 unit tests: Evidence Ledger and SHA-256 hash-chained Decision Log."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from ledger.decision_log import GENESIS_HASH, DecisionLog
from ledger.evidence_ledger import ClaimType, ConfidenceLevel, EvidenceLedger
from scripts.verify_decision_log import main as verify_log_cli


def test_claim_without_source_is_auto_tagged_assumption(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")

    # Even if caller passes FACT, missing or invalid URL must auto-downgrade to ASSUMPTION
    c1 = ledger.add_claim(
        "Students will pay $19 for a Notion exam tracker.",
        project_id="proj-1",
        linked_phase="discover",
        claim_type=ClaimType.FACT,
        source_url=None,
    )
    assert c1.claim_type == ClaimType.ASSUMPTION
    assert c1.source_url is None
    assert "ASSUMPTION" in c1.render_badge()

    c2 = ledger.add_claim(
        "Market size is $10B",
        project_id="proj-1",
        linked_phase="validate",
        claim_type=ClaimType.FACT,
        source_url="not-a-valid-url",
    )
    assert c2.claim_type == ClaimType.ASSUMPTION


def test_claim_with_valid_url_is_stored_as_fact_and_exported(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")

    fact = ledger.add_claim(
        "Notion surpassed 100 million users globally.",
        project_id="proj-1",
        linked_phase="validate",
        claim_type=ClaimType.FACT,
        source_url="https://www.notion.so/blog/100-million-users",
        confidence=ConfidenceLevel.HIGH,
    )
    assumption = ledger.add_claim(
        "5% of surveyed students will convert.",
        project_id="proj-1",
        linked_phase="validate",
    )

    assert fact.claim_type == ClaimType.FACT
    assert fact.retrieved_date is not None

    ok, missing = ledger.validate_claim_ids([fact.id, assumption.id], project_id="proj-1")
    assert ok is True
    assert missing == []

    ok_missing, missing_ids = ledger.validate_claim_ids([fact.id, "CLM-DOESNOTX"], project_id="proj-1")
    assert ok_missing is False
    assert missing_ids == ["CLM-DOESNOTX"]

    stats = ledger.summary_stats(project_id="proj-1")
    assert stats == {"total": 2, "facts": 1, "assumptions": 1, "sourced_percent": 50.0}

    md = ledger.export_markdown(project_id="proj-1")
    assert fact.id in md
    assert "https://www.notion.so/blog/100-million-users" in md
    assert "`ASSUMPTION`" in md


def test_decision_log_hash_chain_and_tamper_detection(tmp_path: Path) -> None:
    log_path = tmp_path / "DECISIONS.jsonl"
    log = DecisionLog(log_path)

    e1 = log.append(
        project_id="proj-1",
        phase="discover",
        decision="Proposed idea brief for AUC student Notion pack",
        actor="agent_proposal",
        outcome="proposed",
        evidence_ids=["CLM-1234ABCD"],
    )
    e2 = log.append(
        project_id="proj-1",
        phase="discover",
        decision="Founder approved Discover gate",
        actor="founder_approval",
        outcome="advance",
        evidence_ids=["CLM-1234ABCD"],
    )

    assert e1.seq == 1
    assert e1.prev_hash == GENESIS_HASH
    assert e2.seq == 2
    assert e2.prev_hash == e1.entry_hash

    verification = log.verify()
    assert verification.valid is True
    assert verification.entry_count == 2
    assert verification.head_hash == e2.entry_hash
    assert verify_log_cli([str(log_path)]) == 0

    # Tamper with line 1 decision text without updating hashes
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered_first = json.loads(lines[0])
    tampered_first["decision"] = "Silently altered decision"
    lines[0] = json.dumps(tampered_first, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_result = log.verify()
    assert tampered_result.valid is False
    assert any("entry_hash mismatch" in err for err in tampered_result.errors)
    assert verify_log_cli([str(log_path)]) == 1


def test_decision_log_rejects_invalid_actor_schema(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "DECISIONS.jsonl")
    with pytest.raises(jsonschema.ValidationError):
        log.append(
            phase="discover",
            decision="Invalid actor test",
            actor="rogue_actor",
            outcome="recorded",
        )
