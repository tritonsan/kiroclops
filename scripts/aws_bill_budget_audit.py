"""Capture AWS billing and budget evidence using read-only AWS CLI calls."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="Exclusive Cost Explorer end date")
    parser.add_argument("--recent-start", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    checked = datetime.now(timezone.utc).isoformat()

    def query(name: str, *command: str) -> dict:
        try:
            proc = subprocess.run(
                ["aws", *command, "--region", "us-east-1", "--output", "json",
                 "--no-cli-pager", "--cli-connect-timeout", "5", "--cli-read-timeout", "20"],
                env={**os.environ, "AWS_MAX_ATTEMPTS": "2", "AWS_PAGER": "", "PYTHONUTF8": "1"},
                capture_output=True, text=True, encoding="utf-8", timeout=55,
            )
            data = json.loads(proc.stdout) if proc.returncode == 0 else {
                "error": proc.stderr.strip(), "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            data = {"error": "AWS CLI request timed out", "timeout_seconds": 55}
        (args.output / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    common_cost = ("--metrics", "UnblendedCost", "--group-by",
                   "Type=DIMENSION,Key=SERVICE", "Type=DIMENSION,Key=RECORD_TYPE")
    requests = {
        "identity": ("sts", "get-caller-identity"),
        "budgets": ("budgets", "describe-budgets", "--account-id", args.account),
        "monthly-costs": ("ce", "get-cost-and-usage", "--time-period",
                          f"Start={args.start},End={args.end}", "--granularity", "MONTHLY", *common_cost),
        "recent-daily-costs": ("ce", "get-cost-and-usage", "--time-period",
                              f"Start={args.recent_start},End={args.end}", "--granularity", "DAILY", *common_cost),
    }
    # Invoicing time-interval filters allow at most one month. Query billing
    # periods separately, including the current provisional period.
    year, month = map(int, args.start[:7].split("-"))
    last_period = datetime.now(timezone.utc).strftime("%Y-%m")
    while f"{year:04d}-{month:02d}" <= min(args.end[:7], last_period):
        name = f"invoices-{year:04d}-{month:02d}"
        invoice_input = args.output / f"{name}-request.json"
        invoice_input.write_text(json.dumps({
            "Selector": {"ResourceType": "ACCOUNT_ID", "Value": args.account},
            "Filter": {"BillingPeriod": {"Month": month, "Year": year}},
            "MaxResults": 100,
        }), encoding="utf-8")
        requests[name] = ("invoicing", "list-invoice-summaries", "--cli-input-json",
                          "file://" + str(invoice_input.resolve()))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(query, name, *command) for name, command in requests.items()}
        results = {name: future.result() for name, future in futures.items()}
    for index, budget in enumerate(results["budgets"].get("Budgets", [])):
        name = budget["BudgetName"]
        query(f"budget-{index}-notifications", "budgets", "describe-notifications-for-budget",
              "--account-id", args.account, "--budget-name", name)
        query(f"budget-{index}-history", "budgets", "describe-budget-performance-history",
              "--account-id", args.account, "--budget-name", name, "--time-period",
              f"Start={args.start}T00:00:00Z,End={checked}")
    (args.output / "audit-meta.json").write_text(json.dumps({
        "checked_at": checked, "account": args.account, "start": args.start,
        "end_exclusive": args.end, "scope": "Account-wide, read-only",
        "warning": "Cost Explorer estimated costs are not finalized invoice or payment evidence.",
    }, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()),
                      "requests": {name: {"error": data.get("error"),
                                          "keys": list(data)} for name, data in results.items()}}, indent=2))


if __name__ == "__main__":
    main()
