#!/usr/bin/env python3
"""Show exactly what chrome stripping removes, before you let it near the corpus.

Usage:
    python scripts/6_inspect_cleaning.py urls.txt              # prefer native .md twins
    python scripts/6_inspect_cleaning.py urls.txt --no-native  # force the Web Unlocker path

Removing text from documents automatically deserves an audit. This prints the lines that
repeat across the batch, how many pages each appeared on, and the per-page size change, so
you can confirm that chrome stripping drops navigation and not content. Nothing is written to
S3 and nothing is ingested.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import brightdata, clean


def short(url: str, width: int = 46) -> str:
    tail = url.split("://", 1)[-1]
    return tail if len(tail) <= width else "..." + tail[-(width - 3):]


def main() -> None:
    p = argparse.ArgumentParser(description="Audit what chrome stripping removes.")
    p.add_argument("url_file")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--top", type=int, default=12, help="How many dropped lines to list.")
    p.add_argument("--no-native", action="store_true",
                   help="Skip the site's own .md twin and always scrape.")
    p.add_argument("--verify", action="store_true",
                   help="Score cleaning quality against each page's own .md twin, so you "
                        "learn whether real content survived rather than only how much went.")
    args = p.parse_args()

    try:
        s = Settings.load(need=("brightdata",))
    except MissingConfig as exc:
        sys.exit(str(exc))
    urls = [ln.strip() for ln in Path(args.url_file).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]

    pages: dict[str, str] = {}
    if not args.no_native:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for url, md in zip(urls, ex.map(clean.native_markdown, urls)):
                if md:
                    pages[url] = md
        print(f"native Markdown twin found for {len(pages)}/{len(urls)} URLs")

    to_scrape = [u for u in urls if u not in pages]
    if to_scrape:
        print(f"scraping {len(to_scrape)} via Web Unlocker (zone={s.unlocker_zone})...")
        scraped, failed = brightdata.scrape_many(
            to_scrape, s.brightdata_token, s.unlocker_zone, workers=args.workers)
        pages.update(scraped)
        if failed:
            print(f"  {len(failed)} failed, continuing with {len(scraped)}")

    if not pages:
        sys.exit("nothing fetched")

    dropped = clean.repeated_lines(pages)
    cleaned = clean.strip_repeated(pages)

    print(f"\nlines repeating on most of the {len(pages)} pages: {len(dropped)}\n")
    print(f"  {'pages':>5}   line")
    for line, n in sorted(dropped.items(), key=lambda kv: -kv[1])[:args.top]:
        text = line if len(line) <= 62 else line[:59] + "..."
        print(f"  {n:>5}   {text}")
    if len(dropped) > args.top:
        print(f"  {'':>5}   ... and {len(dropped) - args.top} more")

    print(f"\n  {'before':>8} {'after':>8} {'saved':>6}   page")
    for url in pages:
        b, a = len(pages[url]), len(cleaned[url])
        print(f"  {b:>8} {a:>8} {(b - a) / b:>5.0%}   {short(url)}")

    tb = sum(len(m) for m in pages.values())
    ta = sum(len(m) for m in cleaned.values())
    print(f"  {'-' * 24}")
    print(f"  {tb:>8} {ta:>8} {(tb - ta) / tb:>5.0%}   batch total")

    if args.verify:
        verify(pages, cleaned)


def verify(pages: dict[str, str], cleaned: dict[str, str]) -> None:
    """Score the cleaner against each page's own Markdown twin, where one exists.

    Size removed says nothing about whether the right text went. Recall is the number to
    watch, because stripping runs by default and deleted content cannot be recovered.
    """
    print("\nfidelity against each page's own .md twin")
    print("read the lost column first, since it is what the cleaner destroyed")
    print(f"\n  {'lost':>5} {'recall':>7} {'residue':>8}   page")
    scored, all_lost = [], []
    for url, raw_text in pages.items():
        reference = clean.native_markdown(url)
        if not reference:
            continue
        f = clean.fidelity(raw_text, cleaned[url], reference)
        scored.append(f)
        all_lost.extend(f.lost)
        print(f"  {len(f.lost):>5} {f.recall:>6.0%} {len(f.residue):>8}   {short(url)}")

    if not scored:
        print("  no page published a twin, so there is nothing to score against")
        return

    print(f"  {'-' * 32}")
    print(f"  {len(all_lost):>5} {sum(f.recall for f in scored) / len(scored):>6.0%}"
          f"           across {len(scored)} page(s)")

    distinct = sorted(set(all_lost))
    print(f"\n{len(distinct)} distinct line(s) removed that the twin considers content:")
    for line in distinct[:10]:
        hits = sum(1 for l in all_lost if l == line)
        print(f"  x{hits}  {line[:88]}")
    print("\nA line removed from every page is almost certainly boilerplate the twin happens to\n"
          "carry, which is the cleaner working. A line lost from one page is the one to inspect.")


if __name__ == "__main__":
    main()
