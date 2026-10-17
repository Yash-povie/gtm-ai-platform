resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "gtm"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "data" {
  name   = "gtm-data"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
}

# pgvector ships with RDS PostgreSQL 16; `CREATE EXTENSION vector` runs in migration 0001.
resource "aws_db_instance" "pg" {
  identifier                          = "gtm-pg"
  engine                              = "postgres"
  engine_version                      = "16"
  instance_class                      = "db.t4g.micro"
  allocated_storage                   = 20
  storage_type                        = "gp3"
  storage_encrypted                   = true
  db_name                             = "gtm_platform"
  username                            = "gtm"
  password                            = random_password.db.result
  db_subnet_group_name                = aws_db_subnet_group.main.name
  vpc_security_group_ids              = [aws_security_group.data.id]
  backup_retention_period             = 7
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "gtm-pg-final"
  multi_az                            = false # single-AZ for the demo budget; set true for real production
  publicly_accessible                 = false
  iam_database_authentication_enabled = true
  performance_insights_enabled        = false
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "gtm"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id                 = "gtm-redis"
  engine                     = "redis"
  node_type                  = "cache.t4g.micro"
  num_cache_nodes            = 1
  engine_version             = "7.1"
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.data.id]
  transit_encryption_enabled = true
}
