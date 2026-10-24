output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "bucket" {
  value = aws_s3_bucket.runs.bucket
}

output "ssm_parameters" {
  description = "Create and rotate with: aws ssm put-parameter --overwrite --type SecureString --name <name> --value <value>"
  value       = local.ssm_name
}
