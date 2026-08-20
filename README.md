# Feed Amazon Bedrock Knowledge Bases with live web data using Bright Data

[![Promo](https://github.com/luminati-io/LinkedIn-Scraper/blob/main/Proxies%20and%20scrapers%20GitHub%20bonus%20banner.png)](https://brightdata.com/products/serp-api/google-search/hotels)

A batch pipeline that turns public web pages into a persistent, searchable Amazon Bedrock
knowledge base. Bright Data scrapes and cleans the pages, S3 is the handoff, and Bedrock
chunks, embeds, indexes, and answers queries with citations.

```
public URLs
  ─▶ Bright Data Web Unlocker        unblocks, geo-routes, renders JS when needed
  ─▶ S3 (.md + .metadata.json)       one clean doc + sidecar per page
  ─▶ Bedrock KB                      chunks, embeds, indexes (S3 Vectors)
  ─▶ Retrieve / RetrieveAndGenerate  cited answers
```

## Why this exists

Today, Bedrock's native Web Crawler data source supports static sites only, treats a missing
`robots.txt` as disallowed, caps at 25,000 pages, and requires OpenSearch Serverless.
Some public pages render client-side, sit behind bot protection, or need clean
extraction. Fetch those with Bright Data, and let Bedrock ingest the result from S3.

## Measured on the live pilot

6 documentation pages, us-east-2, Titan Text Embeddings V2, S3 Vectors store.

- Scrape latency per page 24.6 to 36.7 s, median 30.6 s. All of them in 43.1 s at 6 workers.
- Chrome stripping removed 21% of characters batch-wide and cut the repeated-line count from
  63 to 0.
- Ingestion took about 25 s. Retrieval hit@5 100%, MRR 0.88 on a 6-question golden set.
- A plain GET against a JS-only page (quotes.toscrape.com/js) yielded 96 chars of indexable
  text. Web Unlocker's fast path returned a 191-char shell. `render=True` returned the full
  page.

Treat these as one measured data point, not a benchmark. Reproduce them on your own targets.

## Layout

```
config.py                    settings from environment (.env)
src/web_kb/
  brightdata.py              Web Unlocker (URL -> Markdown, optional render) + Crawl API
  clean.py                   native .md twin preference + cross-page chrome stripping
  dedup.py                   MinHash + LSH near-duplicate detection (opt-in)
  s3_loader.py               docs + sidecars to S3: content hashing, dedup, reconcile, last-seen
  knowledge_base.py          create KB (S3 Vectors or OpenSearch) + data source + ingestion
  retrieve.py                Retrieve / RetrieveAndGenerate
  evaluate.py                retrieval quality against a golden set (hit@k, MRR, misses)
  live_tool.py               hybrid routing: KB first, live Bright Data lookup as fallback
scripts/
  1_scrape_to_s3.py          scrape a URL list -> S3 (incremental, reports failures)
  2_create_kb.py             create the knowledge base + S3 data source
  3_sync.py                  run an ingestion job and read its statistics
  4_query.py                 query the knowledge base (--section filter, --generate)
  5_evaluate.py              score retrieval against a golden set (--min-hit-rate CI gate)
  6_inspect_cleaning.py      audit what chrome stripping would remove, before trusting it
  7_reconcile.py             retire objects whose URL left the list (dry-run by default)
  8_render_check.py          does a target need JS rendering? plain vs fast path vs rendered
eval/
  golden.example.json        golden-set format: {question, expect: [source urls]}
  golden-v1-mislabeled.json  the first version, kept on purpose. See eval/README.md
lambda/
  normalize_crawl_delivery.py  S3-triggered: split Crawl API delivery into per-page docs
infra/
  terraform/                   provisions bucket, vector store, IAM role, KB (see its README)
  kb-service-role-trust.json   trust policy (bedrock.amazonaws.com)
  kb-service-role-policy.json  least-privilege permissions for the KB role
  operator-policy.json         permissions for the human or CI job that runs the scripts
```

## Prerequisites

- Python 3.10+.
- A [Bright Data](https://brightdata.com) account with an API token and a Web Unlocker zone.
- An AWS account with Amazon Bedrock and Titan Text Embeddings V2 enabled in your Region.
- An S3 bucket for the corpus, and a vector store (S3 Vectors by default).

**Check your embedding quota before anything else.** It's set per Region and can be zero on a
new account. A zero quota lets you create everything, then fails every ingestion job. One
account measured 0 requests/min in us-east-1 and 60 in us-east-2 on the same day.

```bash
aws service-quotas list-service-quotas --service-code bedrock --region YOUR_REGION \
  --query "Quotas[?contains(QuotaName,'Titan Text Embeddings V2') \
           && contains(QuotaName,'requests per minute')].[QuotaName,Value]" --output text
```

## Setup

1. **Install.**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env      # then fill it in. AWS_PROFILE works from here too
   ```

2. **Provision the AWS side.** The fastest path is Terraform. It creates the corpus bucket,
   the S3 Vectors store with the right non-filterable metadata keys, the service role, and
   optionally the knowledge base itself.
   ```bash
   cd infra/terraform && tofu init && tofu apply    # or terraform
   tofu output -raw env >> ../../.env               # then add your Bright Data token
   ```
   See `infra/terraform/README.md`. To do it by hand instead, create the KB service role from
   `infra/kb-service-role-trust.json` and `infra/kb-service-role-policy.json`. Replace the
   placeholder account ID, Region, and ARNs first, then put the role ARN in `.env`.

3. **Fill in `.env`** with the Bright Data token, bucket, Region, and ARNs. You don't need to
   create the S3 Vectors bucket or index by hand. `2_create_kb.py` creates them. It also
   declares `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` non-filterable. That setting
   decides whether Bedrock can ingest your documents at all. If an index already exists
   without it, the script refuses to continue and tells you why. The alternative is a sync
   that reports COMPLETE while Bedrock silently fails to embed most of the corpus. Pass
   `--skip-vector-store` if you provision it yourself.

## Run

```bash
# 0. Does your target even need scraping or rendering? Measure before you spend.
python scripts/8_render_check.py https://your-target/page --probe "text only on the rendered page"
python scripts/6_inspect_cleaning.py urls.txt          # audit what stripping would remove
python scripts/6_inspect_cleaning.py urls.txt --verify # and score what survived vs each .md twin

# 1. Scrape a URL list to clean Markdown in S3 (one .md + sidecar per page)
python scripts/1_scrape_to_s3.py urls.txt --section docs
python scripts/1_scrape_to_s3.py urls.txt --section docs --near-dup 0.85   # also drop near-dupes

# 2. Create the knowledge base and connect the bucket as an S3 data source
python scripts/2_create_kb.py --name web-kb --store s3vectors --chunking DEFAULT
#    creates the vector bucket and index with the right metadata config, then the KB
#    -> prints KNOWLEDGE_BASE_ID and DATA_SOURCE_ID, so paste both into .env

# 3. Ingest (sync). Re-run after every re-scrape. Syncs are incremental.
python scripts/3_sync.py

# 4. Query. Chunks by default, a written answer with --generate.
python scripts/4_query.py "what changed in pricing?" --section docs
python scripts/4_query.py "summarize the latest release" --generate

# 5. Measure retrieval BEFORE scaling (chunking is fixed when you create the data source)
python scripts/5_evaluate.py eval/golden.json --k 5
python scripts/5_evaluate.py eval/golden.json --min-hit-rate 0.8    # CI gate
```

Behaviors worth knowing in step 1:

- **Native Markdown twins are preferred.** Sites following the llms.txt convention serve
  `<page>.md`. When it exists it's cleaner, faster, and free, so the loader skips the scrape.
- **Chrome is stripped relative to the batch.** The loader drops lines that repeat on most
  pages. Audit with `6_inspect_cleaning.py` before trusting it on a new site. Score the
  result with `--verify`, which compares what survived against each page's own `.md` twin.
  Measured on this corpus: one line removed per page, the same llms.txt boilerplate every
  time, and no page-unique content lost.
- **Exact duplicates are always dropped, near-duplicates only if you ask.** The content hash
  catches byte-identical mirrors. On real sites that includes `?utm_source=` and `?print=1`
  aliases and trailing-slash variants, all of which returned the same bytes. Copies of the
  same document in a different form need `--near-dup`. It runs MinHash over the cleaned
  text and reports every page it drops with the one it matched. It's off by default because
  a wrong match loses a page. The report lists the closest pairs it kept, so you can set
  the threshold from evidence. Measured on this corpus: distinct pages reach at most 0.02,
  while a page and its `.md` twin score 0.75. The 0.85 default keeps both, and a 0.75
  threshold drops the twin.
- **Unchanged pages aren't rewritten.** Change detection hashes the raw scrape, not the
  stripped body. Bedrock skips a page that didn't change, even when it arrives in a
  different batch. Without this gate every sync re-embeds the whole corpus.
- **Empty and HTML 200s are failures.** A wrong zone name returns empty 200s, and so does a
  zone whose IP allowlist no longer matches your current address. The loader reports both
  responses instead of writing them to S3 as content.
- **Failed fetches are named, and the script exits non-zero**, because a page you can't
  re-scrape keeps serving its previous version as current.
- **The last-seen date is tracked separately from content age.** Every successful fetch
  records the date in a ledger outside the ingested prefix. `--stale-after DAYS` reports pages
  that stopped refreshing, which `scraped_date` can't tell you.

After step 3, **read the statistics, not the status**. An ingestion job reports `COMPLETE`
even when most documents failed. `3_sync.py` prints the counts and exits non-zero on failures,
and `failureReasons` on the job carries the actual error.

## Keeping it fresh

Re-run steps 1 and 3 on a schedule that matches how fast your sources change. The `slugify`
key is deterministic, so a re-scrape overwrites the same object and the incremental sync
re-embeds only the pages you rewrote. When URLs leave your list, run
`7_reconcile.py urls.txt --delete`, and the next sync retires their vectors. It diffs against
the full list, so it never mistakes a page that merely failed to scrape for one you removed.
In production, connect steps 1, 7, and 3 to an Amazon EventBridge schedule.

## Hybrid retrieval

A knowledge base is a snapshot, so it can't answer questions about fast-changing facts or
about pages outside the corpus. `src/web_kb/live_tool.py` routes per query. It asks the
knowledge base first. When the best chunk falls below a relevance floor, it runs a live
Bright Data search-and-fetch instead. Tune the floor against your golden set plus a few
questions the corpus doesn't cover. `5_evaluate.py` prints that band automatically: the
lowest score on a hit and the highest on a miss. Keep those extra questions in a separate
file, so they don't count against the `--min-hit-rate` gate. On the AWS side, connect the
live path as a Bedrock agent action group or an AgentCore Gateway tool. Gateway's native
knowledge base target applies to Bedrock managed knowledge bases. A customer-managed build
like this one sits behind a small Gateway tool that calls `Retrieve`.

## Scaling with the Crawl API

For whole-site coverage instead of a fixed URL list, `brightdata.start_crawl(...)` triggers a
crawl that discovers internal pages and returns Markdown records. Delivery is a separate
step: poll with `wait_for_crawl(...)` until the snapshot is `ready`, then call
`deliver_crawl(...)` to write the records to a landing prefix in S3.
Deploy `lambda/normalize_crawl_delivery.py` with an S3 trigger on that prefix. It splits
each record into a `<slug>.md` document and sidecar under the source prefix, and the next
sync ingests them. Prefer IAM-role delivery credentials over an access-key pair.

## Testing

Offline unit tests cover the pure logic and need no credentials:

```bash
python -m pytest tests/ -q      # 39 tests
```
