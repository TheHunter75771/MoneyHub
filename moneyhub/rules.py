"""The category rules engine.

Deterministic: rules apply in (priority, id) order, first match wins.
Manual categorization is sticky — the engine only touches rows whose
category_source is NULL or 'rule:<id>', and a full run recomputes every
such row from the current ruleset (so fixing a bad rule fixes the rows it
mis-tagged; deleting a rule releases its rows back to the inbox).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import CATEGORY_SOURCE_MANUAL, Rule, Transaction


def rule_matches(rule: Rule, txn: Transaction) -> bool:
    if rule.match_text:
        haystack = f"{txn.merchant_norm} {txn.description_raw}".lower()
        if rule.match_text.lower() not in haystack:
            return False
    if rule.bank_category and txn.bank_category != rule.bank_category:
        return False
    if rule.account_id is not None and txn.account_id != rule.account_id:
        return False
    if rule.direction == "in" and txn.amount_cents <= 0:
        return False
    if rule.direction == "out" and txn.amount_cents >= 0:
        return False
    return True


@dataclass
class ApplyStats:
    examined: int = 0
    categorized: int = 0
    cleared: int = 0  # previously rule-tagged rows no rule matches anymore
    by_rule: dict[int, int] = field(default_factory=dict)


def apply_rules(session: Session, commit: bool = True) -> ApplyStats:
    """Recompute categorization for every non-manual transaction."""
    rules = list(
        session.scalars(
            select(Rule).where(Rule.active.is_(True)).order_by(Rule.priority, Rule.id)
        )
    )
    targets = session.scalars(
        select(Transaction).where(
            or_(
                Transaction.category_source.is_(None),
                Transaction.category_source.like("rule:%"),
            )
        )
    ).all()

    stats = ApplyStats(examined=len(targets))
    for txn in targets:
        matched = next((r for r in rules if rule_matches(r, txn)), None)
        if matched is not None:
            txn.category_id = matched.category_id
            txn.category_source = f"rule:{matched.id}"
            stats.categorized += 1
            stats.by_rule[matched.id] = stats.by_rule.get(matched.id, 0) + 1
        elif txn.category_source is not None:
            txn.category_id = None
            txn.category_source = None
            stats.cleared += 1
    if commit:
        session.commit()
    return stats


def set_category_manual(session: Session, txn: Transaction, category_id: int | None) -> None:
    """Manual override — sticky against all future rule runs."""
    txn.category_id = category_id
    txn.category_source = CATEGORY_SOURCE_MANUAL if category_id is not None else None
    session.commit()
