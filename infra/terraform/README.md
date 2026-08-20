# Terraform for the AWS side

Provisions everything the pipeline needs on AWS so the one setting that silently destroys a
corpus isn't something you type by hand.

```bash
cd infra/terraform
tofu init            # or terraform init
tofu plan            # creates nothing, shows what would happen
tofu apply
tofu output -raw env >> ../../.env    # then add your Bright Data token
```

What it creates:

- the corpus S3 bucket, encrypted, public access blocked
- an S3 Vectors bucket and index, with `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA`
  declared non-filterable, the setting that decides whether Bedrock can ingest your
  documents at all and cannot be changed afterward
- the knowledge base service role, scoped to this corpus bucket, this vector store, and the
  one embedding model, with an `aws:SourceAccount` condition on the trust policy
- the knowledge base and its S3 data source, unless you set `create_knowledge_base = false`

Set `create_knowledge_base = false` if you would rather create those with
`scripts/2_create_kb.py`, which prints the same IDs. Everything else still gets provisioned
here.

## Before you apply

**Check your embedding quota before anything else.** It's set per Region and can be zero
on a new account, which lets all of this apply cleanly and then fails every ingestion job.

```bash
aws service-quotas list-service-quotas --service-code bedrock --region us-east-2 \
  --query "Quotas[?contains(QuotaName,'Titan Text Embeddings V2') \
           && contains(QuotaName,'requests per minute')].[QuotaName,Value]" --output text
```

## Notes

- Needs AWS provider 6.24 or later, which is where the `aws_s3vectors_*` resources arrive.
  Validated and planned against 6.60.0.
- `force_destroy` defaults to false, so `tofu destroy` will refuse to delete non-empty
  buckets. Set it true for a throwaway pilot, leave it false for anything you care about.
- `embedding_dimension` is validated against the values Titan Text Embeddings V2 accepts,
  because a mismatch between the model and the index fails at write time rather than at plan
  time.
