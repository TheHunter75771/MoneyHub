"""Auto-check bills from imported transactions.

A bill with a match_pattern gets checked off automatically when a
transaction lands whose text contains the pattern, whose amount is within
tolerance of the bill amount, and whose posted date falls in the period.
The created BillPayment records WHICH transaction satisfied it, so the UI
can show a "matched to txn" hint instead of a bare checkbox.

Notes:
- Amount tolerance is ±25%: real charges drift from the planned amount
  (a $100 subscription bills $106 with tax; insurance premiums wobble).
- Each transaction can satisfy at most one bill, ever.
- Unchecking an auto-matched bill is not sticky: if the matching
  transaction exists, the next run will re-check it — the ledger wins.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Bill, BillPayment, Category, Transaction
from .reports import month_window

AMOUNT_TOLERANCE = 0.25


def _matches(bill: Bill, txn: Transaction) -> bool:
    if txn.amount_cents >= 0:
        return False
    pattern = bill.match_pattern.strip().lower()
    if not pattern:
        return False
    haystack = f"{txn.merchant_norm} {txn.description_raw}".lower()
    if pattern not in haystack:
        return False
    low = bill.amount_cents * (1 - AMOUNT_TOLERANCE)
    high = bill.amount_cents * (1 + AMOUNT_TOLERANCE)
    return low <= -txn.amount_cents <= high


def auto_check_bills(session: Session, periods: list[str] | None = None) -> list[dict]:
    """Create BillPayments for matched transactions. Returns what was checked."""
    if periods is None:
        today = date.today()
        prev = today.replace(day=1) - timedelta(days=1)
        periods = [prev.strftime("%Y-%m"), today.strftime("%Y-%m")]

    used_txn_ids = set(
        session.scalars(
            select(BillPayment.transaction_id).where(BillPayment.transaction_id.is_not(None))
        )
    )
    bills = session.scalars(
        select(Bill).where(Bill.active.is_(True), Bill.match_pattern != "")
    ).all()

    created: list[dict] = []
    for period in periods:
        year, mon = (int(p) for p in period.split("-"))
        start, end = month_window(year, mon)
        paid_ids = set(
            session.scalars(select(BillPayment.bill_id).where(BillPayment.period == period))
        )
        transfer_cat_ids = select(Category.id).where(Category.grp == "transfer")
        txns = session.scalars(
            select(Transaction)
            .where(
                Transaction.posted_date >= start,
                Transaction.posted_date < end,
                Transaction.amount_cents < 0,
                # A bill is paid by the real outflow, never by the internal
                # transfer that staged the money for it.
                Transaction.category_id.is_(None)
                | ~Transaction.category_id.in_(transfer_cat_ids),
            )
            .order_by(Transaction.posted_date)
        ).all()
        for bill in bills:
            if bill.id in paid_ids:
                continue
            match = next(
                (t for t in txns if t.id not in used_txn_ids and _matches(bill, t)), None
            )
            if match is None:
                continue
            session.add(
                BillPayment(bill_id=bill.id, period=period, transaction_id=match.id)
            )
            used_txn_ids.add(match.id)
            paid_ids.add(bill.id)
            created.append(
                {
                    "bill": bill.name,
                    "period": period,
                    "txn_id": match.id,
                    "txn": match.description_raw,
                    "date": match.posted_date.isoformat(),
                }
            )
    session.commit()
    return created
