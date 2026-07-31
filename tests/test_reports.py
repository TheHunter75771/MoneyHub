from __future__ import annotations

from datetime import date

import pytest

from moneyhub.models import Account, Category, Transaction
from moneyhub.reports import budget_report, inbox, merchant_key, month_window


@pytest.fixture
def world(session):
    account = Account(name="Spending", kind="envelope")
    dining = Category(name="Dining", grp="want", monthly_budget_cents=10000)
    transfer = Category(name="Transfer", grp="transfer")
    income = Category(name="Income", grp="income")
    session.add_all([account, dining, transfer, income])
    session.commit()
    return {"account": account, "dining": dining, "transfer": transfer, "income": income}


def _txn(session, world, day, cents, category=None, desc="x", month=7):
    txn = Transaction(
        account_id=world["account"].id,
        posted_date=date(2026, month, day),
        amount_cents=cents,
        description_raw=desc,
        category_id=world[category].id if category else None,
        dedup_hash=f"h-{month}-{day}-{cents}-{desc}",
    )
    session.add(txn)
    session.commit()
    return txn


def test_month_window_december():
    assert month_window(2026, 12) == (date(2026, 12, 1), date(2027, 1, 1))


def test_budget_report_math(session, world):
    _txn(session, world, 2, -4000, "dining")
    _txn(session, world, 3, -7000, "dining")
    _txn(session, world, 4, 50000, "income")
    _txn(session, world, 5, -20000, "transfer", desc="Transfer to Bills")
    _txn(session, world, 6, -1234, desc="mystery")
    _txn(session, world, 1, -99999, "dining", month=6)  # outside the window

    report = budget_report(session, 2026, 7)
    dining = next(line for line in report.lines if line.category == "Dining")
    assert dining.spent_cents == 11000
    assert dining.remaining_cents == -1000  # over budget
    assert report.income_cents == 50000
    assert report.spending_cents == 11000 + 1234  # transfer excluded
    assert report.transfer_volume_cents == 20000
    assert report.uncategorized_count == 1
    assert report.uncategorized_cents == -1234
    assert all(line.category != "Transfer" for line in report.lines)


def test_pending_rows_are_excluded(session, world):
    txn = _txn(session, world, 2, -4000, "dining")
    txn.status = "pending"
    session.commit()
    report = budget_report(session, 2026, 7)
    assert report.spending_cents == 0


def test_inbox_groups_by_merchant(session, world):
    _txn(session, world, 2, -500, desc="POS Withdrawal COFFEE HUT #9 BEAUFORT SCUS")
    _txn(session, world, 3, -600, desc="POS Withdrawal COFFEE HUT #12 BEAUFORT SCUS")
    _txn(session, world, 4, -999, desc="Something Else Entirely")
    _txn(session, world, 5, -1, "dining")  # categorized: not in inbox

    groups, total = inbox(session)
    assert total == 3
    coffee = next(g for g in groups if g.key.startswith("COFFEE"))
    assert coffee.count == 2
    assert coffee.net_cents == -1100


def test_merchant_key_prefers_merchant_norm(session, world):
    txn = _txn(session, world, 2, -500, desc="POS Withdrawal WHATEVER 123")
    txn.merchant_norm = "Zaxby's"
    assert merchant_key(txn) == "ZAXBY'S"
