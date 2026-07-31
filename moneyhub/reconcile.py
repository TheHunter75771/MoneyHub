"""Balance reconciliation: does the ledger explain the stated balance?

Convention: a BalanceSnapshot is an END-OF-DAY balance. The drift window
between two snapshots therefore covers transactions with
prior.as_of < posted_date <= stated.as_of.

Drift = stated - (prior balance + transactions in window). Zero means the
books fully explain reality; anything else is unrecorded activity — the
thing that used to take 20 minutes to find by hand.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, BalanceSnapshot, Transaction


class ReconcileError(RuntimeError):
    pass


@dataclass
class ReconcileReport:
    account_name: str
    prior_as_of: date
    prior_balance_cents: int
    stated_as_of: date
    stated_balance_cents: int
    txn_count: int
    txn_sum_cents: int

    @property
    def computed_cents(self) -> int:
        return self.prior_balance_cents + self.txn_sum_cents

    @property
    def drift_cents(self) -> int:
        return self.stated_balance_cents - self.computed_cents


def _snapshots_desc(session: Session, account_id: int) -> list[BalanceSnapshot]:
    return list(
        session.scalars(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == account_id)
            .order_by(BalanceSnapshot.as_of.desc(), BalanceSnapshot.id.desc())
        )
    )


def reconcile(
    session: Session,
    account: Account,
    stated_cents: int | None = None,
    stated_as_of: date | None = None,
) -> ReconcileReport:
    """Reconcile against a stated balance, or the newest snapshot if omitted."""
    snapshots = _snapshots_desc(session, account.id)

    if stated_cents is not None:
        if stated_as_of is None:
            raise ReconcileError("a stated balance needs a date (--as-of)")
    else:
        if not snapshots:
            raise ReconcileError(f"no balance snapshots for {account.name!r}")
        stated_cents = snapshots[0].balance_cents
        stated_as_of = snapshots[0].as_of

    prior = next((s for s in snapshots if s.as_of < stated_as_of), None)
    if prior is None:
        raise ReconcileError(
            f"no snapshot earlier than {stated_as_of} for {account.name!r}; "
            "record a starting balance first"
        )

    txn_sum, txn_count = session.execute(
        select(func.coalesce(func.sum(Transaction.amount_cents), 0), func.count()).where(
            Transaction.account_id == account.id,
            Transaction.posted_date > prior.as_of,
            Transaction.posted_date <= stated_as_of,
        )
    ).one()

    return ReconcileReport(
        account_name=account.name,
        prior_as_of=prior.as_of,
        prior_balance_cents=prior.balance_cents,
        stated_as_of=stated_as_of,
        stated_balance_cents=stated_cents,
        txn_count=txn_count,
        txn_sum_cents=txn_sum,
    )
