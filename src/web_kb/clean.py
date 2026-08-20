"""Strip site chrome from scraped Markdown before it reaches the knowledge base.

Measured on a real docs site, Web Unlocker's `data_format: "markdown"` returned pages that
were ~43% navigation and footer: the logo strip, "Skip to main content", the search box, the
entire sidebar nav tree, and a "Was this page helpful" footer. That is expected. Web Unlocker
solves ACCESS and converts HTML to Markdown. It does not decide which parts of a page are the
article. Deciding that is your job, and skipping it costs you twice:

  - every page carries a near-identical nav block, so you embed the same text N times and pay
    for it, and those chunks then compete with real content at retrieval time
  - the duplicate-suppression in s3_loader cannot help, because the pages are not identical,
    only their chrome is

`strip_repeated` removes it without any site-specific rules. Chrome is, by definition, the
text that repeats on every page, so lines appearing on most pages of a batch are dropped and
the content that makes each page unique survives.

Before scraping at all, call `native_markdown`. A growing number of documentation platforms
publish an LLM-ready Markdown twin of every page (the llms.txt convention). When it exists it
is cleaner, faster, and free, and it is the correct source to prefer.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import requests

# lines that are structural rather than content, never counted as boilerplate
# Bare fences and rules only. A language-tagged opening fence is not in this set, so on a
# code-heavy corpus one shared across pages can be dropped while its bare closing fence
# survives, leaving unbalanced Markdown. Add the tags you use before running it there.
_KEEP = {"", "```", "---"}


def native_markdown(url: str, timeout: int = 20) -> str | None:
    """Return the site's own Markdown twin of a page, if it publishes one.

    Follows the llms.txt convention used by Mintlify, Docusaurus and others, where
    `<page>.md` serves the same page as Markdown. It drops the site navigation but still
    carries the platform's own component tags and full image URLs, so it is cleaner than a
    scrape rather than clean. Returns None when unavailable, so callers can fall back to
    scraping.
    """
    try:
        resp = requests.get(url.rstrip("/") + ".md", timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    text = resp.text
    # a Markdown twin should not be an HTML page served with a 200
    if text.lstrip().lower().startswith("<!doctype") or "<html" in text[:400].lower():
        return None
    return text


def repeated_lines(pages: dict[str, str], threshold: float = 0.6,
                   min_pages: int = 3) -> dict[str, int]:
    """Return {line: page count} for every line judged to be chrome.

    Split out from `strip_repeated` so you can inspect what a run would remove before you
    trust it. Removing text from a corpus silently is not something to take on faith.
    """
    if len(pages) < min_pages:
        return {}

    counts: Counter[str] = Counter()
    for markdown in pages.values():
        for line in {ln.strip() for ln in markdown.splitlines()}:
            # short lines are included on purpose: a 2-character line that appears on most
            # pages ("⌘K", a stray ">") is chrome, and the repetition test is what makes
            # including them safe.
            if line not in _KEEP and len(line) >= 2:
                counts[line] += 1

    cutoff = max(2, int(len(pages) * threshold))
    return {line: n for line, n in counts.items() if n >= cutoff}


def strip_repeated(pages: dict[str, str], threshold: float = 0.6,
                   min_pages: int = 3) -> dict[str, str]:
    """Drop lines that appear on `threshold` or more of the pages in the batch.

    Needs at least `min_pages` pages to have any signal. Below that the input is returned
    unchanged rather than guessing.
    """
    boilerplate = set(repeated_lines(pages, threshold, min_pages))
    if not boilerplate:
        return dict(pages)

    cleaned: dict[str, str] = {}
    for url, markdown in pages.items():
        kept = [ln for ln in markdown.splitlines() if ln.strip() not in boilerplate]
        cleaned[url] = _collapse_blanks("\n".join(kept)).strip()
    return cleaned


@dataclass
class Fidelity:
    """How much real content survived cleaning, and how much chrome did not.

    `recall` and `lost` are the trustworthy signals. They only count units the raw scrape
    actually captured, so they measure the cleaner rather than the scraper, and they answer
    the question that matters, since stripping runs by default and deleted text is gone.
    Read `lost` first, because recall is a ratio over a small denominator on short pages and
    swings hard on a single line.

    `precision` is NOT a grade for the cleaner and is deliberately left as a count in
    `residue` rather than headlined. A scrape legitimately carries text the twin words
    differently or omits, so a page can look imprecise while the cleaner did nothing wrong.
    Measured here, precision ranged from 5% to 74% across 6 pages of one site while the
    cleaner lost the same single boilerplate line on every one of them. Use residue to see
    what kind of junk survives, not to judge whether the cleaning worked.
    """
    recall: float
    precision: float
    lost: list[str] = field(default_factory=list)      # reference content the cleaner removed
    residue: list[str] = field(default_factory=list)   # survived cleaning, absent from reference


def content_units(markdown: str, min_len: int = 25) -> set[str]:
    """Comparable units of meaning, with link syntax and emphasis normalized away."""
    units = set()
    for line in markdown.splitlines():
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)   # [label](url) -> label
        text = re.sub(r"[*_`>#|-]", " ", text)
        text = re.sub(r"\s+", " ", text).strip().lower()
        if len(text) >= min_len:
            units.add(text)
    return units


def fidelity(raw: str, cleaned: str, reference: str) -> Fidelity:
    """Score a cleaned page against a trusted version of the same page.

    The site's own Markdown twin is the reference to use when it exists, since it is that
    page without chrome by the publisher's own definition. Only units the raw scrape actually
    captured count against recall, so the score measures the cleaner rather than the scraper.
    """
    ref, got, before = content_units(reference), content_units(cleaned), content_units(raw)
    capturable = ref & before
    kept = capturable & got
    return Fidelity(
        recall=len(kept) / len(capturable) if capturable else 1.0,
        precision=len(kept) / len(got) if got else 1.0,
        lost=sorted(capturable - got),
        residue=sorted(got - ref),
    )


def trim_to_heading(markdown: str) -> str:
    """Fallback for a single page: drop everything before its first H1.

    Useful when you scrape one page at a time and `strip_repeated` has no batch to learn
    from. Returns the input unchanged when there is no H1.
    """
    for i, line in enumerate(markdown.splitlines()):
        if line.startswith("# "):
            return "\n".join(markdown.splitlines()[i:]).strip()
    return markdown


def _collapse_blanks(text: str) -> str:
    out, blank = [], 0
    for line in text.splitlines():
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out)
