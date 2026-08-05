resource "aws_apigatewayv2_api" "score" {
  name          = "visa-propensity-http-api"
  protocol_type = "HTTP"
  tags          = local.common_tags
}

resource "aws_apigatewayv2_integration" "score" {
  api_id                 = aws_apigatewayv2_api.score.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.score.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "score" {
  api_id    = aws_apigatewayv2_api.score.id
  route_key = "POST /score"
  target    = "integrations/${aws_apigatewayv2_integration.score.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.score.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    detailed_metrics_enabled = false
  }

  tags = local.common_tags
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.score.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.score.execution_arn}/*/*"
}

