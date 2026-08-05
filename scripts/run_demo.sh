#!/usr/bin/env bash
# Run the Orchestra P0 demo end-to-end.
#
# Usage:
#   scripts/run_demo.sh           # boot the API on :8000 and wait
#   scripts/run_demo.sh curl      # run a curl-based smoke flow
#
# This is a developer convenience, not part of the test suite.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" == "curl" ]]; then
  set -x
  curl -s -X POST http://127.0.0.1:8000/tasks \
    -H 'content-type: application/json' \
    -d @- <<'JSON' | jq .
{
  "contract_id": "ctr-001",
  "contract_text": "供应商：Acme Cloud Logistics Co., Ltd.\n采购方：Helios\n合同金额：RMB 8,600,000.00\n付款条款：Net 30\n生效日期：2026-01-15\n到期日期：2027-01-14\n管辖：香港\n终止条款：30日违约通知。",
  "vendor_id": "demo-vendor-001",
  "budget_usd": 2.0
}
JSON
  exit 0
fi

# Default: start the API
exec python -m uvicorn orchestra.api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 --log-level info
