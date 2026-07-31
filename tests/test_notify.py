from __future__ import annotations

import io
from datetime import date

import pytest

from moneyhub.models import Account, BalanceSnapshot, Bill, Category, Transaction
from moneyhub.notify import NotifyError, compose_digest, send


@pytest.fixture
def world(session):
    spending = Account(id=1, name="SCU Spending", kind="envelope")
    bills = Account(id=2, name="SCU Bills", kind="envelope")
    session.add_all(
        [
            spending,
            bills,
            Category(id=1, name="Transfer", grp="transfer"),
            Bill(id=1, name="Storage", amount_cents=5000, due_rule="1st"),
            Bill(id=2, name="Car loan", amount_cents=21000, due_rule="15th"),
            BalanceSnapshot(account_id=1, as_of=date(2026, 7, 31), balance_cents=2818),
            BalanceSnapshot(account_id=2, as_of=date(2026, 7, 31), balance_cents=1000),
        ]
    )
    session.commit()
    return session


def test_digest_warns_when_bills_uncovered(world):
    digest = compose_digest(world, today=date(2026, 8, 2))  # 1st-check window
    assert digest["priority"] == "high"
    assert "can't cover" in digest["body"]
    assert "1st-check bills unpaid: $50.00" in digest["body"]
    assert "bills at risk" in digest["title"]


def test_digest_calm_when_covered(world):
    snap = world.get(BalanceSnapshot, 1)
    snap.balance_cents = 100000
    world.commit()
    digest = compose_digest(world, today=date(2026, 8, 2))
    assert digest["priority"] == "default"
    assert "can't cover" not in digest["body"]


def test_digest_payday_nudge_on_the_15th(world):
    digest = compose_digest(world, today=date(2026, 8, 15))
    assert "run /payday" in digest["body"]
    assert "15th-check bills unpaid: $210.00" in digest["body"]


def test_digest_counts_spending_this_month(world):
    world.add(
        Transaction(
            account_id=1,
            posted_date=date(2026, 8, 1),
            amount_cents=-2500,
            description_raw="zaxbys",
            dedup_hash="h1",
        )
    )
    world.commit()
    digest = compose_digest(world, today=date(2026, 8, 2))
    assert "Spent so far this month: $25.00." in digest["body"]


def test_send_requires_topic(monkeypatch):
    monkeypatch.setattr("moneyhub.notify.load_env", lambda *a, **k: {})
    with pytest.raises(NotifyError, match="NTFY_TOPIC"):
        send("t", "b")


def test_send_posts_to_topic(monkeypatch):
    monkeypatch.setattr(
        "moneyhub.notify.load_env",
        lambda *a, **k: {"NTFY_TOPIC": "secret-topic", "NTFY_SERVER": "https://ntfy.example"},
    )
    captured = {}

    class _Ctx(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        captured["priority"] = request.headers.get("Priority")
        return _Ctx(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    send("Title", "hello phone", priority="high", tags="rotating_light")
    assert captured["url"] == "https://ntfy.example/secret-topic"
    assert captured["body"] == "hello phone"
    assert captured["priority"] == "high"
