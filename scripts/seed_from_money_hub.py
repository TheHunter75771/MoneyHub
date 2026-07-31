#!/usr/bin/env python
"""Seed a migrated Money Hub database from a legacy money-hub.html JSON export.

Usage:
    alembic upgrade head
    python scripts/seed_from_money_hub.py [--data data/seed.local.json] [--db data/moneyhub.db]

Refuses to seed a database that already has accounts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import inspect

from moneyhub.db import make_engine, make_session_factory
from moneyhub.seed import SeedError, seed_from_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/seed.local.json", type=Path)
    parser.add_argument("--db", default="data/moneyhub.db", type=Path)
    args = parser.parse_args()

    if not args.data.exists():
        print(f"seed file not found: {args.data}", file=sys.stderr)
        print("copy examples/seed.example.json there and edit it", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"database not found: {args.db} — run `alembic upgrade head` first", file=sys.stderr)
        return 1

    engine = make_engine(args.db)
    if "accounts" not in inspect(engine).get_table_names():
        print("database has no schema — run `alembic upgrade head` first", file=sys.stderr)
        return 1

    data = json.loads(args.data.read_text())
    with make_session_factory(engine)() as session:
        try:
            counts = seed_from_json(session, data)
        except SeedError as exc:
            print(f"seed refused: {exc}", file=sys.stderr)
            return 1

    print(f"seeded {args.db}:")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
