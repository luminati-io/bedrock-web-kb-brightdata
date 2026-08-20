"""Write clean Markdown documents and their metadata sidecars to Amazon S3.

Bedrock reads each supported file in the bucket as a document, and reads an optional
`<file>.metadata.json` sidecar next to it for metadata. This module writes both, in the
shape a Bedrock knowledge base expects.

Two things here matter for cost and retrieval quality, and both are easy to get wrong:

1. Content hashing. Bedrock decides what to re-embed on an incremental sync from the data
   source's own change tracking, and AWS does not document which signal it reads for S3.
   A loader that blindly overwrites every page on every run therefore risks turning every
   sync into a FULL re-embed of the corpus. We store a content hash and skip writing when
   the page is byte-identical, so an unchanged page cannot trigger one either way.
2. Duplicate suppression, EXACT ONLY. Identical content under a second URL is skipped so it
   cannot crowd out diverse results at retrieval time. Note the limit: this is a hash of the
   normalized text, so it catches true mirrors and misses near-duplicates. Syndicated copies,
   print views, and paginated variants usually differ by a few bytes and will both be embedded.
   Catching those needs MinHash with locality-sensitive hashing over shingles, which is the
   standard for large corpus builds and the right upgrade when your sources overlap.

Metadata sidecar format:
  https://docs.aws.amazon.com/bedrock/latest/userguide/kb-metadata.html
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from . import dedup


def slugify(url: str) -> str:
    """Stable, safe object key derived from the URL, so a re-scrape overwrites in place."""
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    tail = url.rstrip("/").split("/")[-1][:40] or "page"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in tail)
    return f"{safe}-{digest}"


def content_hash(markdown: str) -> str:
    """Hash of the page's meaningful text.

    Normalizing whitespace stops different line breaks from reading as a content change and
    triggering a pointless re-embed.
    """
    normalized = re.sub(r"\s+", " ", markdown).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_metadata(url: str, title: str, section: str, chash: str,
                   scraped_date: int | None = None) -> dict:
    """Build the metadataAttributes payload for one document.

    includeForEmbedding=True adds the value to the embedded text (boosts relevance).
    False keeps it filter-only (e.g. a long URL that would only add noise to the vector).
    Types: STRING, NUMBER, BOOLEAN, STRING_LIST.

    scraped_date is the date the content last CHANGED, not the last time the page was
    fetched. An unchanged page skips the write (see put_document), so its date does not
    advance on a re-scrape that finds no change. It is a good content-age filter ("content
    that changed since X") but it does NOT tell you a page has stopped refreshing. For that
    liveness question use record_seen / stale_urls, which track every successful fetch.
    """
    if scraped_date is None:
        scraped_date = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    return {
        "metadataAttributes": {
            "source_url": {"value": {"type": "STRING", "stringValue": url},
                           "includeForEmbedding": False},
            "title": {"value": {"type": "STRING", "stringValue": title},
                      "includeForEmbedding": True},
            "section": {"value": {"type": "STRING", "stringValue": section},
                        "includeForEmbedding": True},
            "scraped_date": {"value": {"type": "NUMBER", "numberValue": scraped_date},
                             "includeForEmbedding": False},
            "content_hash": {"value": {"type": "STRING", "stringValue": chash[:16]},
                             "includeForEmbedding": False},
        }
    }


def stored_hash(bucket: str, key: str, s3_client) -> str | None:
    """Read the content hash we stored on the existing object, or None if absent."""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return head.get("Metadata", {}).get("content-hash")


def put_document(url: str, markdown: str, title: str, section: str,
                 bucket: str, prefix: str, s3_client=None,
                 change_key: str | None = None) -> tuple[str, str]:
    """Write one .md document + sidecar. Returns (key, status).

    status is "written" (new or changed) or "unchanged" (skipped, nothing re-embedded).

    change_key decides "changed" and is stored on the object for the next run to compare
    against. Pass the hash of the RAW scrape so that batch-relative cleaning (strip_repeated)
    cannot make an unchanged page look changed. Defaults to hashing `markdown` when omitted.
    """
    s3 = s3_client or boto3.client("s3")
    key = f"{prefix}/{slugify(url)}.md"
    chash = change_key or content_hash(markdown)

    if stored_hash(bucket, key, s3) == chash:
        return key, "unchanged"

    # Write the sidecar first, then the document. The document carries the content-hash the
    # next run compares against, so it is the commit point. If the process dies after the
    # sidecar but before the document, the document still holds the OLD hash, the change is
    # simply not recorded, and the next run rewrites both. The reverse order could leave a new
    # document beside a stale sidecar that never self-heals, because the hash already matches.
    s3.put_object(Bucket=bucket, Key=f"{key}.metadata.json",
                  Body=json.dumps(build_metadata(url, title, section, chash)).encode("utf-8"),
                  ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=key, Body=markdown.encode("utf-8"),
                  ContentType="text/markdown", Metadata={"content-hash": chash})
    return key, "written"


@dataclass
class LoadResult:
    """Per-run accounting, so a refresh reports what actually changed."""
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # (url, same_as_url, reason) where reason is "exact" or "near 0.91"
    duplicates: list[tuple[str, str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.written)} written, {len(self.unchanged)} unchanged, "
                f"{len(self.duplicates)} duplicate")

    def seen(self) -> list[str]:
        """Every URL successfully fetched this run: written, unchanged, or duplicate.

        This is the liveness set. A URL that is absent here (it is in `failed` instead) was
        not fetched, whether or not its content would have changed.
        """
        return self.written + self.unchanged + [url for url, _, _ in self.duplicates]


def put_documents(pages: dict[str, str], section: str, bucket: str, prefix: str,
                  s3_client=None, change_source: dict[str, str] | None = None,
                  near_dup_threshold: float | None = None) -> LoadResult:
    """Write a batch of {url: markdown}. Skips unchanged pages and duplicate content.

    `pages` is what gets written to S3 (already chrome-stripped). `change_source[url]` is
    what change-detection and dedup hash. Pass the RAW pre-clean text here. Because
    strip_repeated is batch-relative, hashing the cleaned text would let an unchanged page
    look changed whenever the batch composition shifts, re-embedding it for nothing. When
    change_source is None the written text is hashed, which is correct only when no
    batch-level cleaning happened.

    near_dup_threshold turns on MinHash near-duplicate detection over the CLEANED text, which
    catches the syndicated copies and print views an exact hash cannot. It is opt-in because
    it is lossy, so a threshold set too low discards pages that only look alike. Every drop is
    reported with the page it matched and the similarity, so a run can be audited.
    """
    s3 = s3_client or boto3.client("s3")
    result = LoadResult()
    seen: dict[str, str] = {}  # change-key hash -> first url that had it

    near: dict[str, tuple[str, float]] = {}
    if near_dup_threshold is not None:
        near = {url: (kept, score) for url, kept, score
                in dedup.find_near_duplicates(pages, threshold=near_dup_threshold)}

    for url, body in pages.items():
        source = (change_source or pages).get(url, body)
        chash = content_hash(source)
        if chash in seen:
            result.duplicates.append((url, seen[chash], "exact"))
            continue
        if url in near:
            kept, score = near[url]
            result.duplicates.append((url, kept, f"near {score:.2f}"))
            continue
        seen[chash] = url

        title = _first_heading(body) or url
        _, status = put_document(url, body, title, section, bucket, prefix, s3,
                                 change_key=chash)
        (result.written if status == "written" else result.unchanged).append(url)

    return result


def _iter_keys(bucket: str, prefix: str, s3_client) -> list[str]:
    """Every object key under `prefix/`, following pagination."""
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def reconcile(urls: list[str], bucket: str, prefix: str, s3_client=None,
              delete: bool = False) -> list[str]:
    """Find (and optionally delete) objects that no longer match any URL in `urls`.

    Returns the orphan keys. The loader only ever writes, so a URL removed from the list
    leaves its `.md` and sidecar in S3 and the knowledge base keeps serving them. Reconcile
    retires that content: once the objects are gone, the next ingestion sync drops the
    matching vectors.

    The diff is against the FULL intended URL list, not the successfully scraped subset, so
    a URL that failed to scrape this run is still expected and is never deleted. Only removing
    a URL from the list orphans its objects.

    Deletes only when delete=True, and refuses to delete against an empty list, because that
    would orphan the whole corpus and is far more likely a mistake than an intent.
    """
    if delete and not urls:
        raise ValueError(
            "reconcile(delete=True) with an empty URL list would delete the entire corpus. "
            "refusing. Pass the intended URL list."
        )
    s3 = s3_client or boto3.client("s3")

    expected: set[str] = set()
    for url in urls:
        base = f"{prefix}/{slugify(url)}.md"
        expected.add(base)
        expected.add(f"{base}.metadata.json")

    orphans = [k for k in _iter_keys(bucket, prefix, s3) if k not in expected]

    if delete and orphans:
        for i in range(0, len(orphans), 1000):  # delete_objects caps at 1000 keys
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in orphans[i:i + 1000]]},
            )
    return orphans


# Liveness ledger. Kept OUTSIDE the ingested prefix (the data source uses
# inclusionPrefixes=["docs/"]), so it never becomes a knowledge base document and updating
# it every run does not re-embed anything.
LAST_SEEN_KEY = "_state/last_seen.json"


def _load_last_seen(bucket: str, s3_client) -> dict[str, int]:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=LAST_SEEN_KEY)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return {}
        raise
    return json.loads(obj["Body"].read())


def record_seen(urls: list[str], bucket: str, today: int, s3_client=None) -> None:
    """Stamp `today` (an integer YYYYMMDD) as the last successful fetch for each URL.

    This is LIVENESS, deliberately separate from `scraped_date` (content freshness). It
    updates on every successful fetch whether or not the content changed, so a page that is
    fetched fine but never changes is not mistaken for one that has stopped refreshing.
    Assumes a single writer, which the scheduled refresh job is.
    """
    if not urls:
        return
    s3 = s3_client or boto3.client("s3")
    ledger = _load_last_seen(bucket, s3)
    for url in urls:
        ledger[url] = today
    s3.put_object(Bucket=bucket, Key=LAST_SEEN_KEY,
                  Body=json.dumps(ledger).encode("utf-8"),
                  ContentType="application/json")


def stale_urls(urls: list[str], bucket: str, before: int,
               s3_client=None) -> list[tuple[str, int | None]]:
    """URLs whose last successful fetch is older than `before` (YYYYMMDD), or never recorded.

    These are the pages that have stopped refreshing: still in your list, but not successfully
    fetched recently. This is the question `scraped_date` cannot answer, because a page can be
    fetched fine for months without its content changing. Returns (url, last_seen or None),
    never-seen first, then oldest first.
    """
    s3 = s3_client or boto3.client("s3")
    ledger = _load_last_seen(bucket, s3)
    out = [(url, ledger.get(url)) for url in urls
           if ledger.get(url) is None or ledger[url] < before]
    return sorted(out, key=lambda pair: (pair[1] is not None, pair[1] or 0))


def _first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()[:200]
    return None
