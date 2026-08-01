from __future__ import annotations

from datetime import date

import pytest

from moneyhub.models import Account, Category, Transaction
from moneyhub.payday import median_cents, paycheck_summary, recent_paychecks


@pytest.fixture
def world(session):
    session.add_all(
        [
            Account(id=1, name="SCU Paycheck Holdings", kind="envelope"),
            Category(id=1, name="Income", grp="income"),
            Category(id=2, name="Dining", grp="want"),
        ]
    )
    session.commit()
    return session


def _txn(session, day, cents, desc, month=7, category_id=1):
    txn = Transaction(
        account_id=1,
        posted_date=date(2026, month, day),
        amount_cents=cents,
        description_raw=desc,
        category_id=category_id,
        dedup_hash=f"h-{month}-{day}-{cents}-{desc}",
    )
    session.add(txn)
    session.commit()
    return txn


def test_detects_treas_310_and_ignores_other_income(world):
    _txn(world, 27, 153919, "MCTF TREAS 310  -   MCTF PAY")
    _txn(world, 9, 96295, "MCTF TREAS 310  -   MCTF PAY")
    _txn(world, 30, 73, "Credit Dividends")  # income, but not a paycheck
    _txn(world, 5, 17500, "External Deposit ERAU ERAU WEBACH")  # refund
    checks = recent_paychecks(world, today=date(2026, 7, 31))
    assert [c.amount_cents for c in checks] == [153919, 96295]


def test_spending_and_uncategorized_never_count(world):
    _txn(world, 20, -5000, "MCTF TREAS 310 refund reversal")
    _txn(world, 21, 95000, "MCTF TREAS 310 uncategorized", category_id=None)
    assert recent_paychecks(world, today=date(2026, 7, 31)) == []


def test_median_resists_a_big_outlier(world):
    for day, cents in [(9, 91294), (11, 95387), (25, 91292), (27, 153919)]:
        _txn(world, day, cents, "MCTF TREAS 310", month=6 if day < 20 else 7)
    summary = paycheck_summary(world, today=date(2026, 7, 31))
    assert summary["detected_cents"] == 153919  # newest
    assert summary["typical_cents"] == 93340  # median of the four, not the mean
    assert summary["detected_days_ago"] == 4
    assert len(summary["recent"]) == 4


def test_summary_empty_when_no_paychecks(world):
    summary = paycheck_summary(world, today=date(2026, 7, 31))
    assert summary["detected_cents"] is None
    assert summary["typical_cents"] is None
    assert summary["recent"] == []


def test_lookback_window_excludes_old_checks(world):
    _txn(world, 15, 90000, "MCTF TREAS 310", month=1)
    assert recent_paychecks(world, today=date(2026, 7, 31)) == []


@pytest.mark.parametrize(
    ("values", "expected"),
    [([], None), ([5], 5), ([1, 3], 2), ([1, 2, 3], 2), ([10, 20, 30, 100], 25)],
)
def test_median(values, expected):
    assert median_cents(values) == expected
