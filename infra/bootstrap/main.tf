# One-time stack, applied by hand (not by CI): the Terraform state bucket plus the GitHub OIDC
# provider and the two roles the workflow assumes. It lives apart from ../main.tf so the CI role
# can never edit its own permissions. Its state is local (small, and you keep it).
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "lead-triage-agent", Stack = "ci-bootstrap" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  acct         = data.aws_caller_identity.me.account_id
  state_bucket = "lead-triage-tfstate-${local.acct}-${var.region}"
  app_bucket   = "lead-triage-${local.acct}-${var.region}" # must match local.bucket in ../main.tf
  repo         = var.github_sub_prefix
  state_key    = "lead-triage/terraform.tfstate"

  # The only IAM roles ../main.tf creates. Named exactly so the CI roles (ci-lead-triage-*) fall outside.
  runtime_roles = [for n in ["lead-triage-api", "lead-triage-worker"] : "arn:aws:iam::${local.acct}:role/${n}"]
  ci_roles      = ["arn:aws:iam::${local.acct}:role/ci-lead-triage-plan", "arn:aws:iam::${local.acct}:role/ci-lead-triage-apply"]
}

# ---------------------------------------------------------------- state bucket
resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------- GitHub OIDC
# An account can hold only one provider per URL. Set create_oidc_provider = false if it exists.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.repo}:pull_request"]
    }
  }
}

# Apply is only trusted from the "production" environment, which the workflow reaches only through
# a manual run on main. A pull request cannot assume this role.
data "aws_iam_policy_document" "trust_apply" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.repo}:environment:production"]
    }
  }
}

resource "aws_iam_role" "plan" {
  name               = "ci-lead-triage-plan"
  assume_role_policy = data.aws_iam_policy_document.trust_plan.json
}

resource "aws_iam_role" "apply" {
  name               = "ci-lead-triage-apply"
  assume_role_policy = data.aws_iam_policy_document.trust_apply.json
}

# ---------------------------------------------------------------- runtime permissions boundary
# Every runtime role must carry this boundary, and the apply role can only create or write roles that
# do. Whatever inline policy a change puts on a Lambda role, it cannot exceed what is listed here.
data "aws_iam_policy_document" "runtime_boundary" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.acct}:log-group:/aws/lambda/lead-triage-*:*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${local.app_bucket}/runs/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.app_bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["runs/*"]
    }
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.acct}:parameter/lead-triage/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${local.acct}:function:lead-triage-*"]
  }
}

resource "aws_iam_policy" "runtime_boundary" {
  name   = "lead-triage-runtime-boundary"
  policy = data.aws_iam_policy_document.runtime_boundary.json
}

# ---------------------------------------------------------------- CI permissions
# Plan: reads Terraform state only. PR plans run with -refresh=false, so they never read the SSM
# secrets or the app data, and a pull request cannot exfiltrate either.
data "aws_iam_policy_document" "plan" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["s3:GetObject"] # not GetObjectVersion, so old state versions stay unreadable
    resources = ["${aws_s3_bucket.state.arn}/${local.state_key}"]
  }
}

data "aws_iam_policy_document" "apply" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"] # state and its lockfile
    resources = ["${aws_s3_bucket.state.arn}/${local.state_key}", "${aws_s3_bucket.state.arn}/${local.state_key}.tflock"]
  }

  # Refresh reads.
  statement {
    actions   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole"]
    resources = local.runtime_roles
  }
  statement {
    actions   = ["logs:DescribeLogGroups", "logs:ListTagsForResource", "logs:ListTagsLogGroup", "ssm:DescribeParameters"]
    resources = ["*"]
  }

  # IAM writes: only the two runtime roles, and only with the boundary attached.
  statement {
    actions   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:PutRolePermissionsBoundary"]
    resources = local.runtime_roles
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.runtime_boundary.arn]
    }
  }
  statement {
    actions   = ["iam:DeleteRole", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"]
    resources = local.runtime_roles
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = local.runtime_roles
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
  statement { # the boundary cannot be stripped, and the CI roles cannot be touched
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary"]
    resources = ["*"]
  }
  statement {
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = concat(local.ci_roles, [aws_iam_policy.runtime_boundary.arn])
  }

  statement {
    actions   = ["lambda:*"]
    resources = ["arn:aws:lambda:${var.region}:${local.acct}:function:lead-triage-*"]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:TagResource", "logs:UntagResource"]
    resources = ["arn:aws:logs:${var.region}:${local.acct}:log-group:/aws/lambda/lead-triage-*"]
  }
  statement {
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${local.app_bucket}", "arn:aws:s3:::${local.app_bucket}/*"]
  }
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter", "ssm:DeleteParameter", "ssm:ListTagsForResource", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource"]
    resources = ["arn:aws:ssm:${var.region}:${local.acct}:parameter/lead-triage/*"]
  }
  statement { # Terraform reads the SecureString parameters while refreshing
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
  statement {
    actions   = ["apigateway:*"]
    resources = ["arn:aws:apigateway:${var.region}::/apis", "arn:aws:apigateway:${var.region}::/apis/*", "arn:aws:apigateway:${var.region}::/tags/*"]
  }
}

resource "aws_iam_role_policy" "plan" {
  name   = "inline"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

resource "aws_iam_role_policy" "apply" {
  name   = "inline"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.apply.json
}
