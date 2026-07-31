from __future__ import annotations

from datetime import date

import pytest

from moneyhub.models import Account, Category, Transaction
from moneyhub.transfers import pair_transfers, transfer_groups


@pytest.fixture
def world(session):
    spending = Account(id=1, name="Spending", kind="envelope")
    bills = Account(id=2, name="Bills", kind="envelope")
    transfer = Category(id=1, name="Transfer", grp="transfer")
    dining = Category(id=2, name="Dining", grp="want")
    session.add_all([spending, bills, transfer, dining])
    session.commit()
    return session


def _txn(session, account_id, day, cents, desc, category_id=1):
    txn = Transaction(
        account_id=account_id,
        posted_date=date(2026, 7, day),
        amount_cents=cents,
        description_raw=desc,
        category_id=category_id,
        dedup_hash=f"h-{account_id}-{day}-{cents}-{desc}",
    )
    session.add(txn)
    session.commit()
    return txn


def test_pairs_matching_legs_across_accounts(world):
    out = _txn(world, 2, 10, -14625, "Transfer to Spending")
    inn = _txn(world, 1, 10, 14625, "Transfer from Bills")
    result = pair_transfers(world)
    assert result.groups_created == 1
    assert out.transfer_group_id == inn.transfer_group_id is not None
    assert result.unmatched == []

    (group,) = transfer_groups(world)
    assert group["amount_cents"] == 14625
    assert group["from_account_id"] == 2
    assert group["to_account_id"] == 1


def test_same_account_legs_never_pair(world):
    _txn(world, 1, 10, -5000, "out")
    _txn(world, 1, 10, 5000, "in")
    result = pair_transfers(world)
    assert result.groups_created == 0
    assert len(result.unmatched) == 2


def test_window_and_closest_date_wins(world):
    out = _txn(world, 2, 10, -1000, "to spending")
    far = _txn(world, 1, 13, 1000, "from bills (3d)")
    near = _txn(world, 1, 11, 1000, "from bills (1d)")
    pair_transfers(world)
    assert out.transfer_group_id == near.transfer_group_id
    assert far.transfer_group_id is None

    beyond = _txn(world, 2, 20, -700, "lonely")
    _txn(world, 1, 26, 700, "too far (6d)")
    result = pair_transfers(world)
    assert beyond.transfer_group_id is None
    assert any(t.id == beyond.id for t in result.unmatched)


def test_consume_once_with_duplicate_amounts(world):
    _txn(world, 2, 10, -2000, "to spending A")
    _txn(world, 2, 10, -2000, "to spending B")
    _txn(world, 1, 10, 2000, "from bills")
    result = pair_transfers(world)
    assert result.groups_created == 1
    assert len(result.unmatched) == 1


def test_non_transfer_categories_ignored(world):
    _txn(world, 2, 10, -1500, "zaxbys", category_id=2)
    _txn(world, 1, 10, 1500, "refund", category_id=2)
    result = pair_transfers(world)
    assert result.groups_created == 0
    assert result.unmatched == []


def test_pairing_is_idempotent(world):
    _txn(world, 2, 10, -1000, "out")
    _txn(world, 1, 10, 1000, "in")
    first = pair_transfers(world)
    second = pair_transfers(world)
    assert first.groups_created == 1
    assert second.groups_created == 0
    assert len(transfer_groups(world)) == 1
