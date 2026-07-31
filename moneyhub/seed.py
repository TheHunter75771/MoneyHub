"""Seed a fresh database from a JSON export of the legacy money-hub.html data.

The JSON schema mirrors the legacy structures: accounts (ACCTS), categories
(CATS), bills (BILLS), debts (DEBTS), purchases (BUYS). Real personal data
lives in gitignored data/seed.local.json; the committed example is
examples/seed.example.json.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import compute_dedup_hash
from .models import (
    CATEGORY_SOURCE_MANUAL,
    Account,
    BalanceSnapshot,
    Bill,
    Category,
    Debt,
    Transaction,
)
from .money import to_cents


class SeedError(RuntimeError):
    pass


def seed_from_json(session: Session, data: dict) -> dict[str, int]:
    """Populate an empty database. Returns row counts per table.

    Refuses to run if any account already exists, so it cannot double-seed.
    """
    if session.scalars(select(Account)).first() is not None:
        raise SeedError("database already contains accounts; refusing to seed twice")

    as_of = date.fromisoformat(data["as_of"])
    counts = dict.fromkeys(
        ("accounts", "snapshots", "categories", "bills", "debts", "purchases"), 0
    )

    accounts: dict[str, Account] = {}
    for raw in data.get("accounts", []):
        account = Account(
            name=raw["name"], institution=raw.get("institution", ""), kind=raw["kind"]
        )
        session.add(account)
        accounts[account.name] = account
        counts["accounts"] += 1

    categories: dict[str, Category] = {}
    for raw in data.get("categories", []):
        category = Category(
            name=raw["name"],
            grp=raw["grp"],
            monthly_budget_cents=(
                to_cents(raw["monthly_budget"]) if raw.get("monthly_budget") is not None else None
            ),
        )
        session.add(category)
        categories[category.name] = category
        counts["categories"] += 1

    session.flush()  # assign ids so plain FK columns can be used below

    aliases: dict[str, str] = data.get("category_aliases", {})

    def resolve_category(name: str | None) -> Category | None:
        if not name:
            return None
        resolved = aliases.get(name, name)
        if resolved not in categories:
            raise SeedError(f"unknown category {resolved!r} (from {name!r})")
        return categories[resolved]

    for raw in data.get("accounts", []):
        if raw.get("balance") is not None:
            session.add(
                BalanceSnapshot(
                    account_id=accounts[raw["name"]].id,
                    as_of=as_of,
                    balance_cents=to_cents(raw["balance"]),
                    source="manual",
                )
            )
            counts["snapshots"] += 1

    for raw in data.get("bills", []):
        category = resolve_category(raw.get("category"))
        session.add(
            Bill(
                name=raw["name"],
                amount_cents=to_cents(raw["amount"]),
                due_rule=raw["due"],
                category_id=category.id if category else None,
            )
        )
        counts["bills"] += 1

    for raw in data.get("debts", []):
        session.add(
            Debt(
                name=raw["name"],
                balance_cents=to_cents(raw["balance"]),
                note=raw.get("note", ""),
                status=raw.get("status", "open"),
            )
        )
        counts["debts"] += 1

    for raw in data.get("purchases", []):
        account = accounts.get(raw.get("account", ""))
        if account is None:
            raise SeedError(f"purchase references unknown account: {raw.get('account')!r}")
        if raw.get("date"):
            posted, note = date.fromisoformat(raw["date"]), ""
        else:
            posted, note = as_of.replace(day=1), "seeded from legacy tracker; exact date unknown"
        amount_cents = -abs(to_cents(raw["amount"]))  # legacy BUYS are always spending
        category = resolve_category(raw.get("cat"))
        session.add(
            Transaction(
                account_id=account.id,
                posted_date=posted,
                amount_cents=amount_cents,
                description_raw=raw["name"],
                category_id=category.id if category else None,
                category_source=CATEGORY_SOURCE_MANUAL if category else None,
                dedup_hash=compute_dedup_hash(account.id, posted, amount_cents, raw["name"]),
                notes=note,
            )
        )
        counts["purchases"] += 1

    session.commit()
    return counts
