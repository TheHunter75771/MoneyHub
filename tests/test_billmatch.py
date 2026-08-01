from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from moneyhub.billmatch import auto_check_bills
from moneyhub.models import Account, Bill, BillPayment, Transaction


@pytest.fixture
def world(session):
    session.add(Account(id=1, name="Spending", kind="envelope"))
    session.add_all(
        [
            Bill(id=1, name="Car loan", amount_cents=21000, due_rule="15th",
                 match_pattern="nfcu ach loan-recur"),
            Bill(id=2, name="Claude", amount_cents=10000, due_rule="25th",
                 match_pattern="anthropic"),
            Bill(id=3, name="Manual only", amount_cents=5000, due_rule="1st"),  # no pattern
        ]
    )
    session.commit()
    return session


def _txn(session, day, cents, desc, month=7):
    txn = Transaction(
        account_id=1,
        posted_date=date(2026, month, day),
        amount_cents=cents,
        description_raw=desc,
        dedup_hash=f"h-{month}-{day}-{cents}-{desc}",
    )
    session.add(txn)
    session.commit()
    return txn


def test_matches_pattern_amount_and_period(world):
    txn = _txn(world, 13, -21000, "External Withdrawal NFCU ACH LOAN-RECUR - PAYMENT")
    created = auto_check_bills(world, periods=["2026-07"])
    assert [c["bill"] for c in created] == ["Car loan"]
    payment = world.scalars(select(BillPayment)).one()
    assert payment.period == "2026-07"
    assert payment.transaction_id == txn.id


def test_amount_tolerance_covers_tax_drift(world):
    _txn(world, 26, -10600, "ANTHROPIC* CLAUDE SANTHROPIC.COM")  # $106 vs $100 bill
    created = auto_check_bills(world, periods=["2026-07"])
    assert [c["bill"] for c in created] == ["Claude"]


def test_amount_too_far_off_does_not_match(world):
    _txn(world, 26, -30000, "ANTHROPIC* CLAUDE")  # 3x the bill
    assert auto_check_bills(world, periods=["2026-07"]) == []


def test_wrong_period_and_inflows_ignored(world):
    _txn(world, 13, -21000, "NFCU ACH LOAN-RECUR", month=6)
    _txn(world, 13, 21000, "NFCU ACH LOAN-RECUR refund")
    assert auto_check_bills(world, periods=["2026-07"]) == []


def test_existing_payment_and_used_txn_respected(world):
    txn = _txn(world, 13, -21000, "NFCU ACH LOAN-RECUR")
    auto_check_bills(world, periods=["2026-07"])
    # Second run: nothing new, no duplicate payment, txn not reused.
    assert auto_check_bills(world, periods=["2026-07"]) == []
    assert world.query(BillPayment).count() == 1
    assert world.scalars(select(BillPayment)).one().transaction_id == txn.id


def test_transfer_legs_never_satisfy_bills(world):
    from moneyhub.models import Category

    world.add(Category(id=9, name="Transfer", grp="transfer"))
    world.commit()
    txn = _txn(world, 13, -21000, "Transfer to Bills - NFCU ACH LOAN-RECUR staging")
    txn.category_id = 9
    world.commit()
    assert auto_check_bills(world, periods=["2026-07"]) == []


def test_patternless_bills_never_auto_check(world):
    _txn(world, 1, -5000, "Manual only lookalike")
    assert auto_check_bills(world, periods=["2026-07"]) == []
