"""web_kb: a batch pipeline that turns public web pages into a Bedrock knowledge base.

Stages:
    brightdata      scrape public URLs to Markdown (Web Unlocker, Crawl API)
    clean           prefer a site's own .md twin. Strip chrome that repeats across pages
    s3_loader       write clean .md documents + .metadata.json sidecars to S3
    knowledge_base  create the Bedrock KB + S3 data source, run ingestion jobs
    retrieve        query the KB (Retrieve / RetrieveAndGenerate)
    evaluate        measure retrieval quality against a golden set
    live_tool       hybrid routing: KB for the corpus, live Bright Data for the rest
"""

__all__ = ["brightdata", "clean", "s3_loader", "knowledge_base", "retrieve", "evaluate", "live_tool"]
