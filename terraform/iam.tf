data "aws_iam_policy_document" "github_oidc_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/${var.github_branch}"]
    }
  }
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  tags            = local.common_tags
}

resource "aws_iam_role" "github_actions" {
  name               = "visa-propensity-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "github_actions" {
  statement {
    sid     = "ListOnlyPocBucket"
    effect  = "Allow"
    actions = ["s3:ListBucket"]
    resources = [
      aws_s3_bucket.data_lake.arn,
    ]
  }

  statement {
    sid = "ReadWriteOnlyPocBucketObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "${aws_s3_bucket.data_lake.arn}/*",
    ]
  }

  statement {
    sid       = "DeployOnlyThisLambda"
    effect    = "Allow"
    actions   = ["lambda:UpdateFunctionCode"]
    resources = [aws_lambda_function.score.arn]
  }

  statement {
    sid     = "GetEcrLoginToken"
    effect  = "Allow"
    actions = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushOnlyPropensityApiImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.score.arn]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "visa-propensity-poc-deploy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions.json
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "visa-propensity-score-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_model_read" {
  statement {
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.data_lake.arn}/models/visa_card_adoption/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_model_read" {
  name   = "read-propensity-model"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_model_read.json
}
