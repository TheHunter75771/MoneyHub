from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from moneyhub.importing import (
    ImportError_,
    ParsedRow,
    apply_rows,
    detect_format,
    import_file,
    parse_scu_csv,
)
from moneyhub.models import Account, BalanceSnapshot, Transaction

FIXTURES = Path(__file__).parent / "fixtures"
FILE_A = FIXTURES / "scu_a.csv"
FILE_B = FIXTURES / "scu_b_overlap.csv"


@pytest.fixture
def account(session):
    acct = Account(name="Test Spending", kind="envelope")
    session.add(acct)
    session.commit()
    return acct


# --- parsing -----------------------------------------------------------------


def test_parse_scu_fields():
    rows = parse_scu_csv(FILE_A.read_text())
    assert len(rows) == 5
    first = rows[0]
    assert first.posted_date == date(2026, 7, 10)
    assert first.amount_cents == 1000
    assert first.fitid == "900000005"
    assert first.bank_category == "Account Transfer In"
    assert first.balance_after_cents == 3500
    assert first.merchant == "Transfer from Emergency Fund"
    assert rows[1].amount_cents == -2550
    assert rows[1].description_raw == "WAFFLE PALACE 42  BEAUFORT  SCUS"


def test_parse_pending_and_missing_reference():
    text = FILE_A.read_text().splitlines()[0] + "\n" + (
        '"20260713 999 100 15","7/13/2026","7/13/2026","Debit","Pending",'
        '"-1.00000","","","Snack Shack","Fast Food","Debit Card","","",""'
    )
    (row,) = parse_scu_csv(text)
    assert row.status == "pending"
    assert row.fitid == "20260713 999 100 15"  # falls back to Transaction ID
    assert row.balance_after_cents is None
    assert row.description_raw == "Snack Shack"  # falls back to Description


def test_detect_format_rejects_unknown():
    with pytest.raises(ImportError_):
        detect_format("Date,Amount,Payee\n2026-01-01,5,Someone")


# --- import + dedup ----------------------------------------------------------


def test_import_fresh_file(session, account):
    result = import_file(session, FILE_A, account)
    assert result.new_count == 5
    assert result.duplicate_count == 0
    assert result.batch.row_count == 5
    # Snapshot comes from the newest row's running balance.
    assert result.snapshot is not None
    assert result.snapshot.balance_cents == 3500
    assert result.snapshot.as_of == date(2026, 7, 10)
    assert result.snapshot.source == "import"


def test_same_file_twice_is_rejected(session, account):
    import_file(session, FILE_A, account)
    with pytest.raises(ImportError_, match="already imported"):
        import_file(session, FILE_A, account)


def test_overlapping_export_dedups_by_fitid(session, account):
    import_file(session, FILE_A, account)
    result = import_file(session, FILE_B, account)
    assert result.new_count == 2  # only the two Gas Stop rows are new
    assert result.duplicate_count == 3
    assert session.query(Transaction).count() == 7


def test_identical_same_day_rows_both_survive(session, account):
    """Two legitimately identical purchases in one file must both import."""
    import_file(session, FILE_A, account)
    coffees = session.scalars(
        select(Transaction).where(Transaction.merchant_norm == "Coffee Hut")
    ).all()
    assert len(coffees) == 2


def test_hash_count_matching_without_fitid(session, account):
    row = ParsedRow(
        posted_date=date(2026, 7, 8),
        amount_cents=-500,
        description_raw="COFFEE HUT #9  BEAUFORT  SCUS",
    )
    inserted, dups = apply_rows(session, account, [row], import_id=None)
    session.commit()
    assert (len(inserted), dups) == (1, 0)

    # Re-sending two identical rows: one is covered by the existing row,
    # the excess one is genuinely new.
    inserted, dups = apply_rows(session, account, [row, row], import_id=None)
    session.commit()
    assert (len(inserted), dups) == (1, 1)


def test_manual_entries_are_flagged_not_merged(session, account):
    manual = Transaction(
        account_id=account.id,
        posted_date=date(2026, 7, 8),  # one day off the imported 7/9 row
        amount_cents=-2550,
        description_raw="waffles with the squad",
        dedup_hash="manual-1",
    )
    session.add(manual)
    session.commit()

    result = import_file(session, FILE_A, account)
    assert result.new_count == 5  # nothing silently dropped or merged
    assert len(result.merge_candidates) == 1
    candidate = result.merge_candidates[0]
    assert candidate.manual.id == manual.id
    assert candidate.imported.merchant_norm == "Waffle Palace"


def test_merge_candidates_ignore_out_of_window(session, account):
    manual = Transaction(
        account_id=account.id,
        posted_date=date(2026, 7, 20),  # 11 days away from any imported row
        amount_cents=-2550,
        description_raw="old note",
        dedup_hash="manual-2",
    )
    session.add(manual)
    session.commit()
    result = import_file(session, FILE_A, account)
    assert result.merge_candidates == []


def test_snapshot_recorded_once_per_import(session, account):
    import_file(session, FILE_A, account)
    import_file(session, FILE_B, account)
    snaps = session.scalars(select(BalanceSnapshot)).all()
    assert [s.balance_cents for s in snaps] == [3500, 1032]
