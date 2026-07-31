from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from moneyhub.api import create_app
from moneyhub.db import Base, make_engine, make_session_factory
from moneyhub.models import Account, Bill, Category, Transaction


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # notion state file writes land in tmp
    db_path = tmp_path / "api.db"
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        s.add_all(
            [
                Account(id=1, name="SCU Spending", kind="envelope"),
                Account(id=2, name="SCU Bills", kind="envelope"),
                Category(id=1, name="Transfer", grp="transfer"),
                Category(id=2, name="Income", grp="income"),
                Bill(id=1, name="Storage", amount_cents=5000, due_rule="1st"),
                Bill(id=2, name="Car loan", amount_cents=21000, due_rule="15th"),
            ]
        )
        s.add_all(
            [
                Transaction(
                    id=1,
                    account_id=2,
                    posted_date=date(2026, 7, 10),
                    amount_cents=-14625,
                    description_raw="Transfer to Spending",
                    category_id=1,
                    dedup_hash="h1",
                ),
                Transaction(
                    id=2,
                    account_id=1,
                    posted_date=date(2026, 7, 10),
                    amount_cents=14625,
                    description_raw="Transfer from Bills",
                    category_id=1,
                    dedup_hash="h2",
                ),
                Transaction(
                    id=3,
                    account_id=1,
                    posted_date=date(2026, 7, 10),
                    amount_cents=-2334,
                    description_raw="External Transfer to NAVY FEDERAL",
                    category_id=1,
                    dedup_hash="h3",
                ),
                Transaction(
                    id=4,
                    account_id=1,
                    posted_date=date(2026, 7, 27),
                    amount_cents=67748,
                    description_raw="External Deposit MCTF TREAS 310",
                    category_id=2,
                    dedup_hash="h4",
                ),
                Transaction(
                    id=5,
                    account_id=1,
                    posted_date=date(2026, 7, 26),
                    amount_cents=-1900,
                    description_raw="Courtesy Pay Fee",
                    dedup_hash="h5",
                ),
            ]
        )
        s.commit()
    return TestClient(create_app(db_path))


def test_pairing_endpoint_links_and_reports_unmatched(client):
    res = client.post("/api/transfers/pair").json()
    assert res["groups_created"] == 1
    assert res["unmatched"] == 1  # the NFCU leg has no visible other side

    view = client.get("/api/transfers").json()
    (group,) = view["groups"]
    assert group["from_account"] == "SCU Bills"
    assert group["to_account"] == "SCU Spending"
    assert group["amount_cents"] == 14625
    (leg,) = view["unmatched"]
    assert "NAVY FEDERAL" in leg["description"]


def test_payday_shape(client):
    body = client.get("/api/payday", params={"period": "2026-07", "check": "15th"}).json()
    assert [b["name"] for b in body["bills"]] == ["Car loan"]
    assert body["unpaid_cents"] == 21000
    assert body["income_detected_cents"] == 67748
    assert body["fees_60d_count"] >= 0  # fee window is relative to today


def test_payday_check_filter_and_paid(client):
    client.post("/api/bills/1/paid", json={"period": "2026-07", "paid": True})
    body = client.get("/api/payday", params={"period": "2026-07", "check": "1st"}).json()
    assert body["unpaid_cents"] == 0
    assert body["bills"][0]["paid"] is True


def test_notion_status_unconfigured(client, monkeypatch):
    monkeypatch.setattr("moneyhub.notion.load_env", lambda *a, **k: {})
    body = client.get("/api/notion/status").json()
    assert body["configured"] is False
    push = client.post("/api/notion/push")
    assert push.status_code == 502
    assert "not configured" in push.json()["detail"]


def test_notion_push_with_mocked_api(client, monkeypatch):
    import io
    import json as jsonlib

    monkeypatch.setattr(
        "moneyhub.notion.load_env",
        lambda *a, **k: {"NOTION_TOKEN": "secret_x", "NOTION_PARENT_PAGE_ID": "abc123"},
    )
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["payload"] = jsonlib.loads(request.data)
        captured["auth"] = request.headers.get("Authorization")
        return io.BytesIO(jsonlib.dumps({"url": "https://notion.so/fake-page"}).encode())

    class _Ctx(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen_ctx(request, timeout=0):
        captured["payload"] = jsonlib.loads(request.data)
        captured["auth"] = request.headers.get("Authorization")
        return _Ctx(jsonlib.dumps({"url": "https://notion.so/fake-page"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen_ctx)
    res = client.post("/api/notion/push")
    assert res.status_code == 200
    assert res.json()["page_url"] == "https://notion.so/fake-page"
    assert captured["auth"] == "Bearer secret_x"
    titles = captured["payload"]["properties"]["title"]
    assert titles[0]["text"]["content"].startswith("Money Hub — ")
    # snapshot content includes debts/budget headings
    kinds = [b["type"] for b in captured["payload"]["children"]]
    assert "heading_2" in kinds

    status = client.get("/api/notion/status").json()
    assert status["configured"] is True
    assert status["last_result"] == "ok"
