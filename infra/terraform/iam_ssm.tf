data "aws_caller_identity" "me" {}

# --- ECS roles ---
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "gtm-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_ssm" {
  statement {
    actions   = ["ssm:GetParameters", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/gtm/*"]
  }
}

resource "aws_iam_role_policy" "execution_ssm" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_ssm.json
}

resource "aws_iam_role" "task" {
  name               = "gtm-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# --- Secrets: SSM SecureString (free tier; Secrets Manager costs $0.40/secret/month) ---
resource "aws_ssm_parameter" "secret" {
  for_each = toset(local.secret_names)
  name     = "/gtm/${each.key}"
  type     = "SecureString"
  value    = "CHANGE_ME" # set the real value out of band: aws ssm put-parameter --overwrite ...
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "network_config" {
  name = "/gtm/deploy/network_config"
  type = "String"
  value = format("awsvpcConfiguration={subnets=[%s],securityGroups=[%s],assignPublicIp=ENABLED}",
  join(",", aws_subnet.public[*].id), aws_security_group.ecs.id)
}

# --- GitHub Actions OIDC deploy role (no long-lived AWS keys) ---
variable "github_repo" {
  type        = string
  description = "owner/repo allowed to deploy, e.g. yash/gtm-ai-portfolio"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main", "repo:${var.github_repo}:environment:production"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "gtm-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [for r in aws_ecr_repository.svc : r.arn]
  }
  statement {
    actions   = ["ecs:RunTask", "ecs:DescribeTasks", "ecs:UpdateService", "ecs:DescribeServices", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.network_config.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
