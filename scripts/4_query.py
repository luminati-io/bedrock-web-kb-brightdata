#!/usr/bin/env python3
"""Step 5: query the knowledge base.

Retrieve raw chunks:
    python scripts/4_query.py "what changed in pricing?" --section docs
Generate a grounded answer with citations:
    python scripts/4_query.py "what changed in pricing?" --generate
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import retrieve


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the web knowledge base.")
    parser.add_argument("query")
    parser.add_argument("--section", default=None)
    parser.add_argument("--since", type=int, default=None, help="YYYYMMDD lower bound.")
    parser.add_argument("--generate", action="store_true", help="Generate an answer (RAG).")
    args = parser.parse_args()

    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    if not s.knowledge_base_id:
        sys.exit("Set KNOWLEDGE_BASE_ID in .env.")

    if args.generate:
        if not s.generation_model_arn:
            sys.exit("Set GENERATION_MODEL_ARN in .env to generate answers.")
        resp = retrieve.answer(s.knowledge_base_id, args.query, s.generation_model_arn, s.aws_region)
        print(resp["output"]["text"], "\n")
        print("Sources:")
        for url in retrieve.format_citations(resp):
            print(f"  - {url}")
    else:
        results = retrieve.retrieve(
            s.knowledge_base_id, args.query, s.aws_region,
            section=args.section, since_date=args.since,
        )
        print(f"{len(results)} chunks:\n")
        for r in results:
            score = r.get("score")
            url = r.get("metadata", {}).get("source_url", "?")
            text = r["content"].get("text", "")[:280].replace("\n", " ")
            print(f"  [{score:.3f}] {url}\n    {text}...\n")


if __name__ == "__main__":
    main()
