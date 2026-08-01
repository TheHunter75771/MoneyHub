"""Paycheck detection.

The payday screen used to default to a hard-coded $960. Real military pay
moves (base pay changes, allotments, mid-month vs end-of-month splits), so
the deposit is detected from the ledger instead: inflows on an income
category whose text looks like a paycheck.

'Typical' is the MEDIAN of recent paychecks, not the mean — one $1,539
month (back pay, travel claim) shouldn't drag the planning number up.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Category, Transaction

# Substrings that mark a real paycheck rather than any income (dividends,
# refunds, reimbursements). Lowercase; matched against merchant + raw text.
PAYCHECK_PATTERNS = ("treas 310", "dfas", "mctf pay", "payroll")
LOOKBACK_DAYS = 120
MIN_PAYCHECK_CENTS = 10000  # $100 — filters dividend pennies


@dataclass
class Paycheck:
    id: int
    date: date
    amount_cents: int
    account: str
    description: str


def _looks_like_paycheck(txn: Transaction) -> bool:
    text = f"{txn.merchant_norm} {txn.description_raw}".lower()
    return any(p in text for p in PAYCHECK_PATTERNS)


def recent_paychecks(
    session: Session, today: date | None = None, lookback_days: int = LOOKBACK_DAYS
) -> list[Paycheck]:
    """Paycheck deposits, newest first."""
    today = today or date.today()
    income_ids = select(Category.id).where(Category.grp == "income")
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    rows = session.scalars(
        select(Transaction)
        .where(
            Transaction.amount_cents >= MIN_PAYCHECK_CENTS,
            Transaction.posted_date >= today - timedelta(days=lookback_days),
            Transaction.posted_date <= today,
            Transaction.category_id.in_(income_ids),
        )
        .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
    ).all()
    return [
        Paycheck(
            id=t.id,
            date=t.posted_date,
            amount_cents=t.amount_cents,
            account=accounts.get(t.account_id, "?"),
            description=t.merchant_norm or t.description_raw,
        )
        for t in rows
        if _looks_like_paycheck(t)
    ]


def median_cents(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def paycheck_summary(session: Session, today: date | None = None) -> dict:
    """What the payday screen needs to pre-fill itself honestly."""
    today = today or date.today()
    checks = recent_paychecks(session, today)
    latest = checks[0] if checks else None
    typical = median_cents([c.amount_cents for c in checks[:6]])
    return {
        "detected_cents": latest.amount_cents if latest else None,
        "detected_date": latest.date.isoformat() if latest else None,
        "detected_description": latest.description if latest else None,
        "detected_days_ago": (today - latest.date).days if latest else None,
        "typical_cents": typical,
        "recent": [
            {
                "date": c.date.isoformat(),
                "amount_cents": c.amount_cents,
                "account": c.account,
                "description": c.description,
            }
            for c in checks[:6]
        ],
    }
