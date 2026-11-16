#!/usr/bin/env python3
"""CLI verifier for DECISIONS.jsonl used by `make verify-log` and CI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledger.decision_log import DecisionLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    log_path = Path(args[0]) if args else Path("DECISIONS.jsonl")

    log = DecisionLog(log_path)
    result = log.verify()
    if result.valid:
        print(
            f"DECISION LOG VALID: {log_path} "
            f"({result.entry_count} entries, head={result.head_hash[:16]}...)"
        )
        return 0

    print(f"DECISION LOG INVALID: {log_path}", file=sys.stderr)
    for err in result.errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
