# The raw bucket (A-136): versioning and Object Lock from creation, governance mode by
# default so a mistaken retention can still be corrected by a named role with the bypass,
# event holds through legal holds, and each tenant's objects under its own KMS key with
# Bucket Keys on. Delete markers are not protected by Object Lock, so no role the app holds
# may write one: the app may put, get and hold, never delete.

resource "aws_kms_key" "raw" {
  for_each                = var.tenants
  description             = "CCP raw objects of tenant ${each.key}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "raw" {
  for_each      = var.tenants
  name          = "alias/ccp-raw-${each.key}"
  target_key_id = aws_kms_key.raw[each.key].key_id
}

resource "aws_s3_bucket" "raw" {
  bucket              = "ccp-raw-${var.cell}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.raw_retain_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "raw" {
  bucket = aws_s3_bucket.raw.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "KmsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.raw.arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" } }
      },
    ]
  })
}

# The audit account (A-137): checkpoints locked in compliance mode, and the key that signs
# them, which the app may use to sign but nobody in the cell's account may delete or export.

resource "aws_s3_bucket" "audit" {
  provider            = aws.audit
  bucket              = "ccp-audit-${var.cell}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "audit" {
  provider = aws.audit
  bucket   = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  provider = aws.audit
  bucket   = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_retain_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  provider                = aws.audit
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "audit" {
  provider = aws.audit
  bucket   = aws_s3_bucket.audit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AppWritesCheckpoints"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
        Resource  = "${aws_s3_bucket.audit.arn}/*"
      },
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

resource "aws_kms_key" "audit_signing" {
  provider                 = aws.audit
  description              = "Signs CCP audit checkpoints (${var.cell})"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
  deletion_window_in_days  = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AuditAccountAdministers"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.audit.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AppSignsAndVerifies"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = ["kms:Sign", "kms:Verify", "kms:GetPublicKey"]
        Resource  = "*"
      },
    ]
  })
}

data "aws_caller_identity" "audit" {
  provider = aws.audit
}
