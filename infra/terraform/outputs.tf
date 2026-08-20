output "env" {
  description = "Paste these into .env. Terraform does not write the file for you, since it is gitignored and may already hold your Bright Data token."
  value = join("\n", compact([
    "AWS_REGION=${var.region}",
    "S3_BUCKET=${aws_s3_bucket.corpus.id}",
    "S3_PREFIX=${var.corpus_prefix}",
    "KB_ROLE_ARN=${aws_iam_role.kb.arn}",
    "S3_VECTOR_BUCKET_ARN=${aws_s3vectors_vector_bucket.store.vector_bucket_arn}",
    "S3_VECTOR_INDEX_NAME=${aws_s3vectors_index.store.index_name}",
    "EMBEDDING_MODEL_ARN=${local.embedding_model_arn}",
    var.create_knowledge_base ? "KNOWLEDGE_BASE_ID=${aws_bedrockagent_knowledge_base.this[0].id}" : null,
    var.create_knowledge_base ? "DATA_SOURCE_ID=${aws_bedrockagent_data_source.corpus[0].data_source_id}" : null,
  ]))
}

output "corpus_bucket" {
  description = "Bucket the loader writes documents and sidecars to."
  value       = aws_s3_bucket.corpus.id
}

output "knowledge_base_id" {
  description = "Empty when create_knowledge_base is false."
  value       = var.create_knowledge_base ? aws_bedrockagent_knowledge_base.this[0].id : ""
}
