"""Create and operate the Amazon Bedrock knowledge base over the S3 corpus.

Covers both vector-store paths (Amazon S3 Vectors, the low-cost default, and Amazon
OpenSearch Serverless), the S3 data source, and ingestion jobs.

Field shapes verified against the Bedrock Agent API reference:
  CreateKnowledgeBase: storageConfiguration.type in {S3_VECTORS, OPENSEARCH_SERVERLESS, ...}
  CreateDataSource:    dataSourceConfiguration.type = S3, s3Configuration{bucketArn, inclusionPrefixes}

Prerequisite: the vector store (an S3 vector bucket + index, or an OpenSearch Serverless
collection + index) and the KB service role must already exist. See infra/ and the README.
"""
from __future__ import annotations

import time

import boto3


def _client(region: str):
    return boto3.client("bedrock-agent", region_name=region)


# Bedrock stores the chunk text and its own bookkeeping as vector metadata. S3 Vectors caps
# FILTERABLE metadata at 2048 bytes per vector, so unless these two keys are declared
# non-filterable at index creation, anything but a very short chunk fails to ingest. Measured
# on a 6-page corpus, 5 of 6 documents failed and the survivor had a 499-byte chunk.
# The key is AMAZON_BEDROCK_TEXT. AMAZON_BEDROCK_TEXT_CHUNK is the OpenSearch field name and
# silently does nothing here. None of this can be changed after the index exists.
NON_FILTERABLE_KEYS = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]


def ensure_vector_index(vector_bucket_name: str, index_name: str, region: str,
                        dimension: int = 1024, create_bucket: bool = True) -> str:
    """Create the S3 Vectors bucket and index correctly, or verify an existing one.

    Returns "created", "exists", or raises if an existing index is configured in a way that
    will fail at ingestion. Checking now is the whole point, because the alternative is a
    sync that reports COMPLETE while most of the corpus quietly fails to embed.
    """
    s3v = boto3.client("s3vectors", region_name=region)

    if create_bucket:
        try:
            s3v.create_vector_bucket(vectorBucketName=vector_bucket_name)
            print(f"  created vector bucket {vector_bucket_name}")
        except s3v.exceptions.ConflictException:
            pass

    try:
        existing = s3v.get_index(vectorBucketName=vector_bucket_name, indexName=index_name)
    except s3v.exceptions.NotFoundException:
        existing = None

    if existing:
        index = existing["index"]
        declared = set(index.get("metadataConfiguration", {}).get("nonFilterableMetadataKeys", []))
        missing = [k for k in NON_FILTERABLE_KEYS if k not in declared]
        if missing:
            raise RuntimeError(
                f"Index {index_name} already exists but does not declare {', '.join(missing)} "
                f"as non-filterable metadata. Ingestion will fail for all but very short "
                f"chunks, and this cannot be changed after creation. Delete the index and "
                f"re-run, or point S3_VECTOR_INDEX_NAME at a new one."
            )
        if index.get("dimension") != dimension:
            raise RuntimeError(
                f"Index {index_name} has dimension {index.get('dimension')} but the embedding "
                f"model produces {dimension}. Ingestion fails at write time on a mismatch."
            )
        return "exists"

    s3v.create_index(
        vectorBucketName=vector_bucket_name,
        indexName=index_name,
        dataType="float32",
        dimension=dimension,
        distanceMetric="cosine",
        metadataConfiguration={"nonFilterableMetadataKeys": NON_FILTERABLE_KEYS},
    )
    print(f"  created index {index_name} (dimension {dimension}, "
          f"non-filterable {', '.join(NON_FILTERABLE_KEYS)})")
    return "created"


def create_kb_s3_vectors(
    name: str,
    role_arn: str,
    embedding_model_arn: str,
    vector_bucket_arn: str,
    index_name: str,
    region: str,
    description: str = "Public web corpus scraped with Bright Data",
) -> str:
    """Create a knowledge base backed by Amazon S3 Vectors. Returns the knowledgeBaseId."""
    resp = _client(region).create_knowledge_base(
        name=name,
        description=description,
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": embedding_model_arn},
        },
        storageConfiguration={
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "vectorBucketArn": vector_bucket_arn,
                "indexName": index_name,
            },
        },
    )
    return resp["knowledgeBase"]["knowledgeBaseId"]


def create_kb_opensearch(
    name: str,
    role_arn: str,
    embedding_model_arn: str,
    collection_arn: str,
    index_name: str,
    region: str,
    description: str = "Public web corpus scraped with Bright Data",
) -> str:
    """Create a knowledge base backed by OpenSearch Serverless. Returns the knowledgeBaseId."""
    resp = _client(region).create_knowledge_base(
        name=name,
        description=description,
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": embedding_model_arn},
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": index_name,
                "fieldMapping": {
                    "vectorField": "bedrock-knowledge-base-default-vector",
                    "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                    "metadataField": "AMAZON_BEDROCK_METADATA",
                },
            },
        },
    )
    return resp["knowledgeBase"]["knowledgeBaseId"]


def create_s3_data_source(
    knowledge_base_id: str,
    bucket_arn: str,
    prefix: str,
    region: str,
    name: str = "web-corpus",
    chunking: str = "DEFAULT",
) -> str:
    """Connect the S3 bucket/prefix as a data source. Returns the dataSourceId.

    chunking: DEFAULT (~300 tokens) | FIXED_SIZE | HIERARCHICAL | SEMANTIC | NONE.
    You cannot change the chunking strategy after the data source is created.
    """
    data_source_config = {
        "type": "S3",
        "s3Configuration": {
            "bucketArn": bucket_arn,
            "inclusionPrefixes": [f"{prefix}/"],
        },
    }
    kwargs = {
        "knowledgeBaseId": knowledge_base_id,
        "name": name,
        "dataSourceConfiguration": data_source_config,
        "dataDeletionPolicy": "DELETE",
    }
    ingestion = _chunking_config(chunking)
    if ingestion:
        kwargs["vectorIngestionConfiguration"] = ingestion

    resp = _client(region).create_data_source(**kwargs)
    return resp["dataSource"]["dataSourceId"]


def _chunking_config(strategy: str) -> dict | None:
    strategy = strategy.upper()
    if strategy == "DEFAULT":
        return None  # omit -> Bedrock uses its default ~300-token chunking
    if strategy == "NONE":
        return {"chunkingConfiguration": {"chunkingStrategy": "NONE"}}
    if strategy == "FIXED_SIZE":
        return {"chunkingConfiguration": {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {"maxTokens": 300, "overlapPercentage": 20},
        }}
    if strategy == "HIERARCHICAL":
        return {"chunkingConfiguration": {
            "chunkingStrategy": "HIERARCHICAL",
            "hierarchicalChunkingConfiguration": {
                "levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}],
                "overlapTokens": 60,
            },
        }}
    if strategy == "SEMANTIC":
        return {"chunkingConfiguration": {
            "chunkingStrategy": "SEMANTIC",
            "semanticChunkingConfiguration": {
                "maxTokens": 300, "bufferSize": 1, "breakpointPercentileThreshold": 95,
            },
        }}
    raise ValueError(f"Unknown chunking strategy: {strategy}")


def start_sync(knowledge_base_id: str, data_source_id: str, region: str) -> str:
    """Start an ingestion job (a sync). Returns the ingestionJobId."""
    resp = _client(region).start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
    )
    return resp["ingestionJob"]["ingestionJobId"]


def wait_for_sync(
    knowledge_base_id: str, data_source_id: str, job_id: str, region: str,
    poll_seconds: int = 10,
) -> dict:
    """Poll an ingestion job until COMPLETE or FAILED. Returns the final job object."""
    client = _client(region)
    while True:
        resp = client.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        job = resp["ingestionJob"]
        status = job["status"]
        if status in ("COMPLETE", "FAILED"):
            return job
        time.sleep(poll_seconds)
