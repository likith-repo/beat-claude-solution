output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "bucket" {
  value = aws_s3_bucket.runs.bucket
}

output "ssm_parameters" {
  description = "Set real values with: aws ssm put-parameter --overwrite --type SecureString --name <name> --value <value>"
  value = {
    api_key    = aws_ssm_parameter.api_key.name
    openrouter = aws_ssm_parameter.openrouter.name
    serpapi    = aws_ssm_parameter.serpapi.name
  }
}
