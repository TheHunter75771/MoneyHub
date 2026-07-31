from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from moneyhub.models import Account, Transaction


def _txn(account_id: int, fitid: str | None, dedup_hash: str) -> Transaction:
    return Transaction(
        account_id=account_id,
        posted_date=date(2026, 7, 15),
        amount_cents=-500,
        description_raw="TEST",
        fitid=fitid,
        dedup_hash=dedup_hash,
    )


def test_fitid_unique_per_account(session):
    session.add(Account(id=1, name="A", kind="checking"))
    session.add(_txn(1, "FIT-1", "h1"))
    session.commit()

    session.add(_txn(1, "FIT-1", "h2"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Same FITID on a different account is fine.
    session.add(Account(id=2, name="B", kind="checking"))
    session.add(_txn(2, "FIT-1", "h3"))
    session.commit()


def test_null_fitid_not_constrained(session):
    """Manual/CSV rows have no FITID; the partial unique index must ignore them."""
    session.add(Account(id=1, name="A", kind="checking"))
    session.add(_txn(1, None, "h1"))
    session.add(_txn(1, None, "h2"))
    session.commit()
    assert session.query(Transaction).count() == 2


def test_foreign_keys_enforced(session):
    session.add(_txn(999, None, "h1"))  # no such account
    with pytest.raises(IntegrityError):
        session.commit()
