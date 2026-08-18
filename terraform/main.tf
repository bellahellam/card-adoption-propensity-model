terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS region for all POC resources."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "Exact GitHub owner/repository allowed to assume the deployment role, for example acme/visa-propensity-dbt-aws."
  type        = string
}

variable "github_branch" {
  description = "Branch whose GitHub Actions workflows may assume the deployment role."
  type        = string
  default     = "main"
}

variable "alert_email" {
  description = "Email address subscribed to Lambda error alerts. Terraform creates an SNS confirmation email."
  type        = string
  default     = "replace-with-your-email@example.com"
}

locals {
  project_name = "visa-card-adoption"
  lambda_name  = "visa-card-propensity-score"
  lambda_image_tag = substr(sha256(join("", [
    filesha256("${path.module}/../src/api/Dockerfile"),
    filesha256("${path.module}/../src/api/lambda_handler.py"),
  ])), 0, 16)
  ecr_registry = split("/", aws_ecr_repository.score.repository_url)[0]
  common_tags = {
    Project     = local.project_name
    ManagedBy   = "Terraform"
    Environment = "poc"
  }
}
