#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
: "${S3_BUCKET:?S3_BUCKET must name the Terraform-created data lake bucket}"

RUN_DATE="${RUN_DATE:-$(date -u +%F)}"
export DBT_PROFILES_DIR="${ROOT_DIR}/dbt_project"
export DUCKDB_PATH="${DUCKDB_PATH:-/tmp/visa_propensity.duckdb}"

dbt deps --project-dir "${ROOT_DIR}/dbt_project"
if [[ -n "${DBT_SELECTION:-}" ]]; then
  dbt build --project-dir "${ROOT_DIR}/dbt_project" --select "${DBT_SELECTION}" --vars "{run_date: '${RUN_DATE}'}"
else
  # The campaign mart depends on scores produced after the first dbt pass.
  dbt build --project-dir "${ROOT_DIR}/dbt_project" --exclude tag:marts --vars "{run_date: '${RUN_DATE}'}"
fi

