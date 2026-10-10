"""Consistent SQLite snapshots via the online backup API.

The live database runs in WAL mode, so a plain file copy silently omits
every page still sitting in ``moneyhub.db-wal`` and yields a valid-looking
database that is missing the newest writes. ``sqlite3.Connection.backup``
copies a consistent image regardless of the WAL state. Each snapshot is
written as a single self-contained file (journal mode switched back to
DELETE), integrity-checked, and the newest ``keep`` snapshots are retained.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SNAPSHOT_PREFIX = "moneyhub-"
SNAPSHOT_SUFFIX = ".db"


class SnapshotError(Exception):
    pass


def take_snapshot(
    db_path: str | Path, dest_dir: str | Path, keep: int = 14, now: datetime | None = None
) -> tuple[Path, list[Path]]:
    """Write one snapshot into ``dest_dir`` and prune the oldest beyond ``keep``.

    Returns ``(snapshot_path, pruned_paths)``. Raises :class:`SnapshotError`
    when the source is missing or the copy fails its integrity check.
    """
    source = Path(db_path)
    if not source.exists():
        raise SnapshotError(f"database not found: {source}")
    if keep < 1:
        raise SnapshotError("keep must be at least 1")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = dest / f"{SNAPSHOT_PREFIX}{stamp}{SNAPSHOT_SUFFIX}"
    if target.exists():  # two runs inside one second — keep both
        target = dest / f"{SNAPSHOT_PREFIX}{stamp}-{source.stat().st_mtime_ns}{SNAPSHOT_SUFFIX}"

    src_conn = sqlite3.connect(source)
    dst_conn = sqlite3.connect(target)
    try:
        src_conn.backup(dst_conn)
        dst_conn.execute("PRAGMA journal_mode=DELETE")
        verdict = dst_conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst_conn.close()
        src_conn.close()
    if verdict != "ok":
        target.unlink(missing_ok=True)
        raise SnapshotError(f"snapshot failed integrity check: {verdict}")

    snapshots = sorted(
        p
        for p in dest.iterdir()
        if p.name.startswith(SNAPSHOT_PREFIX) and p.suffix == SNAPSHOT_SUFFIX
    )
    pruned = snapshots[:-keep] if len(snapshots) > keep else []
    for old in pruned:
        old.unlink()
    return target, pruned
