"""Transfer-pair linking.

Both legs of a movement between owned accounts should cancel out. This module
finds transfer-classified transactions (category group 'transfer') and links
the outflow leg on one account to the matching inflow leg on another —
same magnitude, different account, posted within a small window. Linked legs
share a transfer_group_id.

Legs that never find a partner are the interesting part: they point at an
account we can't see into yet (an Atlas payment with no Atlas-side feed, an
external transfer to an unsynced institution) or at a mis-categorized row.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Category, Transaction

PAIR_WINDOW_DAYS = 3


@dataclass
class PairResult:
    groups_created: int
    legs_linked: int
    unmatched: list[Transaction] = field(default_factory=list)


def _transfer_txns(session: Session, unlinked_only: bool = True) -> list[Transaction]:
    transfer_cat_ids = select(Category.id).where(Category.grp == "transfer")
    stmt = select(Transaction).where(Transaction.category_id.in_(transfer_cat_ids))
    if unlinked_only:
        stmt = stmt.where(Transaction.transfer_group_id.is_(None))
    return list(session.scalars(stmt.order_by(Transaction.posted_date, Transaction.id)))


def pair_transfers(session: Session) -> PairResult:
    """Link unpaired transfer legs. Greedy, closest-date-first, consume-once."""
    legs = _transfer_txns(session, unlinked_only=True)
    outs = [t for t in legs if t.amount_cents < 0]
    ins = [t for t in legs if t.amount_cents > 0]

    next_group = (
        session.scalar(select(func.coalesce(func.max(Transaction.transfer_group_id), 0))) + 1
    )
    used: set[int] = set()
    groups = 0
    linked = 0
    for out in outs:
        best = None
        best_gap = PAIR_WINDOW_DAYS + 1
        for candidate in ins:
            if candidate.id in used:
                continue
            if candidate.account_id == out.account_id:
                continue
            if candidate.amount_cents != -out.amount_cents:
                continue
            gap = abs((candidate.posted_date - out.posted_date).days)
            if gap < best_gap:
                best, best_gap = candidate, gap
        if best is not None and best_gap <= PAIR_WINDOW_DAYS:
            used.add(best.id)
            out.transfer_group_id = next_group
            best.transfer_group_id = next_group
            next_group += 1
            groups += 1
            linked += 2
    session.commit()

    unmatched = [t for t in _transfer_txns(session, unlinked_only=True)]
    return PairResult(groups_created=groups, legs_linked=linked, unmatched=unmatched)


def transfer_groups(session: Session, limit: int = 60) -> list[dict]:
    """Linked pairs, newest first, as {group_id, date, amount, from, to, legs}."""
    linked = session.scalars(
        select(Transaction)
        .where(Transaction.transfer_group_id.is_not(None))
        .order_by(Transaction.transfer_group_id.desc())
    ).all()
    by_group: dict[int, list[Transaction]] = {}
    for t in linked:
        by_group.setdefault(t.transfer_group_id, []).append(t)
    out = []
    for gid in sorted(by_group, reverse=True)[:limit]:
        legs = by_group[gid]
        src = next((t for t in legs if t.amount_cents < 0), None)
        dst = next((t for t in legs if t.amount_cents > 0), None)
        out.append(
            {
                "group_id": gid,
                "date": max(t.posted_date for t in legs).isoformat(),
                "amount_cents": abs(legs[0].amount_cents),
                "from_account_id": src.account_id if src else None,
                "to_account_id": dst.account_id if dst else None,
                "legs": [
                    {
                        "id": t.id,
                        "account_id": t.account_id,
                        "date": t.posted_date.isoformat(),
                        "amount_cents": t.amount_cents,
                        "description": t.description_raw,
                    }
                    for t in legs
                ],
            }
        )
    return out
