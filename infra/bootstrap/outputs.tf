output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "plan_role_arn" {
  description = "GitHub repo variable AWS_PLAN_ROLE_ARN"
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "GitHub repo variable AWS_APPLY_ROLE_ARN"
  value       = aws_iam_role.apply.arn
}

output "runtime_boundary_arn" {
  description = "Set as permissions_boundary on both IAM roles in ../main.tf (after this stack is applied)"
  value       = aws_iam_policy.runtime_boundary.arn
}
