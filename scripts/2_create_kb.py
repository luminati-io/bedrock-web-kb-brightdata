#!/usr/bin/env python3
"""Step 3: create the Bedrock knowledge base and connect the S3 bucket as a data source.

Prerequisites (see README): the vector store and the KB service role must already exist.
  - S3 Vectors path:      S3_VECTOR_BUCKET_ARN, S3_VECTOR_INDEX_NAME
  - OpenSearch path:      OPENSEARCH_COLLECTION_ARN  (with --store opensearch)

Usage:
    python scripts/2_create_kb.py --name web-kb --store s3vectors --chunking DEFAULT

Prints KNOWLEDGE_BASE_ID and DATA_SOURCE_ID. Put both back into your .env.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MissingConfig, Settings
from src.web_kb import knowledge_base as kb


def bucket_arn(bucket: str) -> str:
    return f"arn:aws:s3:::{bucket}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the Bedrock KB + S3 data source.")
    parser.add_argument("--name", default="web-kb")
    parser.add_argument("--store", choices=["s3vectors", "opensearch"], default="s3vectors")
    parser.add_argument("--chunking", default="DEFAULT",
                        help="DEFAULT | FIXED_SIZE | HIERARCHICAL | SEMANTIC | NONE")
    parser.add_argument("--dimension", type=int, default=1024,
                        help="Embedding dimension. Must match the model. Titan V2 supports "
                             "256, 512, or 1024 and defaults to 1024 here.")
    parser.add_argument("--skip-vector-store", action="store_true",
                        help="Do not create or check the S3 Vectors bucket and index. Use "
                             "this only when you have provisioned them yourself.")
    args = parser.parse_args()

    try:
        s = Settings.load()
    except MissingConfig as exc:
        sys.exit(str(exc))
    if not s.kb_role_arn:
        sys.exit("Set KB_ROLE_ARN in .env (the KB service role). See infra/ and the README.")

    if args.store == "s3vectors":
        if not s.s3_vector_bucket_arn:
            sys.exit("Set S3_VECTOR_BUCKET_ARN and S3_VECTOR_INDEX_NAME in .env.")
        if not args.skip_vector_store:
            bucket_name = s.s3_vector_bucket_arn.rsplit("/", 1)[-1]
            print(f"vector store s3://{bucket_name} index {s.s3_vector_index_name}")
            try:
                state = kb.ensure_vector_index(
                    bucket_name, s.s3_vector_index_name, s.aws_region,
                    dimension=args.dimension)
            except RuntimeError as exc:
                sys.exit(f"  {exc}")
            if state == "exists":
                print("  index already exists and is configured correctly")
        kb_id = kb.create_kb_s3_vectors(
            args.name, s.kb_role_arn, s.embedding_model_arn,
            s.s3_vector_bucket_arn, s.s3_vector_index_name, s.aws_region,
        )
    else:
        if not s.opensearch_collection_arn:
            sys.exit("Set OPENSEARCH_COLLECTION_ARN in .env.")
        kb_id = kb.create_kb_opensearch(
            args.name, s.kb_role_arn, s.embedding_model_arn,
            s.opensearch_collection_arn, s.s3_vector_index_name, s.aws_region,
        )
    print(f"KNOWLEDGE_BASE_ID={kb_id}")

    ds_id = kb.create_s3_data_source(
        kb_id, bucket_arn(s.s3_bucket), s.s3_prefix, s.aws_region, chunking=args.chunking,
    )
    print(f"DATA_SOURCE_ID={ds_id}")
    print("\nAdd both IDs to your .env, then run scripts/3_sync.py")


if __name__ == "__main__":
    main()
