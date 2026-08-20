"""S3-triggered Lambda: normalize Bright Data Crawl API delivery into KB source documents.

The Crawl API delivers records to S3 as JSON or NDJSON (one record per crawled page, with
the custom_output_fields you requested: url, page_title, markdown). Bedrock's S3 data source
wants one document per page plus a metadata sidecar. This function bridges the two.

Trigger: S3 ObjectCreated on the LANDING prefix (where Bright Data delivers).
Effect:  writes <slug>.md + <slug>.md.metadata.json under the SOURCE prefix that the
         knowledge base ingests.

Environment:
    SOURCE_BUCKET   bucket the knowledge base reads (often the same bucket)
    SOURCE_PREFIX   prefix the knowledge base reads (e.g. "docs")
    SECTION         metadata section label for crawled pages (default "crawl")

This function is self-contained (boto3 only) so it packages without the src/ tree.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3

s3 = boto3.client("s3")

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "docs")
SECTION = os.environ.get("SECTION", "crawl")


def _slugify(url: str) -> str:
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    tail = url.rstrip("/").split("/")[-1][:40] or "page"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in tail)
    return f"{safe}-{digest}"


def _iter_records(body: bytes):
    """Yield records from either a JSON array or NDJSON body."""
    text = body.decode("utf-8").strip()
    if not text:
        return
    if text[0] == "[":
        yield from json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)


def _write_document(record: dict) -> str | None:
    url = record.get("url") or record.get("page_url")
    markdown = record.get("markdown") or record.get("content")
    if not (url and markdown):
        return None
    title = record.get("page_title") or record.get("title") or url
    slug = _slugify(url)
    key = f"{SOURCE_PREFIX}/{slug}.md"

    s3.put_object(Bucket=SOURCE_BUCKET, Key=key,
                  Body=markdown.encode("utf-8"), ContentType="text/markdown")

    metadata = {
        "metadataAttributes": {
            "source_url": {"value": {"type": "STRING", "stringValue": url},
                           "includeForEmbedding": False},
            "title": {"value": {"type": "STRING", "stringValue": title[:200]},
                      "includeForEmbedding": True},
            "section": {"value": {"type": "STRING", "stringValue": SECTION},
                        "includeForEmbedding": True},
            "scraped_date": {"value": {"type": "NUMBER",
                                       "numberValue": int(datetime.now(timezone.utc).strftime("%Y%m%d"))},
                             "includeForEmbedding": False},
        }
    }
    s3.put_object(Bucket=SOURCE_BUCKET, Key=f"{key}.metadata.json",
                  Body=json.dumps(metadata).encode("utf-8"),
                  ContentType="application/json")
    return key


def handler(event, context):
    written = 0
    for rec in event.get("Records", []):
        src_bucket = rec["s3"]["bucket"]["name"]
        src_key = unquote_plus(rec["s3"]["object"]["key"])
        obj = s3.get_object(Bucket=src_bucket, Key=src_key)
        for record in _iter_records(obj["Body"].read()):
            if _write_document(record):
                written += 1
    print(json.dumps({"message": "normalized", "documents_written": written}))
    return {"documents_written": written}
