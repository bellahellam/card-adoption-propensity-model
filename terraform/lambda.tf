resource "aws_ecr_repository" "score" {
  name                 = "visa-propensity-score"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "score" {
  repository = aws_ecr_repository.score.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the latest ten API images."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_repository_policy" "lambda_pull" {
  repository = aws_ecr_repository.score.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaImageRetrieval"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
      }
    ]
  })
}

resource "terraform_data" "bootstrap_image" {
  triggers_replace = [local.lambda_image_tag]
  depends_on       = [aws_ecr_repository.score]

  provisioner "local-exec" {
    command = <<-EOT
      aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${local.ecr_registry}
      docker build --platform linux/amd64 --tag ${aws_ecr_repository.score.repository_url}:${local.lambda_image_tag} ${path.module}/../src/api
      docker push ${aws_ecr_repository.score.repository_url}:${local.lambda_image_tag}
    EOT
  }
}

resource "aws_lambda_function" "score" {
  function_name    = local.lambda_name
  description      = "Scores Visa card-adoption propensity from aggregate customer features."
  role             = aws_iam_role.lambda.arn
  package_type     = "Image"
  image_uri        = "${aws_ecr_repository.score.repository_url}:${local.lambda_image_tag}"
  memory_size      = 512
  timeout          = 10

  environment {
    variables = {
      MODEL_BUCKET      = aws_s3_bucket.data_lake.bucket
      MODEL_KEY         = "models/visa_card_adoption/model.pkl"
      BASE_ADOPTION_RATE = "0.03"
    }
  }

  depends_on = [terraform_data.bootstrap_image, aws_ecr_repository_policy.lambda_pull]

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.score.function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_sns_topic" "lambda_alerts" {
  name = "visa-propensity-lambda-errors"
  tags = local.common_tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.lambda_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "visa-propensity-lambda-errors"
  alarm_description   = "Lambda scored at least one error over five minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.lambda_alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.score.function_name
  }

  tags = local.common_tags
}
