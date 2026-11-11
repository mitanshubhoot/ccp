output "api_url" {
  value = "https://${aws_lb.api.dns_name}"
}

output "raw_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "audit_bucket" {
  value = aws_s3_bucket.audit.bucket
}

output "audit_signing_key" {
  value = aws_kms_key.audit_signing.arn
}

output "db_endpoint" {
  value = aws_db_instance.ccp.address
}

output "cluster" {
  value = aws_ecs_cluster.ccp.name
}
