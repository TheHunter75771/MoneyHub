from datetime import datetime

import pytest
from sqlalchemy import text

from moneyhub.db import make_engine
from moneyhub.snapshot import SnapshotError, take_snapshot


def _live_db(tmp_path):
    """A WAL-mode database with an uncheckpointed write still in the WAL."""
    path = tmp_path / "live.db"
    engine = make_engine(path)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.execute(text("INSERT INTO t (v) VALUES ('in-wal')"))
    # keep a connection open so the WAL is not checkpointed on close
    holder = engine.connect()
    assert (path.with_name("live.db-wal")).exists()
    return path, holder


def test_snapshot_includes_wal_pages_and_is_single_file(tmp_path):
    path, holder = _live_db(tmp_path)
    try:
        snap, pruned = take_snapshot(path, tmp_path / "bak", keep=3)
    finally:
        holder.close()
    assert pruned == []
    assert snap.exists() and not snap.with_name(snap.name + "-wal").exists()
    rows = make_engine(snap).connect().execute(text("SELECT v FROM t")).scalars().all()
    assert rows == ["in-wal"]


def test_snapshot_prunes_oldest_beyond_keep(tmp_path):
    path, holder = _live_db(tmp_path)
    holder.close()
    dest = tmp_path / "bak"
    for day in range(1, 6):
        take_snapshot(path, dest, keep=3, now=datetime(2026, 1, day, 9, 0, 0))
    names = sorted(p.name for p in dest.iterdir())
    assert names == [
        "moneyhub-20260103-090000.db",
        "moneyhub-20260104-090000.db",
        "moneyhub-20260105-090000.db",
    ]


def test_snapshot_rejects_missing_source(tmp_path):
    with pytest.raises(SnapshotError):
        take_snapshot(tmp_path / "nope.db", tmp_path / "bak")
