terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.24" # aws_s3vectors_* resources land in 6.24
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  suffix        = "${data.aws_caller_identity.current.account_id}-${var.region}"
  corpus_bucket = coalesce(var.corpus_bucket_name, "${var.name}-corpus-${local.suffix}")
  vector_bucket = coalesce(var.vector_bucket_name, "${var.name}-vectors-${local.suffix}")

  # Bedrock stores the chunk text and its own bookkeeping as vector metadata, and S3 Vectors
  # caps FILTERABLE metadata at 2048 bytes per vector. Unless these two keys are declared
  # non-filterable at index creation, anything but a very short chunk fails to ingest. On the
  # run this project measured, 5 of 6 documents failed and the survivor had a 499-byte chunk.
  # The key is AMAZON_BEDROCK_TEXT. AMAZON_BEDROCK_TEXT_CHUNK is the OpenSearch field name and
  # does nothing here. None of it can be changed after the index exists, which is why this is
  # in Terraform rather than a step you run by hand.
  non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]

  embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"
}

# ---------------------------------------------------------------------------
# Corpus bucket. The loader writes <slug>.md and <slug>.md.metadata.json here.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "corpus" {
  bucket        = local.corpus_bucket
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "corpus" {
  bucket                  = aws_s3_bucket.corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# Vector store. The index metadata configuration is the whole reason this exists.
# ---------------------------------------------------------------------------

resource "aws_s3vectors_vector_bucket" "store" {
  vector_bucket_name = local.vector_bucket
  force_destroy      = var.force_destroy
}

resource "aws_s3vectors_index" "store" {
  vector_bucket_name = aws_s3vectors_vector_bucket.store.vector_bucket_name
  index_name         = var.index_name
  data_type          = "float32"
  dimension          = var.embedding_dimension
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = local.non_filterable_metadata_keys
  }
}

# ---------------------------------------------------------------------------
# Knowledge base service role. Bedrock assumes this, not you.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    # Keep the role usable only by knowledge bases in this account, so a confused-deputy
    # call from another account cannot borrow it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "kb" {
  statement {
    sid       = "ReadCorpus"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.corpus.arn, "${aws_s3_bucket.corpus.arn}/*"]
  }
  statement {
    sid       = "InvokeEmbeddingModel"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }
  statement {
    sid = "WriteVectors"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:QueryVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:ListVectors",
    ]
    resources = [
      aws_s3vectors_vector_bucket.store.vector_bucket_arn,
      "${aws_s3vectors_vector_bucket.store.vector_bucket_arn}/index/*",
    ]
  }
}

resource "aws_iam_role" "kb" {
  name               = "${var.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "kb" {
  name   = "${var.name}-access"
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}

# ---------------------------------------------------------------------------
# Knowledge base and data source. Optional, because scripts/2_create_kb.py
# creates these too, with every choice visible in the call.
# Managing them here instead means `tofu destroy` removes everything.
# ---------------------------------------------------------------------------

resource "aws_bedrockagent_knowledge_base" "this" {
  count    = var.create_knowledge_base ? 1 : 0
  name     = var.name
  role_arn = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      vector_bucket_arn = aws_s3vectors_vector_bucket.store.vector_bucket_arn
      index_name        = aws_s3vectors_index.store.index_name
    }
  }

  # The role policy has to exist before Bedrock validates it during creation.
  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "corpus" {
  count             = var.create_knowledge_base ? 1 : 0
  knowledge_base_id = aws_bedrockagent_knowledge_base.this[0].id
  name              = "${var.name}-corpus"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.corpus.arn
      inclusion_prefixes = ["${var.corpus_prefix}/"]
    }
  }
}
