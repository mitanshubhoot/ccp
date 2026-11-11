# CloudWatch alarms (A-138): the database's health, the services staying up, the api's
# errors, and the hourly audit verify, which logs "audit findings" when it fails (A-137).

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  alarm_name          = "ccp-${var.cell}-db-cpu"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.ccp.identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "ccp-${var.cell}-db-free-storage"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.ccp.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10737418240
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "ccp-${var.cell}-api-5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "workers_down" {
  alarm_name          = "ccp-${var.cell}-workers-running"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.ccp.name, ServiceName = "ccp-worker" }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [var.alarm_topic_arn]
}

resource "aws_cloudwatch_log_metric_filter" "audit_findings" {
  name           = "ccp-${var.cell}-audit-findings"
  log_group_name = aws_cloudwatch_log_group.ccp.name
  pattern        = "\"audit findings\""
  metric_transformation {
    name      = "AuditFindings"
    namespace = "CCP"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "audit_findings" {
  alarm_name          = "ccp-${var.cell}-audit-findings"
  namespace           = "CCP"
  metric_name         = "AuditFindings"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
}
