"""Prove the Alembic migration chain builds the schema the models describe."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from moneyhub.db import Base, make_engine

REPO_ROOT = Path(__file__).parent.parent


def _migrated_engine(tmp_path):
    db_path = tmp_path / "migrated.db"
    config = Config(REPO_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    return make_engine(db_path)


def test_upgrade_head_creates_all_model_tables(tmp_path, monkeypatch):
    monkeypatch.delenv("MONEYHUB_DB", raising=False)
    engine = _migrated_engine(tmp_path)
    migrated = set(inspect(engine).get_table_names()) - {"alembic_version"}
    expected = set(Base.metadata.tables)
    assert migrated == expected


def test_migrated_schema_has_partial_fitid_index(tmp_path, monkeypatch):
    monkeypatch.delenv("MONEYHUB_DB", raising=False)
    engine = _migrated_engine(tmp_path)
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("transactions")}
    fitid_index = indexes["ux_transactions_account_fitid"]
    assert fitid_index["unique"]
    assert fitid_index["column_names"] == ["account_id", "fitid"]
