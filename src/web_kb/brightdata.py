"""Bright Data web access: Web Unlocker (single URL -> Markdown) and the Crawl API.

Web Unlocker is synchronous, one request per URL. The Crawl API is asynchronous and can
deliver output straight to S3. Both authenticate with a Bearer token and run against a zone.

Docs:
  https://docs.brightdata.com/scraping-automation/web-unlocker/features
  https://docs.brightdata.com/scraping-automation/crawl-api/overview
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import requests

UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
TRIGGER_ENDPOINT = "https://api.brightdata.com/datasets/v3/trigger"
PROGRESS_ENDPOINT = "https://api.brightdata.com/datasets/v3/progress"
DELIVER_ENDPOINT = "https://api.brightdata.com/datasets/v3/deliver"


def scrape_markdown(url: str, token: str, zone: str, timeout: int = 120,
                    render: bool = False) -> str:
    """Fetch a public URL as clean, LLM-ready Markdown via Bright Data Web Unlocker.

    render=True forces browser rendering. Web Unlocker decides per request how much
    machinery a page needs, and a page it can fetch without executing JavaScript comes
    back on the fast path without a browser. Content that exists only after client-side
    rendering (measured: quotes.toscrape.com/js returns a 191-char shell on the fast path
    and the full page with render on) needs this flag. Treat it as a correctness switch
    rather than a latency penalty: on that one page the rendered request came back in
    7.7s against the fast path's 17.1s. Leave it off where the fast path already returns
    the content. See scripts/8_render_check.py to test a target.
    """
    payload = {
        "zone": zone,
        "url": url,
        "format": "raw",            # return the response body directly
        "data_format": "markdown",  # convert the page to Markdown
    }
    if render:
        payload["render"] = "true"  # force a browser. Use only when content is JS-only
    resp = requests.post(
        UNLOCKER_ENDPOINT,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _looks_like_content(text: str) -> bool:
    """Reject the empty or HTML-error 200s a misconfigured request returns.

    Web Unlocker with data_format=markdown should return page text. An empty body, or a
    raw HTML document instead of Markdown, means the request did not do what we asked: a
    wrong zone name returns empty 200s, and a block page returns HTML. Writing either to S3
    would poison retrieval, so it is treated as a failed scrape rather than as content.
    """
    stripped = text.strip()
    if len(stripped) < 20:
        return False
    head = stripped[:200].lower()
    return not (head.startswith("<!doctype") or head.startswith("<html"))


def _safe_scrape(url: str, token: str, zone: str, retries: int = 2,
                 render: bool = False) -> str | None:
    """Scrape one URL, retrying transient failures. Return None if it never succeeds.

    A 200 whose body is empty or an HTML error page is treated as a failure too, not
    silently written as if it were the page. See `_looks_like_content`.
    """
    for attempt in range(retries + 1):
        try:
            text = scrape_markdown(url, token, zone, render=render)
            if _looks_like_content(text):
                return text
        except requests.RequestException:
            pass
        if attempt == retries:
            return None
        time.sleep(2 ** attempt)  # 1s, 2s backoff
    return None


def scrape_many(
    urls: list[str], token: str, zone: str, workers: int = 8, render: bool = False
) -> tuple[dict[str, str], list[str]]:
    """Scrape a batch of URLs. Returns ({url: markdown}, [urls that failed]).

    Failures are returned rather than silently dropped. That matters: a page that fails to
    re-scrape leaves its PREVIOUS version sitting in S3, so the knowledge base keeps serving
    stale content as if it were current. The caller has to see the failures to react.

    Keep `workers` modest. The free and pay-as-you-go tiers meter request rate.
    """
    out: dict[str, str] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda u: _safe_scrape(u, token, zone, render=render), urls)
        for url, markdown in zip(urls, results):
            if markdown:
                out[url] = markdown
            else:
                failed.append(url)
    return out, failed


def start_crawl(seed_url: str, token: str, dataset_id: str) -> str:
    """Trigger an async crawl that discovers a site's internal pages and returns Markdown.

    Returns the snapshot_id. Delivery is a SEPARATE call — see deliver_crawl. The
    /datasets/v3/trigger reference documents neither a `deliver` field nor an `input`
    wrapper: the body is a bare array of input objects.
      https://docs.brightdata.com/api-reference/rest-api/scraper/crawl-api
    """
    resp = requests.post(
        TRIGGER_ENDPOINT,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "dataset_id": dataset_id,
            "custom_output_fields": "url|page_title|markdown",
        },
        json=[{"url": seed_url}],
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["snapshot_id"]


def deliver_crawl(
    snapshot_id: str,
    token: str,
    deliver_bucket: str,
    deliver_directory: str,
    aws_region: str,
    role_arn: str | None = None,
    external_id: str | None = None,
    aws_access_key: str | None = None,
    aws_secret_key: str | None = None,
) -> str:
    """Deliver a ready crawl snapshot to S3. Returns the delivery job ID.

    The snapshot must already be in `ready` status, so call wait_for_crawl first. Poll the
    returned job at GET /datasets/v3/delivery/{id} until its status is "done".

    Prefer role-based delivery (role_arn + external_id) over long-lived access keys. This
    path was assembled from the documentation and not executed. Confirm the delivery body
    against the live reference before you depend on it.
    """
    if role_arn and external_id:
        credentials = {"role_arn": role_arn, "external_id": external_id}
    elif aws_access_key and aws_secret_key:
        credentials = {"aws-access-key": aws_access_key, "aws-secret-key": aws_secret_key}
    else:
        raise ValueError("Provide either (role_arn, external_id) or an access-key pair.")

    resp = requests.post(
        f"{DELIVER_ENDPOINT}/{snapshot_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "s3",
            "bucket": deliver_bucket,
            "directory": deliver_directory,
            "region": aws_region,
            "credentials": credentials,
            "filename": {"template": "crawl", "extension": "json"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_crawl(snapshot_id: str, token: str, poll_seconds: int = 15,
                   timeout_seconds: int = 3600) -> str:
    """Poll a crawl snapshot until it is ready or failed. Returns the final status.

    Gives up with a TimeoutError after timeout_seconds rather than polling forever, so a
    crawl that never reaches a terminal state cannot hang the caller indefinitely.
    """
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_seconds
    while True:
        resp = requests.get(f"{PROGRESS_ENDPOINT}/{snapshot_id}", headers=headers, timeout=30)
        resp.raise_for_status()
        status = resp.json().get("status", "unknown")
        if status in ("ready", "failed"):
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"crawl {snapshot_id} still '{status}' after {timeout_seconds}s")
        time.sleep(poll_seconds)
