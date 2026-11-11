# RDS PostgreSQL 17 (A-138): Multi-AZ, encrypted, point-in-time restore for 35 days, and
# pgvector (0.8.x on RDS 17; `ccp db migrate` creates the extension). Snapshots feed the
# restore drill: restore one, alter a row, and `ccp audit verify` must find it (A-137).

resource "aws_db_subnet_group" "ccp" {
  name       = "ccp-${var.cell}"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "db" {
  name   = "ccp-db-${var.cell}"
  vpc_id = var.vpc_id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

resource "aws_db_parameter_group" "ccp" {
  name   = "ccp-pg17-${var.cell}"
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "ccp" {
  identifier                      = "ccp-${var.cell}"
  engine                          = "postgres"
  engine_version                  = "17"
  instance_class                  = var.db_instance_class
  allocated_storage               = 100
  max_allocated_storage           = 1000
  storage_type                    = "gp3"
  storage_encrypted               = true
  multi_az                        = true
  db_name                         = "ccp"
  username                        = "ccp_owner"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.ccp.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.ccp.name
  backup_retention_period         = 35
  copy_tags_to_snapshot           = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "ccp-${var.cell}-final"
  performance_insights_enabled    = true
  auto_minor_version_upgrade      = true
  publicly_accessible             = false
  enabled_cloudwatch_logs_exports = ["postgresql"]
}
