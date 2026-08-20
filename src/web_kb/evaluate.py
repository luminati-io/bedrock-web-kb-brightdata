"""Measure whether the knowledge base actually retrieves the right pages.

Most RAG pipelines ship without this, which is why they quietly underperform. It matters
more than usual on a Bedrock knowledge base because the chunking strategy is fixed when the
data source is created: if chunking is wrong for your pages, the only fix is to recreate the
knowledge base. Run this on a small golden set BEFORE you commit to a full corpus.

A golden set is a list of questions with the source URL(s) that ought to answer each one:

    [
      {"question": "What does the Pro plan cost?",
       "expect": ["https://example.com/pricing"]},
      {"question": "How do I rotate an API key?",
       "expect": ["https://example.com/docs/keys"]}
    ]

Metrics reported:
  hit@k  fraction of questions where an expected page appears in the top k chunks.
         This is the number that decides whether the answer CAN be grounded at all.
  MRR    mean reciprocal rank of the first expected page (1.0 = always ranked first).
         Sensitive to ordering, so it catches "right page, buried at rank 8".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import retrieve as retrieve_mod


@dataclass
class QuestionResult:
    question: str
    expect: list[str]
    hit_rank: int | None          # 1-based rank of first expected page, None if absent
    retrieved: list[str]          # source URLs actually returned, in rank order
    top_score: float | None = None   # best relevance score returned, for tuning the relevance floor

    @property
    def hit(self) -> bool:
        return self.hit_rank is not None


@dataclass
class EvalReport:
    results: list[QuestionResult]
    k: int

    @property
    def hit_rate(self) -> float:
        return sum(r.hit for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(1.0 / r.hit_rank if r.hit_rank else 0.0 for r in self.results) / len(self.results)

    @property
    def misses(self) -> list[QuestionResult]:
        return [r for r in self.results if not r.hit]

    @property
    def floor_band(self) -> tuple[float | None, float | None]:
        """(lowest score on a hit, highest score on a miss).

        The relevance floor in `live_tool.route_answer` belongs between these two. A set with
        no misses gives you only the upper bound, which is why tuning needs a few questions
        the corpus deliberately does not cover.
        """
        hit = [r.top_score for r in self.results if r.hit and r.top_score is not None]
        miss = [r.top_score for r in self.results if not r.hit and r.top_score is not None]
        return (min(hit) if hit else None, max(miss) if miss else None)

    def render(self) -> str:
        lines = [
            f"questions: {len(self.results)}   k={self.k}",
            f"hit@{self.k}: {self.hit_rate:.0%}",
            f"MRR:     {self.mrr:.2f}",
        ]

        lo_hit, hi_miss = self.floor_band
        lines.append("")
        lines.append("relevance floor, from the scores this run saw:")
        lines.append(f"  lowest score on a hit:   "
                     f"{f'{lo_hit:.2f}' if lo_hit is not None else 'n/a'}")
        if hi_miss is not None:
            lines.append(f"  highest score on a miss: {hi_miss:.2f}")
            if lo_hit is not None:
                lines.append(f"  so the floor belongs between {hi_miss:.2f} and {lo_hit:.2f}")
        else:
            lines.append("  highest score on a miss: n/a, every question hit")
            lines.append("  add a few questions the corpus does not cover to get the lower "
                         "bound, in a separate file so they do not count against --min-hit-rate")

        if self.misses:
            lines.append("")
            lines.append(f"missed ({len(self.misses)}) - these cannot be grounded today:")
            for r in self.misses:
                lines.append(f"  - {r.question}")
                lines.append(f"      expected: {', '.join(r.expect)}")
                lines.append(f"      got:      {', '.join(r.retrieved[:3]) or '(nothing)'}")
                if r.top_score is not None:
                    lines.append(f"      top score: {r.top_score:.2f}")
        return "\n".join(lines)


def load_golden(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("golden set must be a JSON list of {question, expect} objects")
    return data


def _normalize(url: str) -> str:
    return url.rstrip("/").split("?")[0].lower()


def evaluate(knowledge_base_id: str, golden: list[dict], region: str,
             k: int = 5, section: str | None = None) -> EvalReport:
    """Run every golden question through Retrieve and score where the expected page appears."""
    results: list[QuestionResult] = []

    for item in golden:
        question = item["question"]
        expect = [_normalize(u) for u in item.get("expect", [])]

        chunks = retrieve_mod.retrieve(knowledge_base_id, question, region,
                                       section=section, num_results=k)
        got = [_normalize(c.get("metadata", {}).get("source_url", "")) for c in chunks]

        hit_rank = next((i + 1 for i, u in enumerate(got) if u and u in expect), None)
        top_score = max((c.get("score", 0.0) for c in chunks), default=None)
        results.append(QuestionResult(question, item.get("expect", []), hit_rank, got, top_score))

    return EvalReport(results, k)
