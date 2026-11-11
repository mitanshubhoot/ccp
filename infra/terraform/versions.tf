# CCP on AWS (spec.md A-138): one pooled cell per region, an audit account apart.
# Written and checked offline (tests/unit/test_infra.py parses it); applied only once AWS
# credentials are given (docs/verify-with-credentials.md).

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80"
    }
  }
  backend "s3" {} # bucket, key and lock table given at `terraform init -backend-config=...`
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { app = "ccp", cell = var.cell }
  }
}

# The audit account holds the checkpoint bucket and the signing key: whoever administers
# the cell's database holds neither (A-137).
provider "aws" {
  alias  = "audit"
  region = var.region
  assume_role {
    role_arn = var.audit_role_arn
  }
  default_tags {
    tags = { app = "ccp", cell = var.cell, account = "audit" }
  }
}
