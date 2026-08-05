output "s3_bucket_name" {
  description = "Data lake bucket. Add this value as the S3_BUCKET GitHub repository secret."
  value       = aws_s3_bucket.data_lake.bucket
}

output "github_actions_role_arn" {
  description = "OIDC role ARN. Add this value as the AWS_ROLE_ARN GitHub repository secret."
  value       = aws_iam_role.github_actions.arn
}

output "api_endpoint" {
  description = "HTTP API base URL; append /score for the scoring route."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "lambda_function_name" {
  value = aws_lambda_function.score.function_name
}

output "ecr_repository_url" {
  description = "Lambda image repository; add this as the ECR_REPOSITORY_URL GitHub repository secret."
  value       = aws_ecr_repository.score.repository_url
}
