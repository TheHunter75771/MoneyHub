from __future__ import annotations

from datetime import date

import pytest

from moneyhub.importing import ParsedRow, filter_cross_source_duplicates
from moneyhub.models import Account, BalanceSnapshot, Transaction
from moneyhub.statements import (
    StatementParseError,
    parse_statement_pages,
    statement_snapshot_dates,
)

PAGE_1 = """Send inquiries to:
Service Federal Credit Union
Account Name:
Member Number:
Period Ending:
TEST MEMBER
XXXXX0000
06/30/2026
Spending - XXXXXX6980 Add direct deposit to qualify for additional benefits.
Eff Date Tran Date Description Debits Credits Balance
06/01 Beginning Balance            $9.09
06/01 Deposit Transfer from Paycheck Holdings           $40.00          $49.09
06/01 POS Withdrawal MURPHY USA 7849 SAINT MARYS GAUS           $25.00           $24.09
06/02 POS Withdrawal DISCORD* NITROMONTHDISCORD.COM CAUS           $10.62           $13.47
"""

PAGE_2 = """Send inquiries to:
Service Federal Credit Union
Everyday Basic - XXXXXX6980 : Continued from previous page...
Eff Date Tran Date Description Debits Credits Balance
06/17 POS Withdrawal Atlas*Debitcard atlasfin.com/ CAUS           $40.27          $-26.80
06/18 Deposit Transfer from Bills           $0.43          $-26.37
06/30 Ending Balance          $-26.37
Emergency Fund - XXXXXX6590
Eff Date Tran Date Description Debits Credits Balance
06/01 Beginning Balance          $327.36
06/15 Withdrawal Transfer to Spending
          $171.25          $156.11
06/30 Ending Balance          $156.11
"""


def test_parse_sections_and_signs():
    statement = parse_statement_pages([PAGE_1, PAGE_2])
    assert statement.period_ending == date(2026, 6, 30)
    assert set(statement.sections) == {"6980", "6590"}

    spending = statement.sections["6980"]
    assert spending.name == "Spending"  # nickname kept over 'Everyday Basic'
    assert spending.beginning_cents == 909
    assert spending.ending_cents == -2637
    amounts = [r.amount_cents for r in spending.rows]
    assert amounts == [4000, -2500, -1062, -4027, 43]  # signs from balance chain

    efund = statement.sections["6590"]
    assert efund.rows[0].amount_cents == -17125  # wrapped line was reassembled
    assert efund.rows[0].eff_date == date(2026, 6, 15)


def test_snapshot_dates_use_end_of_day_convention():
    statement = parse_statement_pages([PAGE_1, PAGE_2])
    snaps = statement_snapshot_dates(statement.sections["6980"])
    assert snaps == [(date(2026, 5, 31), 909), (date(2026, 6, 30), -2637)]


def test_broken_balance_chain_is_rejected():
    bad = PAGE_1.replace("$24.09", "$99.99")
    with pytest.raises(StatementParseError, match="balance chain broken"):
        parse_statement_pages([bad, PAGE_2])


def test_rows_must_reach_ending_balance():
    bad = PAGE_2.replace("06/18 Deposit Transfer from Bills           $0.43          $-26.37\n", "")
    with pytest.raises(StatementParseError, match="Ending Balance"):
        parse_statement_pages([PAGE_1, bad])


def test_cross_source_duplicates_consume_once(session):
    account = Account(name="X", kind="envelope")
    session.add(account)
    session.commit()
    session.add(
        Transaction(
            account_id=account.id,
            posted_date=date(2026, 6, 2),
            amount_cents=-1062,
            description_raw="DISCORD (from CSV)",
            fitid="REF1",
            dedup_hash="h1",
        )
    )
    session.commit()

    incoming = [
        ParsedRow(date(2026, 6, 1), -1062, "POS Withdrawal DISCORD"),  # ±1 day: duplicate
        ParsedRow(date(2026, 6, 1), -1062, "POS Withdrawal DISCORD"),  # no txn left to claim
        ParsedRow(date(2026, 6, 20), -1062, "POS Withdrawal DISCORD"),  # out of window
    ]
    kept, skipped = filter_cross_source_duplicates(session, account, incoming)
    assert skipped == 1
    assert len(kept) == 2


def test_import_statement_end_to_end(session, tmp_path, monkeypatch):
    for name in ("SCU Spending", "SCU E-Fund"):
        session.add(Account(name=name, institution="Service CU", kind="envelope"))
    session.commit()

    import moneyhub.importing as importing

    monkeypatch.setattr(
        "moneyhub.statements.extract_pdf_pages", lambda path: [PAGE_1, PAGE_2]
    )
    fake = tmp_path / "statement.pdf"
    fake.write_bytes(b"fake pdf body")

    results = importing.import_statement_file(session, fake)
    by_account = {r.account_name: r for r in results}
    assert by_account["SCU Spending"].new_count == 5
    assert by_account["SCU E-Fund"].new_count == 1
    assert by_account["SCU Spending"].snapshots_added == 2

    assert session.query(Transaction).count() == 6
    assert session.query(BalanceSnapshot).count() == 4

    # Same bytes again: blocked at the file layer.
    with pytest.raises(importing.ImportError_, match="already imported"):
        importing.import_statement_file(session, fake)
