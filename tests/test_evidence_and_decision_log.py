"""Smoke tests: evidence store and decision_log.json (required keys + hash chain)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from decision_log import GENESIS_HASH, REQUIRED_KEYS, DecisionLog
from evidence import ClaimType, ConfidenceLevel, EvidenceLedger
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

    ledger.record_fetch(
        project_id="proj-1",
        url="https://www.notion.so/blog/100-million-users",
        text="Today Notion surpassed 100 million users around the world.",
    )
    fact = ledger.add_claim(
        "Notion surpassed 100 million users globally.",
        project_id="proj-1",
        linked_phase="validate",
        claim_type=ClaimType.FACT,
        source_url="https://www.notion.so/blog/100-million-users",
        quote="Notion surpassed 100 million users",
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


def test_decision_log_required_keys_hash_chain_and_tamper_detection(tmp_path: Path) -> None:
    log_path = tmp_path / "decision_log.json"
    log = DecisionLog(log_path)

    e1 = log.append(
        project_id="proj-1",
        phase="discover",
        agent="researcher",
        action="find_opportunities",
        actor="agent_proposal",
        outcome="proposed",
        sources=["https://example.org/report"],
        model="qwen2.5:7b",
        evidence_ids=["CLM-1234ABCD"],
    )
    e2 = log.append(
        project_id="proj-1",
        phase="discover",
        agent="gate",
        action="discover_gate",
        actor="founder_approval",
        outcome="advance",
    )

    assert e1.seq == 1 and e1.prev_hash == GENESIS_HASH
    assert e2.seq == 2 and e2.prev_hash == e1.entry_hash
    assert e1.approved_by is None
    assert e2.approved_by == "founder"

    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 2
    for entry in data:
        for key in REQUIRED_KEYS:
            assert key in entry
    assert data[0]["cost"] == 0 and data[1]["model"] is None and data[1]["sources"] == []

    verification = log.verify()
    assert verification.valid is True
    assert verification.entry_count == 2
    assert verification.head_hash == e2.entry_hash
    assert verify_log_cli([str(log_path)]) == 0

    # Tamper with entry 1 without updating hashes
    data[0]["action"] = "silently_altered"
    log_path.write_text(json.dumps(data), encoding="utf-8")
    tampered = log.verify()
    assert tampered.valid is False
    assert any("entry_hash mismatch" in err for err in tampered.errors)
    assert verify_log_cli([str(log_path)]) == 1


def test_decision_log_rejects_bad_entries(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision_log.json")
    with pytest.raises(ValueError):
        log.append(action="x", actor="rogue_actor")
    with pytest.raises(jsonschema.ValidationError):
        log.append(action="x", sources=["not-a-url"])

    # An entry missing a required key fails verification
    path = tmp_path / "missing.json"
    path.write_text(json.dumps([{"timestamp": "2026-11-17T09:00:00Z", "agent": "a", "action": "b"}]))
    result = DecisionLog(path).verify()
    assert result.valid is False
    assert any("missing required key" in err for err in result.errors)


def test_url_that_was_never_fetched_is_an_assumption(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")
    claim = ledger.add_claim(
        "The market is worth 4 billion dollars.",
        project_id="proj-1",
        source_url="https://example.org/made-up-report",
        quote="The market is worth 4 billion dollars.",
    )
    assert claim.claim_type == ClaimType.ASSUMPTION
    assert claim.source_url is None


def test_quote_missing_from_fetched_page_is_an_assumption(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")
    url = "https://example.org/report"
    ledger.record_fetch(project_id="proj-1", url=url, text="Freelancers spend hours on invoices.")
    invented = ledger.add_claim(
        "Most freelancers want this.", project_id="proj-1", source_url=url,
        quote="90 percent of freelancers would pay for this tool",
    )
    other_project = ledger.add_claim(
        "Freelancers spend hours on invoices.", project_id="proj-2", source_url=url,
        quote="Freelancers spend hours on invoices.",
    )
    real = ledger.add_claim(
        "Invoicing takes freelancers hours.", project_id="proj-1", source_url=url,
        quote="freelancers spend   hours on invoices",
    )
    assert invented.claim_type == ClaimType.ASSUMPTION
    assert other_project.claim_type == ClaimType.ASSUMPTION
    assert real.claim_type == ClaimType.FACT
    assert ledger.get_claim(real.id).quote == "freelancers spend   hours on invoices"


def test_quote_matching_ignores_footnotes_and_keeps_only_the_supported_sentence(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")
    url = "https://example.org/report"
    ledger.record_fetch(
        project_id="p", url=url,
        text="Nearly half of freelancers do writing work,[14] with 18% listing it as a primary skill.",
    )
    clean = ledger.add_claim(
        "Writing is common among freelancers.", project_id="p", source_url=url,
        quote="Nearly half of freelancers do writing work, with 18% listing it as a primary skill.",
    )
    assert clean.claim_type == ClaimType.FACT

    mixed = ledger.add_claim(
        "Writing is common.", project_id="p", source_url=url,
        quote="Nearly half of freelancers do writing work, with 18% listing it as a primary skill. "
              "They all earn six figures within a year of starting.",
    )
    assert mixed.claim_type == ClaimType.FACT
    assert "six figures" not in mixed.quote

    short = ledger.add_claim("Writing.", project_id="p", source_url=url, quote="freelancers do writing")
    assert short.claim_type == ClaimType.ASSUMPTION


@pytest.mark.parametrize("citation", ["[14]", "[1, 3]", "[2-4]"])
def test_quote_matching_ignores_numeric_citations(citation: str, tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")
    url = "https://example.org/report"
    quote = "Participants reported spending hours preparing invoices."
    ledger.record_fetch(project_id="p", url=url, text=f"Participants{citation} reported spending hours preparing invoices.")

    claim = ledger.add_claim(quote, project_id="p", source_url=url, quote=quote)

    assert claim.claim_type == ClaimType.FACT


def test_quote_matching_never_erases_bracketed_negation(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite3")
    url = "https://example.org/report"
    source = "Participants did [not] report spending hours preparing invoices."
    ledger.record_fetch(project_id="p", url=url, text=source)

    contradicted = ledger.add_claim(
        "Participants did report spending hours preparing invoices.",
        project_id="p", source_url=url,
        quote="Participants did report spending hours preparing invoices.",
    )
    faithful = ledger.add_claim(source, project_id="p", source_url=url, quote=source)

    assert contradicted.claim_type == ClaimType.ASSUMPTION
    assert faithful.claim_type == ClaimType.FACT
