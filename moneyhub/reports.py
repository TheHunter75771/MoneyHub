"""Budget and inbox reports.

All spending math excludes categories in the 'transfer' group — movement
between owned accounts is never income or spending.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, Transaction


def month_window(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


@dataclass
class BudgetLine:
    category: str
    grp: str
    budget_cents: int | None
    net_cents: int  # signed sum for the month; spending is negative

    @property
    def spent_cents(self) -> int:
        return -self.net_cents if self.net_cents < 0 else 0

    @property
    def remaining_cents(self) -> int | None:
        if self.budget_cents is None:
            return None
        return self.budget_cents - self.spent_cents


@dataclass
class BudgetReport:
    year: int
    month: int
    lines: list[BudgetLine]
    uncategorized_cents: int
    uncategorized_count: int
    transfer_volume_cents: int  # gross transfer movement, for visibility
    income_cents: int
    spending_cents: int  # all non-transfer outflows, categorized or not


def budget_report(session: Session, year: int, month: int) -> BudgetReport:
    start, end = month_window(year, month)
    txns = session.scalars(
        select(Transaction).where(
            Transaction.posted_date >= start,
            Transaction.posted_date < end,
            Transaction.status == "posted",
        )
    ).all()
    categories = {c.id: c for c in session.scalars(select(Category))}

    by_category: dict[int, int] = {}
    uncategorized = uncategorized_count = transfer_volume = income = spending = 0
    for t in txns:
        category = categories.get(t.category_id) if t.category_id else None
        if category is not None and category.grp == "transfer":
            transfer_volume += abs(t.amount_cents)
            continue
        if t.amount_cents > 0:
            income += t.amount_cents
        else:
            spending += -t.amount_cents
        if category is None:
            uncategorized += t.amount_cents
            uncategorized_count += 1
        else:
            by_category[category.id] = by_category.get(category.id, 0) + t.amount_cents

    lines = [
        BudgetLine(
            category=categories[cid].name,
            grp=categories[cid].grp,
            budget_cents=categories[cid].monthly_budget_cents,
            net_cents=net,
        )
        for cid, net in by_category.items()
    ]
    lines.sort(key=lambda line: line.net_cents)
    return BudgetReport(
        year=year,
        month=month,
        lines=lines,
        uncategorized_cents=uncategorized,
        uncategorized_count=uncategorized_count,
        transfer_volume_cents=transfer_volume,
        income_cents=income,
        spending_cents=spending,
    )


_NOISE = re.compile(
    r"^(POS Withdrawal|POS Deposit|Point Of Sale Deposit|External Withdrawal|"
    r"External Deposit|Deposit|Withdrawal|ACH)\s+",
    re.IGNORECASE,
)
_STORE_NUM = re.compile(r"\s*[#*]\d+\b|\s+\d{3,}\b")


def merchant_key(txn: Transaction) -> str:
    """Grouping key for the inbox: normalized-ish merchant name."""
    base = txn.merchant_norm or _NOISE.sub("", txn.description_raw)
    base = _STORE_NUM.sub("", base)
    words = base.split()
    return " ".join(words[:3]).upper() if words else "(blank)"


@dataclass
class InboxGroup:
    key: str
    count: int
    net_cents: int
    sample: str  # one raw description for context


def inbox(session: Session, limit: int = 40) -> tuple[list[InboxGroup], int]:
    """Uncategorized transactions grouped by merchant, biggest money first."""
    txns = session.scalars(
        select(Transaction).where(Transaction.category_id.is_(None))
    ).all()
    groups: dict[str, InboxGroup] = {}
    for t in txns:
        key = merchant_key(t)
        g = groups.get(key)
        if g is None:
            groups[key] = InboxGroup(
                key=key, count=1, net_cents=t.amount_cents, sample=t.description_raw[:60]
            )
        else:
            g.count += 1
            g.net_cents += t.amount_cents
    ordered = sorted(groups.values(), key=lambda g: -abs(g.net_cents))
    return ordered[:limit], len(txns)
