# One-off, permanent foundations: the S3 bucket that holds Terraform state for the other stacks, and a monthly budget alert.

# Both cost next to nothing while idle (a few KB of state; the first two AWS Budgets are free),
# so this stack is applied once and left up. Its own state is local (terraform.tfstate here,
# git-ignored): a state bucket can't store the state of the stack that creates it. Losing that
# file only means `terraform import` before the next change; nothing else depends on it.

locals {
  state_bucket = "${var.project_name}-tfstate-${var.aws_account_id}"
}

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18:Access logging for a single-user state bucket adds cost without audit value here
  #checkov:skip=CKV_AWS_144:Cross-region replication not needed - state can be rebuilt by import
  #checkov:skip=CKV_AWS_145:SSE-S3 is sufficient; a customer-managed key adds cost and a key policy to manage
  #checkov:skip=CKV2_AWS_62:Event notifications not needed
  bucket = local.state_bucket

  # Deleting the state bucket by accident would orphan every resource it tracks
  lifecycle {
    prevent_destroy = true
  }
}

# Every write keeps the previous version, so a bad apply or a corrupted state file can be rolled back
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old state versions are kept 30 days, then removed, so storage never builds up
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

# State can contain sensitive values: refuse anything not sent over TLS
data "aws_iam_policy_document" "state" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  policy     = data.aws_iam_policy_document.state.json
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# The safety net for a forgotten `make down`: an email when real spend passes half the
# budget, and another when AWS forecasts the month will go over it.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

output "state_bucket" {
  description = "Bucket the other stacks keep their state in."
  value       = aws_s3_bucket.state.id
}
