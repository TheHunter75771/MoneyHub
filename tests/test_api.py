from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from moneyhub.api import create_app
from moneyhub.db import Base, make_engine, make_session_factory
from moneyhub.models import Account, BalanceSnapshot, Category, Transaction


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "api.db"
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        spending = Account(id=1, name="SCU Spending", institution="Service CU", kind="envelope")
        dining = Category(id=1, name="Dining", grp="want", monthly_budget_cents=10000)
        transfer = Category(id=2, name="Transfer", grp="transfer")
        income = Category(id=3, name="Income", grp="income")
        s.add_all([spending, dining, transfer, income])
        s.add_all(
            [
                Transaction(
                    id=1,
                    account_id=1,
                    posted_date=date(2026, 7, 10),
                    amount_cents=-4000,
                    description_raw="ZAXBY'S #12101",
                    category_id=1,
                    category_source="manual",
                    dedup_hash="h1",
                    fitid="sfin:T1",
                ),
                Transaction(
                    id=2,
                    account_id=1,
                    posted_date=date(2026, 7, 11),
                    amount_cents=-2500,
                    description_raw="MYSTERY VENDOR 12",
                    dedup_hash="h2",
                ),
                Transaction(
                    id=3,
                    account_id=1,
                    posted_date=date(2026, 7, 12),
                    amount_cents=-2500,
                    description_raw="MYSTERY VENDOR 99",
                    dedup_hash="h3",
                ),
            ]
        )
        s.add(
            BalanceSnapshot(
                account_id=1, as_of=date(2026, 7, 12), balance_cents=12818, source="import"
            )
        )
        s.commit()
    return TestClient(create_app(db_path))


def test_state_reports_badge_and_months(client):
    body = client.get("/api/state").json()
    assert body["inbox_count"] == 2
    assert body["months"] == ["2026-07"]


def test_budget_math_and_rows(client):
    body = client.get("/api/budget", params={"month": "2026-07"}).json()
    assert body["spending_cents"] == 9000
    dining = next(r for r in body["categories"] if r["name"] == "Dining")
    assert dining["spent_cents"] == 4000
    assert dining["remaining_cents"] == 6000
    assert not dining["over"]
    # transfer/income groups never appear as budget rows
    assert all(r["name"] not in ("Transfer", "Income") for r in body["categories"])


def test_cap_edit_persists(client):
    r = client.patch("/api/categories/1", json={"cap_cents": 3000})
    assert r.status_code == 200
    dining = next(
        r for r in client.get("/api/budget", params={"month": "2026-07"}).json()["categories"]
        if r["name"] == "Dining"
    )
    assert dining["cap_cents"] == 3000
    assert dining["over"] is True


def test_transactions_filtering(client):
    all_rows = client.get("/api/transactions").json()
    assert all_rows["total"] == 3
    uncat = client.get("/api/transactions", params={"uncategorized": True}).json()
    assert uncat["total"] == 2
    q = client.get("/api/transactions", params={"q": "zaxby"}).json()
    assert q["total"] == 1
    assert q["rows"][0]["source"] == "sync"


def test_manual_categorization_is_sticky(client):
    r = client.patch(
        "/api/transactions/2", json={"set_category": True, "category_id": 1}
    )
    assert r.status_code == 200
    row = client.get("/api/transactions", params={"q": "MYSTERY VENDOR 12"}).json()["rows"][0]
    assert row["category_id"] == 1
    assert row["source"] == "manual"  # no import batch and no fitid -> manual


def test_inbox_assign_creates_rule_and_categorizes(client):
    inbox = client.get("/api/inbox").json()
    group = next(g for g in inbox["groups"] if g["key"].startswith("MYSTERY"))
    r = client.post(
        "/api/inbox/assign",
        json={
            "txn_ids": group["txn_ids"],
            "key": "mystery vendor",
            "category_id": 1,
            "create_rule": True,
        },
    )
    assert r.status_code == 200 and r.json()["rule_id"] is not None
    assert client.get("/api/state").json()["inbox_count"] == 0


def test_add_manual_and_merge_flow(client):
    r = client.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "amount_cents": -4000,
            "posted_date": "2026-07-11",
            "description": "cash zaxbys with the squad",
        },
    )
    assert r.status_code == 201
    merges = client.get("/api/inbox").json()["merges"]
    assert len(merges) == 1
    manual_id = merges[0]["manual_id"]

    # dismiss keeps both and stops it reappearing
    client.post("/api/merges/resolve", json={"manual_id": manual_id, "action": "dismiss"})
    assert client.get("/api/inbox").json()["merges"] == []


def test_merge_approve_deletes_manual(client):
    client.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "amount_cents": -4000,
            "posted_date": "2026-07-11",
            "description": "cash zaxbys",
        },
    )
    manual_id = client.get("/api/inbox").json()["merges"][0]["manual_id"]
    client.post("/api/merges/resolve", json={"manual_id": manual_id, "action": "approve"})
    assert client.get("/api/transactions", params={"q": "squad"}).json()["total"] == 0
    assert client.get("/api/inbox").json()["merges"] == []


def test_rule_endpoint_applies_immediately(client):
    r = client.post("/api/rules", json={"match_text": "mystery", "category_id": 1})
    assert r.status_code == 201
    assert r.json()["categorized"] >= 2
    assert client.get("/api/state").json()["inbox_count"] == 0


def test_accounts_endpoint_shape(client):
    (acct,) = client.get("/api/accounts").json()
    assert acct["name"] == "SCU Spending"
    assert acct["balance_cents"] == 12818
