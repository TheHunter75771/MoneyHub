from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moneyhub.api import create_app
from moneyhub.db import Base, make_engine, make_session_factory
from moneyhub.models import Account, Bill, Category, Debt

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "api.db"
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        s.add_all(
            [
                Account(id=1, name="Test Spending", institution="Service CU", kind="envelope"),
                Category(id=1, name="Storage locker", grp="need"),
                Bill(id=1, name="Storage locker", amount_cents=5000, due_rule="1st", category_id=1),
                Bill(id=2, name="Car loan", amount_cents=21000, due_rule="15th"),
                Bill(id=3, name="Haircut", amount_cents=2800, due_rule="biweekly"),
                Debt(id=1, name="Car loan", balance_cents=531572),
                Debt(id=2, name="OpenSky", balance_cents=0, status="paid_off"),
            ]
        )
        s.commit()
    return TestClient(create_app(db_path))


# --- bills -------------------------------------------------------------


def test_bills_checklist_and_check_assignment(client):
    body = client.get("/api/bills", params={"period": "2026-08"}).json()
    by_name = {r["name"]: r for r in body["rows"]}
    assert by_name["Storage locker"]["check"] == "1st"
    assert by_name["Car loan"]["check"] == "15th"
    assert by_name["Haircut"]["check"] == "—"
    assert body["paid_cents"] == 0
    assert body["remaining_cents"] == 5000 + 21000 + 2800


def test_bill_paid_toggle_is_per_period(client):
    client.post("/api/bills/1/paid", json={"period": "2026-08", "paid": True})
    aug = client.get("/api/bills", params={"period": "2026-08"}).json()
    sep = client.get("/api/bills", params={"period": "2026-09"}).json()
    assert next(r for r in aug["rows"] if r["id"] == 1)["paid"] is True
    assert aug["paid_cents"] == 5000
    assert next(r for r in sep["rows"] if r["id"] == 1)["paid"] is False

    # toggling twice is idempotent; unchecking removes the payment
    client.post("/api/bills/1/paid", json={"period": "2026-08", "paid": True})
    client.post("/api/bills/1/paid", json={"period": "2026-08", "paid": False})
    aug = client.get("/api/bills", params={"period": "2026-08"}).json()
    assert next(r for r in aug["rows"] if r["id"] == 1)["paid"] is False


def test_bill_deactivate_hides(client):
    client.patch("/api/bills/3", json={"active": False})
    rows = client.get("/api/bills", params={"period": "2026-08"}).json()["rows"]
    assert all(r["id"] != 3 for r in rows)


def test_bill_add(client):
    r = client.post(
        "/api/bills",
        json={"name": "Rent", "amount_cents": 145000, "due_rule": "1st", "category_id": 1},
    )
    assert r.status_code == 201
    rows = client.get("/api/bills", params={"period": "2026-08"}).json()["rows"]
    assert any(r["name"] == "Rent" and r["category"] == "Storage locker" for r in rows)


# --- debts -------------------------------------------------------------


def test_debts_total_excludes_paid_off(client):
    body = client.get("/api/debts").json()
    assert body["total_cents"] == 531572


def test_debt_update_and_payoff(client):
    client.patch("/api/debts/1", json={"balance_cents": 500000})
    client.patch("/api/debts/1", json={"status": "paid_off"})
    body = client.get("/api/debts").json()
    assert body["total_cents"] == 0
    row = next(r for r in body["rows"] if r["id"] == 1)
    assert row["balance_cents"] == 500000 and row["status"] == "paid_off"


# --- admin -------------------------------------------------------------


def test_account_add_and_deactivate(client):
    r = client.post("/api/accounts", json={"name": "Cash", "kind": "checking"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    client.patch(f"/api/accounts/{new_id}", json={"is_active": False})
    assert all(a["name"] != "Cash" for a in client.get("/api/accounts").json())


def test_rules_lifecycle_with_hit_counts(client):
    with_txn = client.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "amount_cents": -5000,
            "posted_date": "2026-08-01",
            "description": "STORAGE PLACE 42",
        },
    )
    assert with_txn.status_code == 201
    client.post("/api/rules", json={"match_text": "storage", "category_id": 1})

    (rule,) = client.get("/api/rules").json()
    assert rule["hits"] == 1
    assert "storage" in rule["conditions"]

    r = client.patch(f"/api/rules/{rule['id']}", json={"active": False})
    assert r.status_code == 200
    assert client.get("/api/state").json()["inbox_count"] == 1  # released

    r = client.delete(f"/api/rules/{rule['id']}")
    assert r.status_code == 200
    assert client.get("/api/rules").json() == []


def test_snapshot_and_reconcile_report(client):
    client.post(
        "/api/snapshots",
        json={"account_id": 1, "as_of": "2026-07-31", "balance_cents": 10000},
    )
    client.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "amount_cents": -2500,
            "posted_date": "2026-08-02",
            "description": "spend",
        },
    )
    client.post(
        "/api/snapshots",
        json={"account_id": 1, "as_of": "2026-08-03", "balance_cents": 7500},
    )
    (row,) = client.get("/api/reconcile").json()
    assert row["computed_cents"] == 7500
    assert row["drift_cents"] == 0


def test_csv_upload_roundtrip(client):
    with (FIXTURES / "scu_a.csv").open("rb") as f:
        r = client.post(
            "/api/import",
            files={"file": ("scu_a.csv", f, "text/csv")},
            data={"account_id": "1"},
        )
    assert r.status_code == 200
    (section,) = r.json()["sections"]
    assert section["new"] == 5
    batches = client.get("/api/imports").json()
    assert batches[0]["filename"] == "scu_a.csv"

    # same file again is refused with a clear error
    with (FIXTURES / "scu_a.csv").open("rb") as f:
        r = client.post(
            "/api/import",
            files={"file": ("scu_a.csv", f, "text/csv")},
            data={"account_id": "1"},
        )
    assert r.status_code == 422
    assert "already imported" in r.json()["detail"]


def test_stale_threshold_tolerates_quiet_savings():
    """Banks skip the balance-date stamp on inactive accounts for days at a
    time; a frozen aggregator record is weeks. The threshold must separate
    the two (seen live: 3-day-old savings vs a 51-day-old frozen envelope)."""
    from moneyhub.api import STALE_AFTER_DAYS

    assert 3 < STALE_AFTER_DAYS <= 14


def test_category_group_can_be_changed(client):
    created = client.post("/api/categories", json={"name": "Pay", "grp": "need"})
    assert created.status_code == 201
    cid = created.json()["id"]
    assert client.patch(f"/api/categories/{cid}", json={"grp": "bogus"}).status_code == 422
    assert client.patch(f"/api/categories/{cid}", json={"grp": "income"}).status_code == 200
    cats = {c["name"]: c["grp"] for c in client.get("/api/categories").json()}
    assert cats["Pay"] == "income"
