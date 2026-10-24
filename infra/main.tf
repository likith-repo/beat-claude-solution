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
    tags = { Project = "lead-triage-agent" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name   = "lead-triage"
  bucket = "${local.name}-${data.aws_caller_identity.me.account_id}-${var.region}"
}

# ---------------------------------------------------------------- storage
resource "aws_s3_bucket" "runs" {
  bucket        = local.bucket
  force_destroy = true # teardown must not be blocked by run files
}

resource "aws_s3_bucket_public_access_block" "runs" {
  bucket                  = aws_s3_bucket.runs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    id     = "expire-runs"
    status = "Enabled"
    filter { prefix = "runs/" }
    expiration { days = var.retention_days }
  }
}

# ---------------------------------------------------------------- secrets
# Terraform creates the parameters with a placeholder. Real values are set outside Terraform
# (aws ssm put-parameter --overwrite), so secrets never enter state or this repo.
resource "aws_ssm_parameter" "api_key" {
  name  = "/${local.name}/api_key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "openrouter" {
  name  = "/${local.name}/openrouter_api_key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "serpapi" {
  name  = "/${local.name}/serpapi_api_key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

# ---------------------------------------------------------------- IAM
data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "api" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.api_key.arn]
  }
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.openrouter.arn, aws_ssm_parameter.serpapi.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "inline"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy" "worker" {
  name   = "inline"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# ---------------------------------------------------------------- Lambda
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = 14
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.name}-worker"
  role             = aws_iam_role.worker.arn
  runtime          = "python3.12"
  handler          = "backend.handler.worker_handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 900
  memory_size      = 512

  environment {
    variables = {
      BUCKET           = aws_s3_bucket.runs.bucket
      PARAM_OPENROUTER = aws_ssm_parameter.openrouter.name
      PARAM_SERPAPI    = aws_ssm_parameter.serpapi.name
      OPENROUTER_MODEL = var.openrouter_model
      MAX_SEARCHES     = tostring(var.max_searches_per_run)
    }
  }
  depends_on = [aws_cloudwatch_log_group.worker]
}

# The worker catches its own errors; an async retry would only repeat a paid run.
resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name          = aws_lambda_function.worker.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "backend.handler.api_handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      BUCKET          = aws_s3_bucket.runs.bucket
      PARAM_API_KEY   = aws_ssm_parameter.api_key.name
      WORKER_FUNCTION = aws_lambda_function.worker.function_name
    }
  }
  depends_on = [aws_cloudwatch_log_group.api]
}

# ---------------------------------------------------------------- API Gateway (HTTP API)
resource "aws_apigatewayv2_api" "http" {
  name          = local.name
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = var.allowed_origins
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "x-api-key"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "start" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /runs"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "get" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /runs/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
