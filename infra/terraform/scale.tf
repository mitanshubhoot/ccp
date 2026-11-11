# LT9 (spec.md A-172): growth without a redesign. PgBouncer in transaction mode in front of
# RDS for the API and workers (LISTEN keeps a direct connection), and a read replica the
# reports read while its lag is within CCP_REPLICA_MAX_LAG_SECONDS. A dedicated or EU cell is
# this same module applied with its own `cell`, `region` and tenants; the EU cell's models
# go through Bedrock inference profiles in its region (config/cells.yaml).

resource "aws_db_instance" "replica" {
  count                        = var.read_replica ? 1 : 0
  identifier                   = "ccp-${var.cell}-replica"
  replicate_source_db          = aws_db_instance.ccp.identifier
  instance_class               = var.db_instance_class
  storage_encrypted            = true
  publicly_accessible          = false
  vpc_security_group_ids       = [aws_security_group.db.id]
  parameter_group_name         = aws_db_parameter_group.ccp.name
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  skip_final_snapshot          = true
}

resource "aws_security_group" "pooler" {
  name   = "ccp-pooler-${var.cell}"
  vpc_id = var.vpc_id
  ingress {
    from_port       = 6432
    to_port         = 6432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

resource "aws_ecs_task_definition" "pooler" {
  family                   = "ccp-pooler-${var.cell}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([
    {
      name         = "pgbouncer"
      image        = var.pgbouncer_image
      essential    = true
      portMappings = [{ containerPort = 6432 }]
      environment = [
        { name = "POOL_MODE", value = "transaction" },
        { name = "MAX_CLIENT_CONN", value = "2000" },
        { name = "DEFAULT_POOL_SIZE", value = "40" },
        { name = "SERVER_TLS_SSLMODE", value = "require" },
      ]
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "arn:aws:secretsmanager:${var.region}:${var.app_account_id}:secret:ccp/${var.cell}/DATABASE_DIRECT_URL"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ccp.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "pooler"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "pooler" {
  name            = "ccp-pooler"
  cluster         = aws_ecs_cluster.ccp.id
  task_definition = aws_ecs_task_definition.pooler.arn
  desired_count   = 2
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [aws_security_group.pooler.id, aws_security_group.tasks.id]
  }
}
