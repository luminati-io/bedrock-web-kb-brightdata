#!/usr/bin/env python3
"""Step 6: measure whether the knowledge base retrieves the right pages.

Run this on a small corpus BEFORE you commit to a full one. The chunking strategy is fixed
when the data source is created, so a bad choice can only be fixed by recreating the
knowledge base. Ten good golden questions will tell you more than ten thousand more pages.

Usage:
    python scripts/5_evaluate.py eval/golden.example.json
    python scripts/5_evaluate.py eval/golden.json --k 10 --section docs
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import evaluate as ev


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate knowledge base retrieval.")
    parser.add_argument("golden", help="JSON file of {question, expect} objects.")
    parser.add_argument("--k", type=int, default=5, help="Chunks to retrieve per question.")
    parser.add_argument("--section", default=None, help="Restrict to one metadata section.")
    parser.add_argument("--min-hit-rate", type=float, default=0.0,
                        help="Exit non-zero below this hit rate (for CI).")
    args = parser.parse_args()

    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    if not s.knowledge_base_id:
        sys.exit("Set KNOWLEDGE_BASE_ID in .env.")

    golden = ev.load_golden(args.golden)
    report = ev.evaluate(s.knowledge_base_id, golden, s.aws_region,
                         k=args.k, section=args.section)
    print(report.render())

    if report.hit_rate < args.min_hit_rate:
        print(f"\nhit@{args.k} {report.hit_rate:.0%} is below the required "
              f"{args.min_hit_rate:.0%}")
        sys.exit(1)


if __name__ == "__main__":
    main()
