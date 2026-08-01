from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import moneyhub.importing as importing
from moneyhub.api import create_app
from moneyhub.db import Base, make_engine, make_session_factory
from moneyhub.models import Account, SyncRun
from moneyhub.simplefin import SimpleFinError

PAYLOAD = {"accounts": []}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "sync.db"
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        s.add(Account(id=1, name="SCU Spending", kind="envelope"))
        s.commit()
    return path


@pytest.fixture
def session(db_path):
    with make_session_factory(make_engine(db_path))() as s:
        yield s


def _ok_env(monkeypatch, payload=PAYLOAD):
    monkeypatch.setattr(
        "moneyhub.simplefin.load_env",
        lambda *a, **k: {"SIMPLEFIN_ACCESS_URL": "https://u:p@host/simplefin"},
    )
    monkeypatch.setattr(
        "moneyhub.simplefin.fetch_accounts", lambda url, days: json.dumps(payload).encode()
    )


def test_successful_sync_is_recorded(session, monkeypatch):
    _ok_env(monkeypatch)
    importing.sync_simplefin(session)
    run = session.scalars(select(SyncRun)).one()
    assert run.status == "ok"
    assert run.finished_at is not None


def test_failed_sync_is_recorded_and_reraised(session, monkeypatch):
    monkeypatch.setattr("moneyhub.simplefin.load_env", lambda *a, **k: {})
    with pytest.raises(SimpleFinError):
        importing.sync_simplefin(session)
    run = session.scalars(select(SyncRun)).one()
    assert run.status == "failed"
    assert "SimpleFinError" in run.message
    assert run.finished_at is not None


def test_state_distinguishes_last_success_from_last_attempt(db_path, monkeypatch):
    client = TestClient(create_app(db_path))
    with make_session_factory(make_engine(db_path))() as s:
        _ok_env(monkeypatch)
        importing.sync_simplefin(s)
        monkeypatch.setattr("moneyhub.simplefin.load_env", lambda *a, **k: {})
        with pytest.raises(SimpleFinError):
            importing.sync_simplefin(s)

    body = client.get("/api/state").json()
    assert body["sync_failing"] is True
    assert "SimpleFinError" in body["sync_error"]
    # the success timestamp is older than the failed attempt
    assert body["sync_last_at"] < body["sync_last_attempt_at"]

    history = client.get("/api/sync/history").json()
    assert [h["status"] for h in history] == ["failed", "ok"]


def test_network_errors_surface_as_simplefin_errors(session, monkeypatch):
    """A refused connection must be a clean SimpleFinError, not a raw traceback."""
    import urllib.error

    monkeypatch.setattr(
        "moneyhub.simplefin.load_env",
        lambda *a, **k: {"SIMPLEFIN_ACCESS_URL": "https://u:p@127.0.0.1:9/simplefin"},
    )

    def boom(request, timeout=0):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(SimpleFinError, match="unreachable"):
        importing.sync_simplefin(session)
    run = session.scalars(select(SyncRun)).one()
    assert run.status == "failed"


def test_state_clean_when_never_failed(db_path, monkeypatch):
    client = TestClient(create_app(db_path))
    with make_session_factory(make_engine(db_path))() as s:
        _ok_env(monkeypatch)
        importing.sync_simplefin(s)
    body = client.get("/api/state").json()
    assert body["sync_failing"] is False
    assert body["sync_error"] is None
