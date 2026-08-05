SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

.PHONY: help init generate-data dbt-build train score build-mart deploy-infra deploy-api test-api run-pipeline

help:
	@echo "Targets: init generate-data dbt-build train score deploy-infra deploy-api test-api run-pipeline"

init:
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt
	dbt deps --project-dir dbt_project
	terraform -chdir=terraform init

generate-data:
	python src/ingestion/generate_and_tokenize.py --run-date "$${RUN_DATE:-$$(date -u +%F)}"

dbt-build:
	./scripts/run_dbt.sh

train:
	python src/training/train.py

score:
	python src/scoring/batch_score.py --score-date "$${RUN_DATE:-$$(date -u +%F)}"

build-mart:
	DBT_SELECTION="+mart_campaign_segments" ./scripts/run_dbt.sh

deploy-infra:
	terraform -chdir=terraform init
	terraform -chdir=terraform apply

deploy-api:
	./scripts/deploy_lambda.sh

test-api:
	@API_ENDPOINT="$$(terraform -chdir=terraform output -raw api_endpoint)"; \
	curl --fail --silent --show-error --request POST "$${API_ENDPOINT}/score" \
		--header "content-type: application/json" \
		--data '{"recency_days":5,"frequency_30d":12,"monetary_30d":1500.0,"digital_ratio_30d":0.75,"cross_border_count_90d":2,"age":32,"tenure_months":24}'

run-pipeline:
	$(MAKE) generate-data
	$(MAKE) dbt-build
	$(MAKE) train
	$(MAKE) score
	$(MAKE) build-mart

