"""MOCKED delivery of a digital product.

Nothing here takes, holds, moves or refunds money, and no real buyer or file is involved.
A simulated order event grants a simulated access record; a simulated refund event revokes
it. The module exists to prove two rules of the sale-to-cash design in code:

- delivery is idempotent per order id (a repeated event never delivers twice);
- a refund revokes access.

Every record and every log line is labelled MOCKED.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

LABEL = "MOCKED"


class DeliveryRecord(BaseModel):
    order_id: str
    project_id: str
    product_name: str
    access_token: str
    status: str  # "delivered" or "revoked"
    delivered_at: str
    revoked_at: str | None = None
    label: str = LABEL


class DeliveryResult(BaseModel):
    record: DeliveryRecord
    newly_delivered: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockDeliveryService:
    """SQLite-backed simulated delivery. The order id is the idempotency key."""

    def __init__(self, db_path: str | Path = "data/foundry.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mock_deliveries (
                    order_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('delivered', 'revoked')),
                    delivered_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, order_id: str) -> DeliveryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mock_deliveries WHERE order_id = ?", (order_id,)
            ).fetchone()
        return DeliveryRecord(**dict(row)) if row else None

    def deliver(self, *, order_id: str, project_id: str, product_name: str) -> DeliveryResult:
        """Handle a simulated 'order confirmed' event. Repeats for the same order do nothing."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO mock_deliveries "
                "(order_id, project_id, product_name, access_token, status, delivered_at) "
                "VALUES (?, ?, ?, ?, 'delivered', ?)",
                (order_id, project_id, product_name, f"mock-access-{uuid.uuid4().hex[:12]}", _now()),
            )
            created = cursor.rowcount == 1
        record = self.get(order_id)
        assert record is not None
        return DeliveryResult(record=record, newly_delivered=created)

    def revoke(self, order_id: str) -> DeliveryRecord:
        """Handle a simulated 'refund' event: access for that order is revoked."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE mock_deliveries SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?) "
                "WHERE order_id = ?",
                (_now(), order_id),
            )
        record = self.get(order_id)
        if record is None:
            raise KeyError(f"No simulated delivery exists for order {order_id!r}.")
        return record

    def has_access(self, order_id: str) -> bool:
        record = self.get(order_id)
        return record is not None and record.status == "delivered"


def run_mock_delivery_test(
    service: MockDeliveryService, *, project_id: str, product_name: str
) -> str:
    """Run the simulated order, repeat and refund sequence and return its log as Markdown."""
    order_id = f"MOCK-ORDER-{uuid.uuid4().hex[:8].upper()}"
    first = service.deliver(order_id=order_id, project_id=project_id, product_name=product_name)
    repeat = service.deliver(order_id=order_id, project_id=project_id, product_name=product_name)
    access_before = service.has_access(order_id)
    revoked = service.revoke(order_id)
    access_after = service.has_access(order_id)
    late_repeat = service.deliver(order_id=order_id, project_id=project_id, product_name=product_name)

    checks = [
        ("First order event delivers once", first.newly_delivered),
        ("Repeated event for the same order does not deliver again",
         not repeat.newly_delivered and repeat.record.access_token == first.record.access_token),
        ("Buyer has access after delivery", access_before),
        ("Refund event revokes access", revoked.status == "revoked" and not access_after),
        ("Order event after a refund does not restore access",
         not late_repeat.newly_delivered and not service.has_access(order_id)),
    ]
    rows = "\n".join(f"| {name} | {'PASS' if ok else 'FAIL'} |" for name, ok in checks)
    return (
        "# Delivery test log (MOCKED)\n\n"
        "**No payment took place.** Foundry contains no payment code. This is a simulated run of "
        "the delivery rules only: the order, the buyer and the access token are all `MOCKED`.\n\n"
        f"- Project: `{project_id}`\n- Product: {product_name}\n"
        f"- Simulated order id: `{order_id}`\n- Run at: {_now()}\n\n"
        "| Check | Result |\n|---|---|\n"
        f"{rows}\n\n"
        f"Result: {sum(ok for _, ok in checks)}/{len(checks)} checks passed.\n"
    )
