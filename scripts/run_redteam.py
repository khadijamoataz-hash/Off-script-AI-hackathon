#!/usr/bin/env python3
"""Print the live Red-Team pass/fail Markdown table and verify 100% pass rate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from policy.redteam import format_redteam_markdown_table, run_redteam_suite  # noqa: E402


def main() -> int:
    results = run_redteam_suite()
    print(format_redteam_markdown_table(results))
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\nRed-Team Summary: {passed}/{total} cases passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
