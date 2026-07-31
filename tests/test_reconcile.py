from __future__ import annotations

from datetime import date

import pytest

from moneyhub.models import Account, BalanceSnapshot, Transaction
from moneyhub.reconcile import ReconcileError, reconcile


@pytest.fixture
def account(session):
    acct = Account(name="Test Spending", kind="envelope")
    session.add(acct)
    session.commit()
    return acct


def _txn(account_id: int, day: int, cents: int, tag: str) -> Transaction:
    return Transaction(
        account_id=account_id,
        posted_date=date(2026, 7, day),
        amount_cents=cents,
        description_raw=tag,
        dedup_hash=tag,
    )


def _snap(account_id: int, day: int, cents: int, source: str = "manual") -> BalanceSnapshot:
    return BalanceSnapshot(
        account_id=account_id, as_of=date(2026, 7, day), balance_cents=cents, source=source
    )


def test_books_explain_balance(session, account):
    session.add_all(
        [
            _snap(account.id, 7, 10000),
            _txn(account.id, 8, -500, "coffee"),
            _txn(account.id, 10, 1000, "transfer in"),
            _snap(account.id, 10, 10500, source="import"),
        ]
    )
    session.commit()
    report = reconcile(session, account)
    assert report.computed_cents == 10500
    assert report.drift_cents == 0
    assert report.txn_count == 2


def test_drift_surfaces_unrecorded_spending(session, account):
    session.add_all(
        [
            _snap(account.id, 7, 10000),
            _txn(account.id, 8, -500, "coffee"),
            _snap(account.id, 10, 2300, source="import"),  # reality says less
        ]
    )
    session.commit()
    report = reconcile(session, account)
    assert report.drift_cents == -7200  # the $72 that was never recorded


def test_stated_balance_override(session, account):
    session.add_all([_snap(account.id, 7, 10000), _txn(account.id, 8, -500, "coffee")])
    session.commit()
    report = reconcile(session, account, stated_cents=9600, stated_as_of=date(2026, 7, 9))
    assert report.drift_cents == 100


def test_window_excludes_prior_day_and_includes_stated_day(session, account):
    session.add_all(
        [
            _snap(account.id, 7, 10000),
            _txn(account.id, 7, -99999, "already inside prior snapshot"),
            _txn(account.id, 10, -500, "on stated day"),
            _snap(account.id, 10, 9500, source="import"),
        ]
    )
    session.commit()
    report = reconcile(session, account)
    assert report.txn_count == 1
    assert report.drift_cents == 0


def test_stated_needs_a_date(session, account):
    session.add(_snap(account.id, 7, 10000))
    session.commit()
    with pytest.raises(ReconcileError, match="needs a date"):
        reconcile(session, account, stated_cents=5000)


def test_requires_prior_snapshot(session, account):
    session.add(_snap(account.id, 7, 10000))
    session.commit()
    with pytest.raises(ReconcileError, match="no snapshot earlier"):
        reconcile(session, account)
