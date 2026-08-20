"""Hybrid retrieval: the knowledge base for the curated corpus, Bright Data for the rest.

A persistent knowledge base is a snapshot. It is excellent for a corpus you query repeatedly
and want cited, filtered, and fast. It is the wrong tool for two cases:

  - volatile facts (a price, a score, a status) that change faster than your sync cadence
  - long-tail questions about pages that are not in the corpus at all

Rather than choosing once, route per query. Ask the knowledge base first. If it cannot ground
the question, fall back to a live Bright Data lookup. `route_answer` implements exactly that
and reports which path served the query, so the decision is measurable rather than a guess.

Wire the live path into Bedrock as an agent action group, or expose it through AgentCore
Gateway so any MCP-compatible agent can call both paths. Note that Gateway's native
knowledge base target applies to Bedrock MANAGED knowledge bases. A customer-managed one
like this build sits behind a small Gateway tool that calls retrieve() instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

import requests

from . import brightdata
from . import retrieve as retrieve_mod

SERP_ENDPOINT = "https://api.brightdata.com/request"


def search(query: str, token: str, serp_zone: str, limit: int = 3) -> list[str]:
    """Find candidate pages for a query with Bright Data's SERP API. Returns result URLs."""
    resp = requests.post(
        SERP_ENDPOINT,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "zone": serp_zone,
            "format": "raw",
            # brd_json=1 asks Bright Data to parse the SERP into JSON. gl/hl pin the locale
            "url": f"https://www.google.com/search?q={quote_plus(query)}&brd_json=1&gl=us&hl=en",
        },
        timeout=60,
    )
    resp.raise_for_status()
    organic = resp.json().get("organic", []) or []
    return [r["link"] for r in organic[:limit] if r.get("link")]


def live_context(query: str, token: str, serp_zone: str, unlocker_zone: str,
                 pages: int = 2, char_budget: int = 6000) -> tuple[str, list[str]]:
    """Search, fetch the top pages as Markdown, and return (context, source_urls).

    This is the same Web Unlocker call the batch loader uses, just at query time. Note the
    difference from the batch path: nothing here strips site chrome, so the context is
    truncated raw Markdown. Chrome was roughly 43% of the characters on the measured corpus, so budget
    accordingly or run the pages through clean.strip_repeated first.
    """
    urls = search(query, token, serp_zone, limit=pages)
    parts, used = [], []
    for url in urls:
        markdown = brightdata._safe_scrape(url, token, unlocker_zone)
        if not markdown:
            continue
        parts.append(f"# Source: {url}\n\n{markdown[:char_budget // max(pages, 1)]}")
        used.append(url)
    return "\n\n---\n\n".join(parts), used


@dataclass
class RoutedAnswer:
    path: str              # "knowledge_base" or "live_web"
    context: str
    sources: list[str]
    top_score: float | None


def route_answer(knowledge_base_id: str, query: str, region: str, token: str,
                 serp_zone: str, unlocker_zone: str,
                 min_score: float = 0.4, num_results: int = 5) -> RoutedAnswer:
    """Try the knowledge base. Fall back to a live lookup when it cannot ground the query.

    min_score is the relevance floor. Bedrock returns a score per chunk. If the best chunk
    falls below the floor the corpus almost certainly does not cover the question, and
    answering from it would invent a citation. Tune the floor against your own eval set
    rather than trusting this default.
    """
    chunks = retrieve_mod.retrieve(knowledge_base_id, query, region, num_results=num_results)
    top_score = max((c.get("score", 0.0) for c in chunks), default=None)

    if chunks and top_score is not None and top_score >= min_score:
        context = "\n\n---\n\n".join(c["content"].get("text", "") for c in chunks)
        sources, seen = [], set()
        for c in chunks:
            url = c.get("metadata", {}).get("source_url")
            if url and url not in seen:
                seen.add(url)
                sources.append(url)
        return RoutedAnswer("knowledge_base", context, sources, top_score)

    context, sources = live_context(query, token, serp_zone, unlocker_zone)
    return RoutedAnswer("live_web", context, sources, top_score)
