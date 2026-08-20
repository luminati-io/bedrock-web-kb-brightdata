"""Offline unit tests for the pure logic. No AWS or Bright Data credentials required.

    python -m pytest tests/ -q
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web_kb import s3_loader, knowledge_base as kb, evaluate as ev, clean, brightdata, dedup

os.environ.setdefault("SOURCE_BUCKET", "test-bucket")
os.environ.setdefault("SOURCE_PREFIX", "docs")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
import normalize_crawl_delivery as ncd  # noqa: E402


class FakeS3:
    """Minimal S3 stand-in: remembers objects and their content-hash metadata."""

    def __init__(self):
        self.objects = {}
        self.writes = []  # keys in the order they were written

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
        self.objects[Key] = {"Body": Body, "Metadata": Metadata or {}}
        self.writes.append(Key)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": self.objects[Key]["Metadata"]}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        objects = self.objects

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}

        return _Paginator()

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {"Deleted": Delete["Objects"]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io
        body = self.objects[Key]["Body"]
        return {"Body": io.BytesIO(body if isinstance(body, bytes) else body.encode())}


# ---------- keys and metadata ----------

def test_slugify_is_stable_and_safe():
    url = "https://example.com/docs/getting-started/"
    assert s3_loader.slugify(url) == s3_loader.slugify(url)
    assert "/" not in s3_loader.slugify(url)
    assert s3_loader.slugify(url).startswith("getting-started-")


def test_metadata_shape_matches_bedrock_contract():
    meta = s3_loader.build_metadata("https://a.com/p", "Title", "docs", "abc123", 20260712)
    attrs = meta["metadataAttributes"]
    assert set(attrs) == {"source_url", "title", "section", "scraped_date", "content_hash"}
    assert attrs["source_url"]["value"] == {"type": "STRING", "stringValue": "https://a.com/p"}
    assert attrs["source_url"]["includeForEmbedding"] is False
    assert attrs["title"]["includeForEmbedding"] is True
    assert attrs["scraped_date"]["value"] == {"type": "NUMBER", "numberValue": 20260712}


# ---------- content hashing / incremental sync ----------

def test_content_hash_ignores_whitespace_reflow():
    a = s3_loader.content_hash("# Title\n\nSome   body text.")
    b = s3_loader.content_hash("# Title\nSome body text.")
    assert a == b, "cosmetic reflow must not look like a content change"


def test_content_hash_detects_real_change():
    assert s3_loader.content_hash("# A") != s3_loader.content_hash("# B")


def test_unchanged_page_is_not_rewritten():
    """The whole point: an unchanged page must not be re-uploaded, or Bedrock re-embeds it."""
    fake = FakeS3()
    args = ("https://a.com/p", "# Hello\n\nbody", "Hello", "docs", "b", "docs", fake)
    assert s3_loader.put_document(*args)[1] == "written"
    assert s3_loader.put_document(*args)[1] == "unchanged"
    assert s3_loader.put_document("https://a.com/p", "# Hello\n\nCHANGED",
                                  "Hello", "docs", "b", "docs", fake)[1] == "written"


def test_put_document_writes_sidecar_before_doc():
    """Crash safety (#7): the doc carries the content-hash and must be the commit point, so
    the sidecar is written first. A crash between the two then leaves the change unrecorded
    and the next run redoes both, instead of stranding a new doc beside a stale sidecar.
    """
    fake = FakeS3()
    s3_loader.put_document("https://a.com/p", "# P\n\nbody", "P", "docs", "b", "docs", fake)
    md = f"docs/{s3_loader.slugify('https://a.com/p')}.md"
    sidecar = f"{md}.metadata.json"
    assert fake.writes.index(sidecar) < fake.writes.index(md)


def test_wait_for_crawl_times_out_instead_of_looping_forever(monkeypatch):
    """#5: a crawl stuck non-terminal must raise, not spin forever."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "running"}  # never terminal

    monkeypatch.setattr(brightdata.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(brightdata.time, "sleep", lambda *a, **k: None)
    with pytest.raises(TimeoutError):
        brightdata.wait_for_crawl("snap", "tok", poll_seconds=0, timeout_seconds=0)


def test_last_seen_tracks_liveness_not_content_change():
    """The #3 fix: a page fetched fine but unchanged must NOT look stale, while a page that
    stopped being fetched successfully must. scraped_date can't tell these apart. last_seen can.
    """
    fake = FakeS3()
    s3_loader.record_seen(["https://a.com/x", "https://a.com/y"], "b", 20260101, fake)
    # later, only x is still fetched successfully (y began failing every run)
    s3_loader.record_seen(["https://a.com/x"], "b", 20260110, fake)

    stale = s3_loader.stale_urls(["https://a.com/x", "https://a.com/y", "https://a.com/z"],
                                 "b", before=20260105, s3_client=fake)
    urls = [u for u, _ in stale]
    assert "https://a.com/y" in urls        # last fetched 20260101, before cutoff -> stale
    assert "https://a.com/z" in urls        # never fetched -> stale
    assert "https://a.com/x" not in urls    # fetched 20260110 -> fresh
    assert stale[0][0] == "https://a.com/z"  # never-seen sorts first


def test_load_result_seen_covers_written_unchanged_and_duplicates():
    r = s3_loader.LoadResult(written=["a"], unchanged=["b"], duplicates=[("c", "a", "exact")])
    assert set(r.seen()) == {"a", "b", "c"}


def test_reconcile_retires_removed_urls_and_their_sidecars():
    """A URL dropped from the list must have its .md AND sidecar retired. Kept URLs survive."""
    fake = FakeS3()
    s3_loader.put_documents({"https://a.com/keep": "# Keep\n\nbody",
                             "https://a.com/gone": "# Gone\n\nbody"},
                            "docs", "b", "docs", fake)
    keep = f"docs/{s3_loader.slugify('https://a.com/keep')}.md"
    gone = f"docs/{s3_loader.slugify('https://a.com/gone')}.md"

    urls = ["https://a.com/keep"]  # /gone was removed from the list
    orphans = s3_loader.reconcile(urls, "b", "docs", fake, delete=False)
    assert gone in orphans and f"{gone}.metadata.json" in orphans
    assert keep not in orphans and fake.objects.get(gone) is not None  # dry run deletes nothing

    s3_loader.reconcile(urls, "b", "docs", fake, delete=True)
    assert gone not in fake.objects and f"{gone}.metadata.json" not in fake.objects
    assert keep in fake.objects and f"{keep}.metadata.json" in fake.objects


def test_reconcile_never_deletes_a_url_that_only_failed_to_scrape():
    """The critical safety property: a URL still in the list but not written this run (it
    failed to scrape) must be preserved, not mistaken for a removed URL and deleted."""
    fake = FakeS3()
    s3_loader.put_documents({"https://a.com/p": "# P\n\nbody"}, "docs", "b", "docs", fake)
    # /p failed to scrape this run so was not re-written, but is STILL in the list
    urls = ["https://a.com/p", "https://a.com/newly-added"]
    assert s3_loader.reconcile(urls, "b", "docs", fake, delete=True) == []
    assert f"docs/{s3_loader.slugify('https://a.com/p')}.md" in fake.objects


def test_reconcile_refuses_to_delete_against_empty_list():
    fake = FakeS3()
    s3_loader.put_documents({"https://a.com/p": "# P\n\nb"}, "docs", "b", "docs", fake)
    with pytest.raises(ValueError):
        s3_loader.reconcile([], "b", "docs", fake, delete=True)
    assert f"docs/{s3_loader.slugify('https://a.com/p')}.md" in fake.objects  # untouched


def test_change_source_decouples_cleaning_from_change_detection():
    """The bug this guards against: batch-relative chrome stripping changes the cleaned body
    of a page whose real content did not change, which would re-embed it for nothing. When
    change-detection keys on the raw source, a different cleaned body alone must NOT re-embed.
    """
    fake = FakeS3()
    raw = {"https://a.com/p": "RAW source text for the page, unchanged between runs"}

    first = s3_loader.put_documents({"https://a.com/p": "cleaned body v1"},
                                    "docs", "b", "docs", fake, change_source=raw)
    assert first.written == ["https://a.com/p"]

    # Different written body, identical raw source -> unchanged, nothing re-embedded.
    second = s3_loader.put_documents({"https://a.com/p": "cleaned body v2 DIFFERENT"},
                                     "docs", "b", "docs", fake, change_source=raw)
    assert second.unchanged == ["https://a.com/p"]
    assert second.written == []

    # A real change in the raw source DOES re-embed.
    third = s3_loader.put_documents({"https://a.com/p": "cleaned body v3"}, "docs", "b", "docs",
                                    fake, change_source={"https://a.com/p": "RAW source CHANGED"})
    assert third.written == ["https://a.com/p"]


def test_scrape_markdown_render_flag_is_opt_in(monkeypatch):
    """render=True must add the documented body parameter. The default must not."""
    sent = []

    class _Resp:
        text = "# a real page with enough body text to pass"
        def raise_for_status(self): pass

    monkeypatch.setattr(brightdata.requests, "post",
                        lambda url, headers, json, timeout: (sent.append(json), _Resp())[1])
    brightdata.scrape_markdown("https://a.com", "tok", "zone")
    brightdata.scrape_markdown("https://a.com", "tok", "zone", render=True)
    assert "render" not in sent[0]
    assert sent[1]["render"] == "true"


def test_scrape_content_guard_rejects_empty_and_html():
    """A misconfigured zone returns empty or HTML 200s. Those must not pass as page content."""
    assert brightdata._looks_like_content("# Real page\n\nreal body text that is long enough")
    assert not brightdata._looks_like_content("")
    assert not brightdata._looks_like_content("   \n  \t ")
    assert not brightdata._looks_like_content("tiny")  # under the length floor
    assert not brightdata._looks_like_content("<!DOCTYPE html><html><body>blocked</body></html>")
    assert not brightdata._looks_like_content("<html><head><title>Access denied</title></head>")


# ---------- near-duplicate detection ----------

_ARTICLE = (
    "Amazon Bedrock Knowledge Bases gives you managed retrieval augmented generation. "
    "You point it at a data source and it chunks your documents, embeds them, writes the "
    "vectors to a store, and answers queries with citations. The built in connectors read "
    "from where enterprise data already sits, including object storage and wikis. There is "
    "also a native web crawler connector intended for public pages you are authorized to use."
)


def test_minhash_estimates_similarity_and_is_deterministic():
    a = dedup.minhash(dedup.shingles(_ARTICLE))
    b = dedup.minhash(dedup.shingles(_ARTICLE))
    assert a == b, "signatures must be reproducible across calls"
    assert dedup.similarity(a, b) == 1.0
    other = dedup.minhash(dedup.shingles("completely unrelated text about gardening tools"))
    assert dedup.similarity(a, other) < 0.2


def test_near_duplicate_is_caught_where_exact_hashing_provably_fails():
    """The whole point. A syndicated copy differs by a byline, so the content hashes differ
    and exact dedup keeps both, while MinHash sees them as the same document."""
    original = _ARTICLE
    syndicated = "By a staff writer, republished March 2026.\n\n" + _ARTICLE

    assert s3_loader.content_hash(original) != s3_loader.content_hash(syndicated)

    pairs = dedup.find_near_duplicates({"https://a.com/post": original,
                                        "https://b.com/post": syndicated}, threshold=0.85)
    assert len(pairs) == 1
    url, kept, score = pairs[0]
    assert url == "https://b.com/post" and kept == "https://a.com/post"
    assert score >= 0.85


def test_closest_pairs_ranks_and_surfaces_sub_threshold_matches():
    """A real duplicate can sit under the default. The audit view has to show it anyway."""
    pages = {"a": _ARTICLE,
             "b": _ARTICLE + " " + " ".join(f"extra{i}" for i in range(120)),
             "c": "Postgres autovacuum falls behind on busy tables and dead tuples pile up."}
    top = dedup.closest_pairs(pages, limit=3)
    assert {top[0][0], top[0][1]} == {"a", "b"}, "the related pair must rank first"
    assert top[0][2] > dedup.closest_pairs(pages, limit=3)[-1][2], "results are ranked"
    assert dedup.find_near_duplicates(pages, threshold=0.95) == [], "and it is under 0.95"


def test_distinct_pages_are_not_flagged():
    pages = {
        "u1": _ARTICLE,
        "u2": "Postgres vacuum settings matter when autovacuum falls behind on a busy table "
              "and dead tuples accumulate faster than the daemon can reclaim them.",
    }
    assert dedup.find_near_duplicates(pages, threshold=0.85) == []


def test_first_seen_page_is_the_one_kept():
    pages = {"first": _ARTICLE, "second": _ARTICLE + " A trailing sentence was added here."}
    pairs = dedup.find_near_duplicates(pages, threshold=0.8)
    assert [p[0] for p in pairs] == ["second"]


def test_loader_drops_near_duplicates_only_when_opted_in():
    syndicated = "By a staff writer.\n\n" + _ARTICLE
    pages = {"https://a.com/x": _ARTICLE, "https://b.com/x": syndicated}

    off = s3_loader.put_documents(pages, "docs", "b", "docs", FakeS3())
    assert len(off.written) == 2 and off.duplicates == []

    on = s3_loader.put_documents(pages, "docs", "b", "docs", FakeS3(),
                                 near_dup_threshold=0.85)
    assert len(on.written) == 1
    assert len(on.duplicates) == 1
    url, kept, reason = on.duplicates[0]
    assert url == "https://b.com/x" and kept == "https://a.com/x"
    assert reason.startswith("near ")
    assert set(on.seen()) == set(pages)   # a duplicate was still fetched, so it stays live


def test_duplicate_content_across_urls_is_skipped():
    fake = FakeS3()
    pages = {
        "https://a.com/x": "# Same\n\nidentical body",
        "https://a.com/y": "# Same\n\nidentical body",   # syndicated copy
        "https://a.com/z": "# Different\n\nother body",
    }
    res = s3_loader.put_documents(pages, "docs", "b", "docs", fake)
    assert len(res.written) == 2
    assert len(res.duplicates) == 1
    assert res.duplicates[0][0] == "https://a.com/y"


# ---------- chunking config ----------

def test_chunking_config_variants():
    assert kb._chunking_config("DEFAULT") is None
    assert kb._chunking_config("NONE")["chunkingConfiguration"]["chunkingStrategy"] == "NONE"
    fixed = kb._chunking_config("FIXED_SIZE")["chunkingConfiguration"]
    assert fixed["fixedSizeChunkingConfiguration"]["maxTokens"] == 300


# ---------- retrieval evaluation ----------

def _qr(rank):
    return ev.QuestionResult("q", ["https://a.com/p"], rank, [])


def test_eval_metrics():
    report = ev.EvalReport([_qr(1), _qr(2), _qr(None), _qr(None)], k=5)
    assert report.hit_rate == 0.5
    assert abs(report.mrr - (1.0 + 0.5) / 4) < 1e-9
    assert len(report.misses) == 2


def _scored(rank, score):
    return ev.QuestionResult("q", ["https://a.com/p"], rank, [], score)


def test_floor_band_brackets_the_routing_floor():
    # the floor belongs above the best miss and below the worst hit
    report = ev.EvalReport([_scored(1, 0.71), _scored(4, 0.55), _scored(None, 0.31)], k=5)
    assert report.floor_band == (0.55, 0.31)


def test_floor_band_reports_no_lower_bound_when_nothing_missed():
    # a golden set with every question covered can only bound the floor from above, which is
    # why tuning needs questions the corpus deliberately does not cover
    report = ev.EvalReport([_scored(1, 0.71), _scored(2, 0.62)], k=5)
    assert report.floor_band == (0.62, None)
    assert "add a few questions the corpus does not cover" in report.render()


def test_eval_report_lists_misses_so_they_are_actionable():
    out = ev.EvalReport([_qr(None)], k=5).render()
    assert "missed" in out and "cannot be grounded" in out


# ---------- crawl delivery normalizer ----------

def test_crawl_delivery_parses_json_array_and_ndjson():
    array_body = b'[{"url": "https://a.com/1", "markdown": "# A"}]'
    ndjson_body = b'{"url": "https://a.com/1", "markdown": "# A"}\n{"url": "https://a.com/2", "markdown": "# B"}\n'
    assert len(list(ncd._iter_records(array_body))) == 1
    assert len(list(ncd._iter_records(ndjson_body))) == 2
    assert list(ncd._iter_records(b"")) == []


def test_crawl_record_needs_url_and_markdown(monkeypatch):
    calls = []
    monkeypatch.setattr(ncd.s3, "put_object", lambda **kw: calls.append(kw))
    assert ncd._write_document({"url": "https://a.com/1", "markdown": "# A"}) is not None
    assert ncd._write_document({"url": "https://a.com/2"}) is None
    assert len(calls) == 2


# ---------- boilerplate stripping ----------

def test_strip_repeated_removes_cross_page_chrome():
    nav = "Skip to main content\nSign up\nSearch..."
    pages = {
        "u1": f"{nav}\n# Page One\nunique body one",
        "u2": f"{nav}\n# Page Two\nunique body two",
        "u3": f"{nav}\n# Page Three\nunique body three",
    }
    out = clean.strip_repeated(pages)
    for md in out.values():
        assert "Skip to main content" not in md
        assert "Sign up" not in md
    assert "unique body one" in out["u1"]
    assert "# Page One" in out["u1"]


def test_fidelity_flags_destroyed_content_and_ignores_scraper_gaps():
    """Recall must blame the cleaner only for text the scrape actually had, so a page the
    scraper never captured does not read as a cleaning failure."""
    reference = ("The knowledge base answers queries with citations from your corpus.\n"
                 "Chunking strategy cannot be changed after the data source is created.\n"
                 "A paragraph the scraper never managed to capture at all here.")
    raw = ("Skip to main content and other navigation furniture goes here\n"
           "The knowledge base answers queries with citations from your corpus.\n"
           "Chunking strategy cannot be changed after the data source is created.")

    perfect = clean.fidelity(raw, raw.split("\n", 1)[1], reference)
    assert perfect.recall == 1.0 and perfect.lost == []

    over_eager = clean.fidelity(raw, "The knowledge base answers queries with citations "
                                     "from your corpus.", reference)
    assert over_eager.recall == 0.5
    assert len(over_eager.lost) == 1 and "chunking strategy" in over_eager.lost[0]


def test_fidelity_reports_surviving_chrome_as_residue():
    reference = "The real body sentence of this documentation page lives here."
    raw = "Was this page helpful and other footer text\n" + reference
    f = clean.fidelity(raw, raw, reference)          # nothing stripped at all
    assert f.recall == 1.0, "no content was destroyed"
    assert any("helpful" in r for r in f.residue), "but the footer survived"


def test_strip_repeated_needs_a_batch_to_learn_from():
    pages = {"u1": "Sign up\n# Only page\nbody"}
    assert clean.strip_repeated(pages) == pages   # too few pages, returned unchanged


def test_trim_to_heading_drops_preamble():
    md = "logo\nSign up\n# Real Title\nbody text"
    assert clean.trim_to_heading(md).startswith("# Real Title")
    assert clean.trim_to_heading("no heading here") == "no heading here"


# ---------- vector index guard (the 5-of-6-documents-fail trap) ----------

class _FakeS3Vectors:
    """Stands in for the s3vectors client, including its exception classes."""

    class exceptions:
        class ConflictException(Exception): pass
        class NotFoundException(Exception): pass

    def __init__(self, index=None):
        self.index, self.created = index, []

    def create_vector_bucket(self, vectorBucketName):
        raise self.exceptions.ConflictException()          # already there, the common case

    def get_index(self, vectorBucketName, indexName):
        if self.index is None:
            raise self.exceptions.NotFoundException()
        return {"index": self.index}

    def create_index(self, **kw):
        self.created.append(kw)


def _patch(monkeypatch, fake):
    monkeypatch.setattr(kb.boto3, "client", lambda *a, **k: fake)


def test_missing_index_is_created_with_the_keys_that_matter(monkeypatch):
    fake = _FakeS3Vectors(index=None)
    _patch(monkeypatch, fake)
    assert kb.ensure_vector_index("bkt", "web-kb-index", "us-east-2") == "created"
    sent = fake.created[0]
    assert sent["metadataConfiguration"]["nonFilterableMetadataKeys"] == kb.NON_FILTERABLE_KEYS
    assert "AMAZON_BEDROCK_TEXT" in kb.NON_FILTERABLE_KEYS
    assert sent["dimension"] == 1024 and sent["dataType"] == "float32"


def test_existing_index_missing_the_keys_is_refused_before_ingestion(monkeypatch):
    """The whole point. Catching this now beats a sync that reports COMPLETE while
    most of the corpus silently fails to embed."""
    _patch(monkeypatch, _FakeS3Vectors(index={"dimension": 1024,
                                              "metadataConfiguration": {"nonFilterableMetadataKeys": []}}))
    with pytest.raises(RuntimeError, match="AMAZON_BEDROCK_TEXT"):
        kb.ensure_vector_index("bkt", "web-kb-index", "us-east-2")


def test_existing_index_with_the_opensearch_key_name_is_also_refused(monkeypatch):
    """AMAZON_BEDROCK_TEXT_CHUNK is the OpenSearch field name and does nothing here."""
    _patch(monkeypatch, _FakeS3Vectors(index={"dimension": 1024,
        "metadataConfiguration": {"nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT_CHUNK"]}}))
    with pytest.raises(RuntimeError, match="AMAZON_BEDROCK_TEXT"):
        kb.ensure_vector_index("bkt", "web-kb-index", "us-east-2")


def test_dimension_mismatch_is_refused(monkeypatch):
    _patch(monkeypatch, _FakeS3Vectors(index={"dimension": 256,
        "metadataConfiguration": {"nonFilterableMetadataKeys": kb.NON_FILTERABLE_KEYS}}))
    with pytest.raises(RuntimeError, match="dimension"):
        kb.ensure_vector_index("bkt", "web-kb-index", "us-east-2", dimension=1024)


def test_correctly_configured_index_passes(monkeypatch):
    _patch(monkeypatch, _FakeS3Vectors(index={"dimension": 1024,
        "metadataConfiguration": {"nonFilterableMetadataKeys": kb.NON_FILTERABLE_KEYS}}))
    assert kb.ensure_vector_index("bkt", "web-kb-index", "us-east-2") == "exists"
