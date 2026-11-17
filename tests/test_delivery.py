"""MOCKED delivery: idempotent per order, and a refund revokes access."""

from __future__ import annotations

from pathlib import Path

import pytest

from delivery import MockDeliveryService, run_mock_delivery_test


def test_delivery_is_idempotent_per_order(tmp_path: Path) -> None:
    service = MockDeliveryService(tmp_path / "db.sqlite3")
    first = service.deliver(order_id="o-1", project_id="p", product_name="Guide")
    again = service.deliver(order_id="o-1", project_id="p", product_name="Guide")
    assert first.newly_delivered is True and again.newly_delivered is False
    assert again.record.access_token == first.record.access_token
    assert first.record.label == "MOCKED"


def test_refund_revokes_access_and_stays_revoked(tmp_path: Path) -> None:
    service = MockDeliveryService(tmp_path / "db.sqlite3")
    service.deliver(order_id="o-1", project_id="p", product_name="Guide")
    assert service.has_access("o-1")
    assert service.revoke("o-1").status == "revoked"
    assert not service.has_access("o-1")
    service.deliver(order_id="o-1", project_id="p", product_name="Guide")
    assert not service.has_access("o-1")
    with pytest.raises(KeyError):
        service.revoke("unknown-order")


def test_generated_log_says_mocked_and_no_payment(tmp_path: Path) -> None:
    log = run_mock_delivery_test(MockDeliveryService(tmp_path / "db.sqlite3"), project_id="p", product_name="Guide")
    assert "MOCKED" in log and "No payment took place" in log
    assert "5/5 checks passed" in log and "FAIL" not in log
