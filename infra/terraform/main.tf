terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "gtm-ai-portfolio", ManagedBy = "terraform" } }
}

variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "alert_email" {
  type        = string
  description = "Receives the budget alarm. Day-1 requirement."
}
variable "certificate_arn" {
  type        = string
  default     = ""
  description = "ACM cert for the ALB. HTTPS everywhere: with no cert the ALB serves nothing on 80 except a 503."
}
variable "monthly_budget_usd" {
  type    = number
  default = 35
}
variable "desired_count" {
  type    = number
  default = 1
}

data "aws_availability_zones" "available" { state = "available" }

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# --- Network (no NAT gateway: ~$32/mo saved; tasks sit in public subnets but only the ALB can reach them) ---
resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "gtm" }
}

resource "aws_internet_gateway" "igw" { vpc_id = aws_vpc.main.id }

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone = local.azs[count.index]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name   = "gtm-alb"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
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

resource "aws_security_group" "ecs" {
  name   = "gtm-ecs"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Registry + cluster ---
resource "aws_ecr_repository" "svc" {
  for_each             = toset(["api", "worker"])
  name                 = "gtm-${each.key}"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/gtm"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = "gtm-cluster"
  setting {
    name  = "containerInsights"
    value = "disabled" # enable if the demo budget allows
  }
}

# --- Load balancer: HTTPS only; port 80 redirects ---
resource "aws_lb" "main" {
  name               = "gtm-alb"
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "api" {
  name        = "gtm-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = var.certificate_arn == "" ? "fixed-response" : "redirect"
    dynamic "redirect" {
      for_each = var.certificate_arn == "" ? [] : [1]
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
    dynamic "fixed_response" {
      for_each = var.certificate_arn == "" ? [1] : []
      content {
        content_type = "text/plain"
        message_body = "HTTPS is not configured yet (set certificate_arn)."
        status_code  = "503"
      }
    }
  }
}

# --- Task definitions (ARM64 / Graviton) ---
locals {
  common_env = [
    { name = "GTM_ENV", value = "production" },
    { name = "GTM_LIVE_SEND_ENABLED", value = "false" },
    { name = "GTM_REQUIRE_APPROVAL_ALL", value = "true" },
    { name = "LLM_PRIMARY_MODEL", value = "claude-sonnet-5-5" },
    { name = "LLM_JUDGE_MODEL", value = "claude-haiku-5-5" },
    { name = "DATABASE_URL", value = "postgresql+asyncpg://gtm:${random_password.db.result}@${aws_db_instance.pg.address}:5432/gtm_platform" },
    { name = "REDIS_URL", value = "rediss://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0" },
  ]
  secret_names = ["ANTHROPIC_API_KEY", "NEO4J_URI", "NEO4J_PASSWORD", "NEO4J_USER", "APOLLO_API_KEY", "HUBSPOT_PRIVATE_APP_TOKEN",
    "CLAY_SIGNING_SECRET", "N8N_WEBHOOK_SECRET", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "UNSUBSCRIBE_JWT_SECRET"]
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = toset(["api", "worker"])
  family                   = "gtm-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name         = each.key # the migration step overrides this container's command
    image        = "${aws_ecr_repository.svc[each.key].repository_url}:latest"
    essential    = true
    portMappings = each.key == "api" ? [{ containerPort = 8000 }] : []
    environment  = local.common_env
    secrets      = [for n in local.secret_names : { name = n, valueFrom = aws_ssm_parameter.secret[n].arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.app.name, awslogs-region = var.region, awslogs-stream-prefix = each.key }
    }
  }])
}

resource "aws_ecs_service" "svc" {
  for_each        = toset(["api", "worker"])
  name            = "gtm-${each.key}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.svc[each.key].arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.api.arn
      container_name   = "api"
      container_port   = 8000
    }
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle { ignore_changes = [desired_count] }
}

# --- Day-1 cost guardrail ---
resource "aws_budgets_budget" "monthly" {
  name         = "gtm-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
