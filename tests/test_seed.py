from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from moneyhub.models import Account, BalanceSnapshot, Bill, Category, Debt, Transaction
from moneyhub.money import to_cents
from moneyhub.seed import SeedError, seed_from_json

EXAMPLE = Path(__file__).parent.parent / "examples" / "seed.example.json"


@pytest.fixture
def data() -> dict:
    return json.loads(EXAMPLE.read_text())


def test_seed_counts_match_input(session, data):
    counts = seed_from_json(session, data)
    assert counts["accounts"] == len(data["accounts"])
    assert counts["categories"] == len(data["categories"])
    assert counts["bills"] == len(data["bills"])
    assert counts["debts"] == len(data["debts"])
    assert counts["purchases"] == len(data["purchases"])
    assert counts["snapshots"] == sum(1 for a in data["accounts"] if a["balance"] is not None)


def test_seed_money_survives_round_trip(session, data):
    seed_from_json(session, data)

    expected_debt = sum(to_cents(d["balance"]) for d in data["debts"])
    assert sum(d.balance_cents for d in session.scalars(select(Debt))) == expected_debt

    expected_spend = -sum(to_cents(p["amount"]) for p in data["purchases"])
    assert sum(t.amount_cents for t in session.scalars(select(Transaction))) == expected_spend

    snapshots = {s.account_id: s.balance_cents for s in session.scalars(select(BalanceSnapshot))}
    for raw in data["accounts"]:
        account = session.scalars(select(Account).where(Account.name == raw["name"])).one()
        if raw["balance"] is None:
            assert account.id not in snapshots
        else:
            assert snapshots[account.id] == to_cents(raw["balance"])


def test_seed_resolves_category_aliases(session, data):
    seed_from_json(session, data)
    aliased = data["category_aliases"]  # e.g. legacy "Food" -> "Groceries"
    for legacy_name, real_name in aliased.items():
        category = session.scalars(select(Category).where(Category.name == real_name)).one()
        purchases = [p for p in data["purchases"] if p["cat"] == legacy_name]
        if purchases:
            rows = session.scalars(
                select(Transaction).where(Transaction.category_id == category.id)
            ).all()
            assert len(rows) >= len(purchases)


def test_seed_marks_undated_purchases(session, data):
    seed_from_json(session, data)
    undated = [p for p in data["purchases"] if not p["date"]]
    flagged = session.scalars(
        select(Transaction).where(Transaction.notes != "")
    ).all()
    assert len(flagged) == len(undated)


def test_seed_links_bills_to_categories(session, data):
    seed_from_json(session, data)
    for bill in session.scalars(select(Bill)):
        assert bill.category_id is not None


def test_seed_refuses_to_run_twice(session, data):
    seed_from_json(session, data)
    with pytest.raises(SeedError):
        seed_from_json(session, data)


def test_seed_rejects_unknown_category(session, data):
    data["purchases"][0]["cat"] = "Nonexistent"
    with pytest.raises(SeedError):
        seed_from_json(session, data)
