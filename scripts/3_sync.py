#!/usr/bin/env python3
"""Step 4: ingest (sync) the S3 data source into the knowledge base.

Run this after the first load, and again after every re-scrape. Syncs after the first are
incremental: only new and changed documents are re-embedded, and vectors for removed
documents are deleted.

Usage:
    python scripts/3_sync.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import knowledge_base as kb


def main() -> None:
    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    if not (s.knowledge_base_id and s.data_source_id):
        sys.exit("Set KNOWLEDGE_BASE_ID and DATA_SOURCE_ID in .env (from scripts/2_create_kb.py).")

    print(f"Starting ingestion job for KB {s.knowledge_base_id}...")
    job_id = kb.start_sync(s.knowledge_base_id, s.data_source_id, s.aws_region)
    print(f"  job {job_id} started, polling until complete...")

    job = kb.wait_for_sync(s.knowledge_base_id, s.data_source_id, job_id, s.aws_region)
    stats = job.get("statistics", {})
    print(f"  status: {job['status']}")
    if stats:
        print(f"  scanned={stats.get('numberOfDocumentsScanned')} "
              f"new={stats.get('numberOfNewDocumentsIndexed')} "
              f"modified={stats.get('numberOfModifiedDocumentsIndexed')} "
              f"deleted={stats.get('numberOfDocumentsDeleted')} "
              f"failed={stats.get('numberOfDocumentsFailed')}")
    # A job reports COMPLETE even when most of its documents failed, so the document
    # count has to gate the exit code too. Without this a scheduled run stays green
    # while the corpus quietly stops being indexed.
    failed = stats.get("numberOfDocumentsFailed") or 0
    if job["status"] == "FAILED" or failed:
        print("  failureReasons:", job.get("failureReasons"))
        sys.exit(1)


if __name__ == "__main__":
    main()
