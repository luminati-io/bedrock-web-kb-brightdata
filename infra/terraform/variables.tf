variable "region" {
  description = "AWS Region. Check your Titan embedding quota here first, it is per-Region and can be zero."
  type        = string
  default     = "us-east-2"
}

variable "name" {
  description = "Base name for the bucket, vector store, role, and knowledge base."
  type        = string
  default     = "web-kb"
}

variable "corpus_bucket_name" {
  description = "Override the corpus bucket name. Defaults to <name>-corpus-<account>-<region>."
  type        = string
  default     = null
}

variable "vector_bucket_name" {
  description = "Override the vector bucket name. Defaults to <name>-vectors-<account>-<region>."
  type        = string
  default     = null
}

variable "corpus_prefix" {
  description = "Prefix the loader writes to and the data source reads from."
  type        = string
  default     = "docs"
}

variable "index_name" {
  description = "S3 Vectors index name."
  type        = string
  default     = "web-kb-index"
}

variable "embedding_model_id" {
  description = "Bedrock embedding model. Changing it after creation means recreating the knowledge base."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimension" {
  description = "Must match the model. Titan Text Embeddings V2 supports 256, 512, or 1024."
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024], var.embedding_dimension)
    error_message = "Titan Text Embeddings V2 supports 256, 512, or 1024 dimensions."
  }
}

variable "create_knowledge_base" {
  description = "Create the knowledge base and data source here. Set false to create them with scripts/2_create_kb.py instead."
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow `tofu destroy` to delete non-empty buckets. Convenient for a pilot, dangerous for a corpus you care about."
  type        = bool
  default     = false
}
