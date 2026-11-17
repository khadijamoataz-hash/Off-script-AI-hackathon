#!/usr/bin/env python3
"""Run the MOCKED delivery test on its own and write outputs/payment_test_log.md.

No payment takes place: Foundry has no payment code. This runs the simulated order, repeat
and refund sequence from `delivery.py` and records the run in the decision log.

    python scripts/run_delivery_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decision_log import DecisionLog  # noqa: E402
from delivery import MockDeliveryService, run_mock_delivery_test  # noqa: E402


def main() -> int:
    out_dir = Path(os.environ.get("FOUNDRY_OUTPUTS_DIR", ROOT / "outputs"))
    db_path = Path(os.environ.get("FOUNDRY_DB_PATH", ROOT / "data" / "foundry.sqlite3"))
    log = DecisionLog(os.environ.get("FOUNDRY_DECISION_LOG", ROOT / "decision_log.json"))
    report = run_mock_delivery_test(
        MockDeliveryService(db_path), project_id="delivery-test", product_name="Sample digital product"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "payment_test_log.md").write_text(report, encoding="utf-8")
    passed = "FAIL" not in report
    log.append(
        action="mock_delivery_test", agent="delivery", actor="system", phase="launch",
        decision="Ran the MOCKED delivery test on its own (idempotent delivery, refund revokes "
                 "access). No payment took place.",
        outcome="recorded" if passed else "failed", sources=[], model=None, cost=0,
        project_id="delivery-test", metadata={"label": "MOCKED"},
    )
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
