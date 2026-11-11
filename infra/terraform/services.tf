# ECS Fargate (A-138): `api` behind a load balancer (`ccp serve`), `worker` draining the
# queue (`ccp worker run`), one image, one task role. The role may put, get and hold raw
# objects and use each tenant's key, sign checkpoints with the audit key and write them to
# the audit bucket; it holds no delete and no AWS write outside CCP's own buckets.

resource "aws_ecs_cluster" "ccp" {
  name = "ccp-${var.cell}"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "ccp" {
  name              = "/ccp/${var.cell}"
  retention_in_days = 400
}

resource "aws_security_group" "lb" {
  name   = "ccp-lb-${var.cell}"
  vpc_id = var.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name   = "ccp-tasks-${var.cell}"
  vpc_id = var.vpc_id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.lb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "ccp-task-${var.cell}"
  assume_role_policy = data.aws_iam_policy_document.tasks_assume.json
}

resource "aws_iam_role" "execution" {
  name               = "ccp-execution-${var.cell}"
  assume_role_policy = data.aws_iam_policy_document.tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "RawPutGetHold"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObjectLegalHold", "s3:PutObjectRetention"]
    resources = ["${aws_s3_bucket.raw.arn}/*"]
  }
  statement {
    sid       = "RawHead"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.raw.arn]
  }
  statement {
    sid       = "NoDelete"
    effect    = "Deny"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention"]
    resources = ["${aws_s3_bucket.raw.arn}/*", "${aws_s3_bucket.audit.arn}/*"]
  }
  statement {
    sid       = "TenantKeys"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = [for key in aws_kms_key.raw : key.arn]
  }
  statement {
    sid       = "SignCheckpoints"
    actions   = ["kms:Sign", "kms:Verify", "kms:GetPublicKey"]
    resources = [aws_kms_key.audit_signing.arn]
  }
  statement {
    sid       = "WriteCheckpoints"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.audit.arn}/*"]
  }
  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${var.app_account_id}:secret:ccp/${var.cell}/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "ccp-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

locals {
  environment = [
    { name = "CCP_RAW_STORE", value = "s3" },
    { name = "CCP_RAW_BUCKET", value = aws_s3_bucket.raw.bucket },
    { name = "CCP_RAW_RETAIN_DAYS", value = tostring(var.raw_retain_days) },
    { name = "CCP_AUDIT_BUCKET", value = aws_s3_bucket.audit.bucket },
    { name = "CCP_AUDIT_KMS_KEY_ID", value = aws_kms_key.audit_signing.arn },
    { name = "CCP_AUTH_MODE", value = "oidc" },
    { name = "AWS_REGION", value = var.region },
  ]
  secrets = [
    for name in ["DATABASE_URL", "DATABASE_ADMIN_URL", "DATABASE_AGENT_URL", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "CCP_OIDC_CLIENT_SECRET"] : {
      name      = name
      valueFrom = "arn:aws:secretsmanager:${var.region}:${var.app_account_id}:secret:ccp/${var.cell}/${name}"
    }
  ]
}

resource "aws_ecs_task_definition" "service" {
  for_each                 = { api = ["ccp", "serve", "--host", "0.0.0.0", "--port", "8000"], worker = ["ccp", "worker", "run"] }
  family                   = "ccp-${each.key}-${var.cell}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.key == "api" ? 1024 : 2048
  memory                   = each.key == "api" ? 2048 : 4096
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([
    {
      name         = each.key
      image        = var.image
      command      = each.value
      essential    = true
      environment  = local.environment
      secrets      = local.secrets
      portMappings = each.key == "api" ? [{ containerPort = 8000 }] : []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ccp.name
          awslogs-region        = var.region
          awslogs-stream-prefix = each.key
        }
      }
    }
  ])
}

resource "aws_lb" "api" {
  name               = "ccp-${var.cell}"
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  security_groups    = [aws_security_group.lb.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name        = "ccp-api-${var.cell}"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path = "/v1/health"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "service" {
  for_each        = aws_ecs_task_definition.service
  name            = "ccp-${each.key}"
  cluster         = aws_ecs_cluster.ccp.id
  task_definition = each.value.arn
  desired_count   = each.key == "api" ? 2 : 3
  launch_type     = "FARGATE"
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [aws_security_group.tasks.id]
  }
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.api.arn
      container_name   = "api"
      container_port   = 8000
    }
  }
}
