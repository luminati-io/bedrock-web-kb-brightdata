"""Query the knowledge base.

Retrieve returns raw source chunks with citations and metadata.
RetrieveAndGenerate returns a written answer grounded in those chunks, with citations.

Both use the bedrock-agent-runtime client. This module uses vectorSearchConfiguration,
which applies to a knowledge base you built with your own vector store (S3 Vectors or
OpenSearch Serverless). For a fully managed knowledge base, use managedSearchConfiguration
instead. See the note in retrieve().

Docs: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-retrieve.html
"""
from __future__ import annotations

import boto3


def _runtime(region: str):
    return boto3.client("bedrock-agent-runtime", region_name=region)


def retrieve(
    knowledge_base_id: str,
    query: str,
    region: str,
    section: str | None = None,
    since_date: int | None = None,
    num_results: int = 5,
) -> list[dict]:
    """Return the source chunks relevant to `query`, optionally filtered by metadata.

    section:    scope to one part of the corpus (exact match on the `section` attribute).
    since_date: keep only pages scraped on/after this YYYYMMDD integer.
    """
    vector_config: dict = {"numberOfResults": num_results}

    filters = []
    if section:
        filters.append({"equals": {"key": "section", "value": section}})
    if since_date:
        filters.append({"greaterThanOrEquals": {"key": "scraped_date", "value": since_date}})
    if len(filters) == 1:
        vector_config["filter"] = filters[0]
    elif len(filters) > 1:
        vector_config["filter"] = {"andAll": filters}

    resp = _runtime(region).retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_config},
        # Fully managed KB: replace the line above with
        #   retrievalConfiguration={"managedSearchConfiguration": {...}}
    )
    return resp["retrievalResults"]


def answer(
    knowledge_base_id: str,
    query: str,
    model_arn: str,
    region: str,
) -> dict:
    """Return a generated answer grounded in the knowledge base, with citations.

    The response holds resp["output"]["text"] and resp["citations"]. Each citation maps a
    span of the answer to the chunk it came from, whose metadata carries the source_url.
    """
    return _runtime(region).retrieve_and_generate(
        input={"text": query},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": knowledge_base_id,
                "modelArn": model_arn,
            },
        },
    )


def format_citations(rag_response: dict) -> list[str]:
    """Pull the distinct source URLs out of a retrieve_and_generate response."""
    urls: list[str] = []
    for citation in rag_response.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            attrs = ref.get("metadata", {})
            url = attrs.get("source_url")
            if url and url not in urls:
                urls.append(url)
    return urls
