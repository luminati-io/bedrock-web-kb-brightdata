#!/usr/bin/env python3
"""Retire content whose URL was removed from the list.

The loader only ever writes, so deleting a URL from your list leaves its `.md` and sidecar
in S3, and the knowledge base keeps serving them as if they were current. This reconciles
the bucket against the current list: it reports objects that no longer match any URL, and
deletes them with --delete. Once the objects are gone, the next ingestion job
(scripts/3_sync.py) drops their vectors.

    python scripts/7_reconcile.py urls.txt            # report orphans, delete nothing
    python scripts/7_reconcile.py urls.txt --delete   # remove them, then run 3_sync.py

Safety:
  - The diff is against the FULL URL list, so a URL that merely failed to scrape is never
    treated as removed. Only deleting it from the list orphans its objects.
  - Refuses to run against an empty list, which would orphan the whole corpus.
  - Without --delete it only reports, so you can see what would go before it goes.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import s3_loader


def read_urls(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def main() -> None:
    p = argparse.ArgumentParser(description="Retire S3 objects whose URL left the list.")
    p.add_argument("url_file", help="The current, complete URL list.")
    p.add_argument("--delete", action="store_true",
                   help="Actually delete the orphaned objects. Without it, only report.")
    args = p.parse_args()

    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    urls = read_urls(args.url_file)
    if not urls:
        sys.exit("URL list is empty. Refusing to reconcile (this would orphan everything).")

    orphans = s3_loader.reconcile(urls, s.s3_bucket, s.s3_prefix, delete=args.delete)
    location = f"s3://{s.s3_bucket}/{s.s3_prefix}/"
    if not orphans:
        print(f"in sync: every object under {location} matches the {len(urls)}-URL list")
        return

    docs = sorted(k for k in orphans if not k.endswith(".metadata.json"))
    verb = "deleted" if args.delete else "orphaned (re-run with --delete to remove)"
    print(f"{len(docs)} document(s) and their sidecars {verb}:")
    for key in docs[:30]:
        print(f"  - {key}")
    if len(docs) > 30:
        print(f"  ... and {len(docs) - 30} more")

    if args.delete:
        print("\nNow run scripts/3_sync.py so the knowledge base drops their vectors.")
    else:
        sys.exit(1)  # non-zero so a scheduled check notices drift


if __name__ == "__main__":
    main()
