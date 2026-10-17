output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.svc : k => r.repository_url }
}

output "rds_endpoint" {
  value = aws_db_instance.pg.address
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN GitHub secret"
  value       = aws_iam_role.deploy.arn
}
