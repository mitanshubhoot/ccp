variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cell" {
  type        = string
  description = "The cell's name: one database, one raw bucket, one deployment."
  default     = "use1-a"
}

variable "audit_role_arn" {
  type        = string
  description = "A role in the separate audit account that Terraform assumes."
}

variable "app_account_id" {
  type        = string
  description = "The account the cell runs in, trusted by the audit account's key and bucket."
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "tenants" {
  type        = set(string)
  description = "One KMS key per tenant for its raw objects; destroying it offboards them."
  default     = []
}

variable "image" {
  type        = string
  description = "The container image (ECR URI and tag) the api and worker services run."
}

variable "db_instance_class" {
  type    = string
  default = "db.r7g.large"
}

variable "raw_retain_days" {
  type        = number
  description = "Governance-mode retention for each raw object (CCP_RAW_RETAIN_DAYS)."
  default     = 400
}

variable "audit_retain_days" {
  type        = number
  description = "Compliance-mode retention for each checkpoint: nobody can shorten it."
  default     = 2557
}

variable "alarm_topic_arn" {
  type        = string
  description = "SNS topic the CloudWatch alarms notify."
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate for the api's load balancer."
}
