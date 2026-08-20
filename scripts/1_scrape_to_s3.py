#!/usr/bin/env python3
"""Step 1-2: fetch a list of public URLs, clean them, and store them in S3.

Usage:
    python scripts/1_scrape_to_s3.py urls.txt --section docs
    python scripts/1_scrape_to_s3.py urls.txt --no-native --no-strip   # raw scrape only

Order of operations, cheapest correct source first:
  1. If the site publishes an LLM-ready Markdown twin (<page>.md, the llms.txt convention),
     use it. It is free, instant, and carries no navigation.
  2. Otherwise fetch through Bright Data Web Unlocker.
  3. Strip chrome that repeats across the batch, so navigation is not embedded once per page.
  4. Write to S3, skipping pages whose content has not changed.

Reports what a refresh needs to surface:
  written    content changed, and the next sync re-embeds these
  unchanged  byte-identical, skipped so the sync does not re-embed them
  FAILED     could not be fetched. The PREVIOUS version is still in S3 and the knowledge base
             will serve it as current. Exit code is non-zero so a scheduled run notices.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import brightdata, clean, dedup, s3_loader


def read_urls(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch URLs to clean Markdown in S3.")
    p.add_argument("url_file", help="Text file with one URL per line.")
    p.add_argument("--section", default="general", help="Metadata section label.")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--no-native", action="store_true",
                   help="Skip the site's own .md twin and always scrape.")
    p.add_argument("--no-strip", action="store_true",
                   help="Keep site chrome instead of removing repeated lines.")
    p.add_argument("--stale-after", type=int, metavar="DAYS",
                   help="After the run, report URLs not successfully fetched in this many "
                        "days (pages that have stopped refreshing).")
    p.add_argument("--near-dup", type=float, metavar="THRESHOLD", nargs="?", const=0.85,
                   help="Also drop near-duplicate pages at this MinHash similarity "
                        "(default 0.85 when the flag is given without a value). Catches "
                        "syndicated copies and print views that an exact hash misses.")
    args = p.parse_args()

    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    urls = read_urls(args.url_file)
    pages: dict[str, str] = {}
    native_hits = 0

    if not args.no_native:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for url, md in zip(urls, ex.map(clean.native_markdown, urls)):
                if md:
                    pages[url] = md
        native_hits = len(pages)
        print(f"native Markdown twin found for {native_hits}/{len(urls)} URLs")

    to_scrape = [u for u in urls if u not in pages]
    failed: list[str] = []
    if to_scrape:
        print(f"scraping {len(to_scrape)} via Bright Data Web Unlocker "
              f"(zone={s.unlocker_zone})...")
        scraped, failed = brightdata.scrape_many(
            to_scrape, s.brightdata_token, s.unlocker_zone, workers=args.workers)
        pages.update(scraped)
        print(f"  fetched {len(scraped)}/{len(to_scrape)}")

    # Snapshot the raw text before cleaning. Change-detection keys on this, not on the
    # chrome-stripped body, so that adding or losing a URL (which shifts what strip_repeated
    # removes from other pages) cannot make an unchanged page look changed and re-embed it.
    raw_pages = dict(pages)

    if not args.no_strip and pages:
        before = sum(len(m) for m in pages.values())
        pages = clean.strip_repeated(pages)
        after = sum(len(m) for m in pages.values())
        if before:
            print(f"stripped repeated chrome: {before} -> {after} chars "
                  f"({(before - after) / before:.0%} removed)")

    result = s3_loader.put_documents(pages, section=args.section,
                                     bucket=s.s3_bucket, prefix=s.s3_prefix,
                                     change_source=raw_pages,
                                     near_dup_threshold=args.near_dup)
    print(f"s3://{s.s3_bucket}/{s.s3_prefix}/ -> {result.summary()}")
    for url, same_as, reason in result.duplicates:
        print(f"  duplicate ({reason}), skipped: {url}\n      same as: {same_as}")

    # Show what sat just under the line, so a threshold can be tuned from evidence rather
    # than guessed. A real duplicate can score well below the default. The .md twin of a
    # page in this project's own corpus lands at 0.75.
    if args.near_dup is not None:
        dropped = {u for u, _, _ in result.duplicates}
        near_misses = [(a, b, sc) for a, b, sc in dedup.closest_pairs(pages, limit=3)
                       if sc < args.near_dup and a not in dropped and b not in dropped]
        if near_misses:
            print(f"\n  closest pairs still kept (below the {args.near_dup} threshold):")
            for a, b, sc in near_misses:
                print(f"    {sc:.2f}  {a}\n          {b}")

    # Record liveness for every URL fetched this run, changed or not. This is separate from
    # the scraped_date metadata, which only advances on a content change.
    now = datetime.now(timezone.utc)
    s3_loader.record_seen(result.seen(), s.s3_bucket, int(now.strftime("%Y%m%d")))

    if args.stale_after is not None:
        cutoff = int((now - timedelta(days=args.stale_after)).strftime("%Y%m%d"))
        stale = s3_loader.stale_urls(urls, s.s3_bucket, cutoff)
        if stale:
            print(f"\n{len(stale)} URL(s) not successfully fetched in {args.stale_after} day(s) "
                  f"(stopped refreshing):")
            for url, last in stale:
                print(f"  - {url}  (last seen: {last or 'never'})")

    if failed:
        print(f"\nWARNING: {len(failed)} page(s) failed. Their previous version is still in "
              f"S3 and will be served as current:")
        for url in failed:
            print(f"  - {url}")
        sys.exit(1)


if __name__ == "__main__":
    main()
