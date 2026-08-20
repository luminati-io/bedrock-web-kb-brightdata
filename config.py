"""Central configuration, loaded from environment variables (.env supported).

Nothing here is a secret in source control. Copy .env.example to .env and fill it in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional, and env vars still work without it
    pass


class MissingConfig(RuntimeError):
    """Raised with everything that is missing at once, not one variable per run."""


def _check(names: list[str]) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingConfig(
            "Missing required configuration: " + ", ".join(missing) + "\n"
            "Copy .env.example to .env and fill those in, or export them, then run again.\n"
            "See the Setup section of README.md."
        )


@dataclass(frozen=True)
class Settings:
    # --- Bright Data ---
    brightdata_token: str
    unlocker_zone: str
    serp_zone: str
    crawl_dataset_id: str

    # --- AWS / S3 ---
    aws_region: str
    s3_bucket: str
    s3_prefix: str

    # --- Bedrock knowledge base ---
    embedding_model_arn: str
    generation_model_arn: str
    kb_role_arn: str

    # Vector store (S3 Vectors path)
    s3_vector_bucket_arn: str
    s3_vector_index_name: str
    # Vector store (OpenSearch Serverless path, optional alternative)
    opensearch_collection_arn: str

    # Set after creation (scripts print these, so put them back in .env)
    knowledge_base_id: str
    data_source_id: str

    @classmethod
    def load(cls, need: tuple[str, ...] = ("brightdata", "aws")) -> "Settings":
        """Load settings, requiring only the sides this script actually touches.

        The diagnostics that decide whether a target is even worth scraping talk to Bright
        Data and nothing else, so making them demand an S3 bucket would put an AWS setup
        between you and a thirty-second question. Pass need=("brightdata",) for those.
        """
        required: list[str] = []
        if "brightdata" in need:
            required.append("BRIGHTDATA_TOKEN")
        if "aws" in need:
            required.append("S3_BUCKET")
        _check(required)

        region = os.environ.get("AWS_REGION", "us-east-1")
        titan = f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
        return cls(
            brightdata_token=os.environ.get("BRIGHTDATA_TOKEN", ""),
            unlocker_zone=os.environ.get("WEB_UNLOCKER_ZONE", "web_unlocker"),
            serp_zone=os.environ.get("SERP_ZONE", "serp_api"),
            crawl_dataset_id=os.environ.get("CRAWL_DATASET_ID", ""),
            aws_region=region,
            s3_bucket=os.environ.get("S3_BUCKET", ""),
            s3_prefix=os.environ.get("S3_PREFIX", "docs"),
            embedding_model_arn=os.environ.get("EMBEDDING_MODEL_ARN", titan),
            generation_model_arn=os.environ.get("GENERATION_MODEL_ARN", ""),
            kb_role_arn=os.environ.get("KB_ROLE_ARN", ""),
            s3_vector_bucket_arn=os.environ.get("S3_VECTOR_BUCKET_ARN", ""),
            s3_vector_index_name=os.environ.get("S3_VECTOR_INDEX_NAME", "web-kb-index"),
            opensearch_collection_arn=os.environ.get("OPENSEARCH_COLLECTION_ARN", ""),
            knowledge_base_id=os.environ.get("KNOWLEDGE_BASE_ID", ""),
            data_source_id=os.environ.get("DATA_SOURCE_ID", ""),
        )
