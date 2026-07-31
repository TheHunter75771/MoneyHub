from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

import moneyhub.importing as importing
from moneyhub.models import Account, BalanceSnapshot, Transaction
from moneyhub.simplefin import (
    SimpleFinError,
    auto_map_account,
    claim_setup_token,
    load_env,
    rows_from_account,
)


def _epoch(y, m, d):
    return int(datetime(y, m, d, 12, tzinfo=UTC).timestamp())


PAYLOAD = {
    "errors": [],
    "accounts": [
        {
            "id": "ACT-123",
            "name": "Spending",
            "org": {"name": "Service Credit Union", "domain": "servicecu.org"},
            "currency": "USD",
            "balance": "128.18",
            "balance-date": _epoch(2026, 7, 31),
            "transactions": [
                {
                    "id": "TXN-1",
                    "posted": _epoch(2026, 7, 31),
                    "amount": "-20.53",
                    "description": "Zaxby's",
                },
                {
                    "id": "TXN-2",
                    "posted": _epoch(2026, 7, 30),
                    "amount": "-16.99",
                    "description": "Steam",
                },
                {
                    "id": "TXN-3",
                    "posted": _epoch(2026, 7, 31),
                    "amount": "-5.00",
                    "description": "Pending thing",
                    "pending": True,
                },
            ],
        },
        {
            "id": "ACT-999",
            "name": "Mystery Account",
            "org": {"name": "Some Other Bank"},
            "balance": "1.00",
            "balance-date": _epoch(2026, 7, 31),
            "transactions": [],
        },
    ],
}


def test_load_env_parses_and_environ_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SIMPLEFIN_SETUP_TOKEN='abc'\n# comment\nOTHER=1\n")
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "https://u:p@host/simplefin")
    env = load_env(env_file)
    assert env["SIMPLEFIN_SETUP_TOKEN"] == "abc"
    assert env["SIMPLEFIN_ACCESS_URL"] == "https://u:p@host/simplefin"


def test_claim_rejects_non_https():
    token = base64.b64encode(b"http://insecure/claim/x").decode()
    with pytest.raises(SimpleFinError, match="not https"):
        claim_setup_token(token)


def test_claim_rejects_garbage_token():
    with pytest.raises(SimpleFinError, match="not valid base64"):
        claim_setup_token("!!!not-base64!!!")


def test_rows_from_account_skips_pending():
    rows = rows_from_account(PAYLOAD["accounts"][0])
    assert len(rows) == 2
    assert rows[0].fitid == "sfin:TXN-1"
    assert rows[0].amount_cents == -2053
    assert rows[0].posted_date == date(2026, 7, 31)


def test_auto_map_by_org_and_name(session):
    account = Account(name="SCU Spending", institution="Service CU", kind="envelope")
    session.add(account)
    session.commit()
    mapped = auto_map_account(session, PAYLOAD["accounts"][0])
    assert mapped is not None and mapped.id == account.id
    assert account.simplefin_id == "ACT-123"
    # Unknown org/name maps to nothing.
    assert auto_map_account(session, PAYLOAD["accounts"][1]) is None


@pytest.fixture
def synced(session, monkeypatch):
    session.add(Account(name="SCU Spending", institution="Service CU", kind="envelope"))
    session.commit()
    monkeypatch.setattr(
        "moneyhub.simplefin.load_env",
        lambda *a, **k: {"SIMPLEFIN_ACCESS_URL": "https://u:p@host/simplefin"},
    )
    monkeypatch.setattr(
        "moneyhub.simplefin.fetch_accounts",
        lambda url, days: json.dumps(PAYLOAD).encode(),
    )
    return session


def test_sync_end_to_end(synced):
    results, unmapped = importing.sync_simplefin(synced, days=30)
    (result,) = results
    assert result.account_name == "SCU Spending"
    assert result.new_count == 2  # pending row skipped
    assert result.snapshots_added == 1
    assert [u["id"] for u in unmapped] == ["ACT-999"]

    snap = synced.scalars(select(BalanceSnapshot)).one()
    assert snap.balance_cents == 12818
    assert snap.as_of == date(2026, 7, 31)


def test_resync_identical_payload_is_noop(synced):
    importing.sync_simplefin(synced, days=30)
    results, unmapped = importing.sync_simplefin(synced, days=30)
    assert results == [] and unmapped == []
    assert synced.query(Transaction).count() == 2


def test_sync_dedups_against_bank_fitid_rows(synced):
    """The same real charge already imported from a CSV must not double-count."""
    account = synced.scalars(select(Account)).one()
    account.simplefin_id = "ACT-123"
    synced.add(
        Transaction(
            account_id=account.id,
            posted_date=date(2026, 7, 31),
            amount_cents=-2053,
            description_raw="ZAXBY'S #12101 (from CSV)",
            fitid="446994865",
            dedup_hash="csv-h1",
        )
    )
    synced.commit()

    (result,), _ = importing.sync_simplefin(synced, days=30)
    assert result.cross_source_count == 1  # Zaxby's absorbed by the CSV row
    assert result.new_count == 1  # Steam is genuinely new
    assert synced.query(Transaction).count() == 2


def test_sync_without_credentials_errors(session, monkeypatch):
    monkeypatch.setattr("moneyhub.simplefin.load_env", lambda *a, **k: {})
    with pytest.raises(SimpleFinError, match="bridge.simplefin.org"):
        importing.sync_simplefin(session, days=30)
