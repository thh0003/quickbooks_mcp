"""End-to-end check against a QuickBooks Online *sandbox* company.

Creates a 1h30m billable time entry dated today, invoices it, then re-reads the entry
to see whether QBO marked it as billed. Exit codes:

  0  entry ended HasBeenBilled or NotBillable (invoicing removed it from unbilled time)
  1  entry is still Billable: QBO did not honour the TimeActivity link
  2  refused to run (not a sandbox) or a precondition was missing

Usage: uv run python scripts/sandbox_check.py [--customer NAME] [--worker NAME] [--item NAME]
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date

from qbo_mcp import qbo
from qbo_mcp.auth import NotAuthorizedError, TokenStore, build_quickbooks_client
from qbo_mcp.config import ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customer", default="", help="Customer name filter (default: first active customer)")
    parser.add_argument("--worker", default="", help="Worker name filter (default: first employee or vendor)")
    parser.add_argument("--item", default="", help="Service item name filter (default: first service item)")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if settings.environment != "sandbox":
        print(f"refusing to run: QBO_ENVIRONMENT is {settings.environment!r}; this check creates real transactions and only runs against a sandbox.", file=sys.stderr)
        return 2

    try:
        qb = build_quickbooks_client(settings, TokenStore(settings.token_path))
    except NotAuthorizedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    customers = qbo.search_customers(qb, args.customer)
    workers = qbo.search_workers(qb, args.worker)
    items = qbo.search_service_items(qb, args.item)
    for label, found in (("customer", customers), ("worker", workers), ("service item", items)):
        if not found:
            print(f"error: no {label} matched; create one in the sandbox company or adjust the filter.", file=sys.stderr)
            return 2
    customer, worker, item = customers[0], workers[0], items[0]
    print(f"customer={customer.display_name} ({customer.id}) worker={worker.display_name} ({worker.kind} {worker.id}) item={item.name} ({item.id})")

    marker = f"qbo-mcp sandbox check {uuid.uuid4().hex[:8]}"
    entry = qbo.create_time_entry(
        qb,
        worker_kind=worker.kind,
        worker_id=worker.id,
        customer_id=customer.id,
        item_id=item.id,
        txn_date=date.today().isoformat(),
        hours=1,
        minutes=30,
        hourly_rate=item.unit_price or 100.0,
        description=marker,
    )
    print(f"created time entry {entry.id} status={entry.billable_status} hours={entry.hours}:{entry.minutes:02d}")

    from qbo_mcp.server import MARK_NOT_BILLABLE_ON_INVOICE

    today = date.today().isoformat()
    result = qbo.create_invoice_from_time(
        qb, customer_id=customer.id, start_date=today, end_date=today, mark_not_billable_on_invoice=MARK_NOT_BILLABLE_ON_INVOICE
    )
    if isinstance(result, qbo.NoUnbilledTime):
        print("error: the sweep found no unbilled time even though an entry was just created", file=sys.stderr)
        return 1
    print(f"created invoice id={result.invoice_id} doc_number={result.doc_number} total={result.total} lines={len(result.lines)}")
    if entry.id not in {line.time_entry_id for line in result.lines}:
        print(f"error: invoice does not include time entry {entry.id}", file=sys.stderr)
        return 1

    after = qbo.get_time_entry(qb, entry.id)
    print(f"time entry {after.id} billable status after invoicing: {after.billable_status}")
    if after.billable_status in ("HasBeenBilled", "NotBillable"):
        print("OK: entry no longer counts as unbilled time")
        return 0
    print("entry still Billable: QBO did not honour LinkedTxn", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
