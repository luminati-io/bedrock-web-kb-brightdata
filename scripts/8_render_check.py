#!/usr/bin/env python3
"""Does this page need JavaScript rendering to yield its content? Measure, don't guess.

Usage:
    python scripts/8_render_check.py https://quotes.toscrape.com/js/ --probe "The world"

Runs the same URL three ways and reports what each yields as indexable text:
  1. plain GET with a browser User-Agent, scripts and tags stripped
  2. Web Unlocker on its default fast path (no forced browser)
  3. Web Unlocker with render forced

If a scrape comes back suspiciously thin, this is the first thing to run. A page whose
content only exists after client-side rendering shows near-empty results on paths 1 and 2
and the real content on path 3, which means the loader needs render=True for that target.
Treat that as a correctness switch rather than a latency penalty, since the rendered
request is not reliably slower. Server-rendered pages show full content on the cheap
paths, so leave render off for them. With --probe, exits non-zero if even the rendered fetch lacks
the probe text, so a scheduled check can alarm.
"""
import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import brightdata

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def visible_text(html: str) -> str:
    """What a non-rendering fetcher actually yields for indexing: tags and scripts gone."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def report(label: str, text: str, seconds: float, probe: str | None) -> bool:
    hit = probe in text if probe else None
    probe_msg = "" if probe is None else f"   probe: {'FOUND' if hit else 'absent'}"
    print(f"  {label:<28} {len(text):>8,} chars   {seconds:>5.1f}s{probe_msg}")
    if "Web Unlocker" in label and not text.strip():
        print("    ^ EMPTY response from Web Unlocker. That is a zone-side rejection, not an"
              " empty page. Usual causes, in order: the zone's IP allowlist no longer matches"
              " your current address, a wrong zone name, or a revoked token. Check the zone's"
              " Access details in the Bright Data control panel.")
    return bool(hit)


def main() -> None:
    p = argparse.ArgumentParser(description="Compare plain, fast-path, and rendered fetches.")
    p.add_argument("url")
    p.add_argument("--probe", help="Text that should appear once the page has rendered.")
    args = p.parse_args()

    try:
        s = Settings.load(need=("brightdata",))
    except MissingConfig as exc:
        sys.exit(str(exc))
    print(f"target: {args.url}\n")

    t0 = time.time()
    req = urllib.request.Request(args.url, headers={"User-Agent": BROWSER_UA})
    plain = visible_text(urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace"))
    report("plain GET, visible text", plain, time.time() - t0, args.probe)

    t0 = time.time()
    fast = brightdata.scrape_markdown(args.url, s.brightdata_token, s.unlocker_zone)
    report("Web Unlocker, fast path", fast, time.time() - t0, args.probe)

    t0 = time.time()
    rendered = brightdata.scrape_markdown(args.url, s.brightdata_token, s.unlocker_zone,
                                          render=True)
    hit = report("Web Unlocker, render=True", rendered, time.time() - t0, args.probe)

    if args.probe and not hit:
        sys.exit("probe text absent even with rendering. The content may need interaction "
                 "or a different tool (Browser API)")


if __name__ == "__main__":
    main()
