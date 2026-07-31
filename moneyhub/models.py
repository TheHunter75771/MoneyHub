"""SQLAlchemy models.

Money convention: every amount is integer cents (see moneyhub.money).
- Transaction.amount_cents is signed: negative = money leaving the account,
  positive = money arriving.
- BalanceSnapshot.balance_cents is the signed account balance: positive for
  asset accounts, negative = amount owed on credit accounts.

Transfers: both legs of a movement between owned accounts (e.g. the Atlas
card auto-paying its balance out of SCU Spending) share a transfer_group_id
and are excluded from income/spending math. The purchase on the card is the
real spend; the autopay is not.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


ACCOUNT_KINDS = ("checking", "savings", "envelope", "credit", "investment")
# 'transfer' marks movement between owned accounts — excluded from all
# income/spending math (the running fix for the card-autopay double-count).
CATEGORY_GROUPS = ("need", "want", "debt", "savings", "income", "transfer")
CATEGORY_SOURCE_MANUAL = "manual"  # rules never overwrite manual categorization


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    institution: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(20))  # one of ACCOUNT_KINDS
    is_active: Mapped[bool] = mapped_column(default=True)
    simplefin_id: Mapped[str | None] = mapped_column(String(128))  # live-sync mapping

    transactions: Mapped[list[Transaction]] = relationship(back_populates="account")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    grp: Mapped[str] = mapped_column(String(20))  # one of CATEGORY_GROUPS
    monthly_budget_cents: Mapped[int | None] = mapped_column()
    active: Mapped[bool] = mapped_column(default=True)


class ImportBatch(Base):
    """One imported file. The file hash blocks accidental re-imports."""

    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    # Null for multi-account files (e.g. a member statement covering all shares).
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    row_count: Mapped[int] = mapped_column(default=0)
    new_count: Mapped[int] = mapped_column(default=0)
    duplicate_count: Mapped[int] = mapped_column(default=0)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_dedup_hash", "dedup_hash"),
        Index("ix_transactions_account_date", "account_id", "posted_date"),
        Index(
            "ux_transactions_account_fitid",
            "account_id",
            "fitid",
            unique=True,
            sqlite_where=text("fitid IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    posted_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column()  # signed; negative = outflow
    description_raw: Mapped[str] = mapped_column(Text)  # verbatim from source, never mutated
    merchant_norm: Mapped[str] = mapped_column(String(200), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    category_source: Mapped[str | None] = mapped_column(String(30))  # 'manual' | 'rule:<id>'
    status: Mapped[str] = mapped_column(String(10), default="posted")  # posted | pending
    transfer_group_id: Mapped[int | None] = mapped_column()
    import_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"))  # null = manual entry
    fitid: Mapped[str | None] = mapped_column(String(255))  # bank's stable transaction id
    dedup_hash: Mapped[str] = mapped_column(String(64))
    bank_category: Mapped[str] = mapped_column(String(100), default="", server_default="")
    balance_after_cents: Mapped[int | None] = mapped_column()  # running balance from export
    notes: Mapped[str] = mapped_column(Text, default="")
    # Manual rows a human ruled "not a duplicate" — excluded from merge candidates.
    merge_dismissed: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="transactions")
    category: Mapped[Category | None] = relationship()


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    amount_cents: Mapped[int] = mapped_column()
    due_rule: Mapped[str] = mapped_column(String(50))  # '1st', '15th', 'biweekly', 'qtrly 7/20'
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    autopay_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    match_pattern: Mapped[str] = mapped_column(String(200), default="")  # auto-check on import
    active: Mapped[bool] = mapped_column(default=True)

    category: Mapped[Category | None] = relationship()


class BillPayment(Base):
    """Paid-state of a bill for one period — the checkbox, now auditable."""

    __tablename__ = "bill_payments"
    __table_args__ = (UniqueConstraint("bill_id", "period", name="ux_bill_payments_bill_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"))
    period: Mapped[str] = mapped_column(String(7))  # 'YYYY-MM'
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    bill: Mapped[Bill] = relationship()


class Debt(Base):
    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    balance_cents: Mapped[int] = mapped_column()
    min_payment_cents: Mapped[int | None] = mapped_column()
    apr: Mapped[float | None] = mapped_column()
    note: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | paid_off


class Rule(Base):
    """A categorization rule. First match wins, ordered by (priority, id).

    Empty condition fields mean "any". Rules only ever touch transactions
    whose category_source is NULL or 'rule:<id>' — manual categorization is
    sticky and can never be overwritten by the engine.
    """

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    priority: Mapped[int] = mapped_column(default=100, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    match_text: Mapped[str] = mapped_column(String(200), default="")  # ci substring
    bank_category: Mapped[str] = mapped_column(String(100), default="")  # exact
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    direction: Mapped[str] = mapped_column(String(3), default="")  # '' | 'in' | 'out'
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    category: Mapped[Category] = relationship()


class BalanceSnapshot(Base):
    """A stated balance at a point in time — the reconciliation anchor.

    Drift report = (last snapshot + transactions since) vs. a newer snapshot.
    """

    __tablename__ = "balance_snapshots"
    __table_args__ = (Index("ix_balance_snapshots_account_date", "account_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date] = mapped_column(Date)
    balance_cents: Mapped[int] = mapped_column()
    source: Mapped[str] = mapped_column(String(10), default="manual")  # manual | import
