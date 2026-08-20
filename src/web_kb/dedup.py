"""Near-duplicate detection with MinHash and locality-sensitive hashing.

The content hash in `s3_loader` catches only byte-identical pages. Web corpora are mostly
NOT byte-identical: a syndicated copy carries a different byline, a print view drops the
nav, page 2 repeats the intro with a new date. Each of those embeds twice under an exact
hash, and the near-identical vectors then compete with each other at retrieval time.

MinHash estimates Jaccard similarity between two documents from small fixed-size signatures,
and LSH banding finds the candidate pairs without comparing every document to every other.
This is the standard approach for large corpus builds.

Run it on CLEANED text, not raw. Site chrome is near-identical across every page of a site,
so raw pages from one domain carry a similarity floor that has nothing to do with what they
say. Measured on the 6 documentation pages in this project, stripping chrome took mean
pairwise similarity from 0.070 to 0.010 and the maximum from 0.156 to 0.023. Neither figure
is near a sane threshold, so chrome alone did not cause a false positive here, but it is
sevenfold more noise for the detector to see past, and it grows with how heavy the chrome is.

Pick the threshold from your own corpus with `closest_pairs`, and do not assume the default
catches everything. Measured on this project's own pages, the same document reached two ways
(the HTML page and its llms.txt Markdown twin) scores 0.75 after chrome stripping, so a 0.85
default keeps both. Distinct pages in that same corpus top out at 0.02, so the gap is wide and
what matters is seeing which side of it your pairs fall on.

Read the threshold correctly, because it is not "percent of the text that matches". Measured
on an 810-word document with the defaults, prepending a byline or appending a 30-word footer
leaves similarity at 1.00, which is exactly the syndication and print-view case this exists
for. Changing one word in ten drops it to 0.41, and truncating to half the document gives
0.47. Every changed word breaks the five shingles containing it, so the score falls far faster
than the share of edited text. Treat 0.85 as "near-identical body text", and lower k if you
need to catch heavier rewrites.

Scope of this implementation. It is dependency-free and readable, which suits corpora up to
a few thousand pages (50 documents take about a second). Signature building is
O(pages * shingles * num_perm) in pure Python, so for tens of thousands of pages move to
`datasketch` or a numpy implementation, which does the same thing with vectorized permutations.
"""
from __future__ import annotations

import hashlib
import random
import re
from functools import lru_cache

_MERSENNE_PRIME = (1 << 61) - 1
_SEED = 42  # fixed, so signatures are reproducible across runs and machines


def shingles(text: str, k: int = 5) -> set[int]:
    """Hashed word-level k-shingles. Overlapping k-grams are what MinHash compares.

    Words are lowercased and stripped of punctuation so that formatting differences do not
    register as content differences. A document shorter than k words yields one shingle.
    """
    words = re.findall(r"\w+", text.lower())
    if not words:
        return set()
    if len(words) < k:
        return {_hash64(" ".join(words))}
    return {_hash64(" ".join(words[i:i + k])) for i in range(len(words) - k + 1)}


def minhash(shingle_set: set[int], num_perm: int = 128) -> tuple[int, ...]:
    """Signature of `num_perm` minima under independent hash permutations.

    The share of positions where two signatures agree estimates the Jaccard similarity of
    the underlying shingle sets, with standard error around 1/sqrt(num_perm).
    """
    perms = _permutations(num_perm)
    if not shingle_set:
        return tuple([_MERSENNE_PRIME] * num_perm)
    sig = [_MERSENNE_PRIME] * num_perm
    for h in shingle_set:
        for i, (a, b) in enumerate(perms):
            value = (a * h + b) % _MERSENNE_PRIME
            if value < sig[i]:
                sig[i] = value
    return tuple(sig)


def similarity(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    """Estimated Jaccard similarity, the fraction of signature positions that agree."""
    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0
    return sum(x == y for x, y in zip(sig_a, sig_b)) / len(sig_a)


def find_near_duplicates(pages: dict[str, str], threshold: float = 0.85,
                         num_perm: int = 128, bands: int = 16,
                         k: int = 5) -> list[tuple[str, str, float]]:
    """Return (url, duplicate_of, similarity) for pages judged near-duplicates.

    The first page in insertion order is kept and later matches are reported, so a stable
    URL list gives a stable decision about which copy survives.

    `bands` controls the LSH tradeoff. Two documents become candidates when any one band of
    `num_perm // bands` rows matches exactly, which makes the detector cheap but approximate,
    so every candidate is then verified against `threshold` before it is reported. More bands
    catch more pairs and cost more comparisons.
    """
    if len(pages) < 2 or num_perm % bands:
        return []

    signatures = {url: minhash(shingles(text, k), num_perm) for url, text in pages.items()}
    rows = num_perm // bands

    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
    for url, sig in signatures.items():
        for band in range(bands):
            key = (band, sig[band * rows:(band + 1) * rows])
            buckets.setdefault(key, []).append(url)

    candidates: set[tuple[str, str]] = set()
    for urls in buckets.values():
        if len(urls) > 1:
            for i, left in enumerate(urls):
                for right in urls[i + 1:]:
                    candidates.add((left, right) if left < right else (right, left))

    neighbours: dict[str, list[tuple[str, float]]] = {}
    for left, right in candidates:
        score = similarity(signatures[left], signatures[right])
        if score >= threshold:
            neighbours.setdefault(left, []).append((right, score))
            neighbours.setdefault(right, []).append((left, score))

    order = {url: i for i, url in enumerate(pages)}
    dropped: dict[str, tuple[str, float]] = {}
    for url in pages:
        if url in dropped:
            continue
        for other, score in neighbours.get(url, []):
            if other not in dropped and order[other] > order[url]:
                dropped[other] = (url, score)

    return sorted((url, kept, score) for url, (kept, score) in dropped.items())


def closest_pairs(pages: dict[str, str], limit: int = 5, num_perm: int = 128,
                  k: int = 5) -> list[tuple[str, str, float]]:
    """The most similar page pairs in the batch, whatever the threshold.

    Pick a threshold from your own data rather than from a default. Run this first, look at
    where the scores cluster, and set the cut between the pairs you consider the same document
    and the pairs you do not. On the 6 documentation pages in this project, distinct pages
    top out around 0.02 while the same page fetched two ways reaches 0.75, so the gap is wide
    and the exact threshold matters less than knowing which side of it your pairs land on.
    """
    if len(pages) < 2:
        return []
    signatures = {url: minhash(shingles(text, k), num_perm) for url, text in pages.items()}
    urls = list(pages)
    scored = [(similarity(signatures[a], signatures[b]), a, b)
              for i, a in enumerate(urls) for b in urls[i + 1:]]
    scored.sort(reverse=True)
    return [(a, b, score) for score, a, b in scored[:limit]]


def _hash64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


@lru_cache(maxsize=8)
def _permutations(num_perm: int) -> tuple[tuple[int, int], ...]:
    rnd = random.Random(_SEED)
    return tuple((rnd.randrange(1, _MERSENNE_PRIME), rnd.randrange(0, _MERSENNE_PRIME))
                 for _ in range(num_perm))
