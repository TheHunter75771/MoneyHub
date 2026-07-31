"""Phone notifications via ntfy (https://ntfy.sh).

The topic name is the only credential — anyone who knows it can read the
pushes — so it must be long and random and live in the gitignored .env:

    NTFY_TOPIC=noah-moneyhub-<random>
    NTFY_SERVER=https://ntfy.sh        (optional; self-hostable later)

Digest philosophy: one push after the daily sync — balances, anything that
needs attention, and a payday nudge on the 1st/15th. High priority only when
money is actually at risk (courtesy-fee exposure, drift, sync failure).
"""
from __future__ import annotations

import urllib.request
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, BalanceSnapshot, Bill, BillPayment, Category, Transaction
from .money import from_cents
from .simplefin import USER_AGENT, load_env


class NotifyError(RuntimeError):
    pass


def config() -> dict[str, str]:
    env = load_env()
    return {
        "topic": env.get("NTFY_TOPIC", ""),
        "server": env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
    }


def send(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    cfg = config()
    if not cfg["topic"]:
        raise NotifyError("NTFY_TOPIC is not set in .env — pick a long random topic name")
    request = urllib.request.Request(
        f"{cfg['server']}/{cfg['topic']}",
        data=body.encode(),
        method="POST",
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": tags,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"ntfy push failed: HTTP {exc.code}") from exc


def _latest_balance(session: Session, account_name: str) -> int | None:
    account = session.scalars(select(Account).where(Account.name == account_name)).first()
    if account is None:
        return None
    snap = session.scalars(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account.id)
        .order_by(BalanceSnapshot.as_of.desc(), BalanceSnapshot.id.desc())
    ).first()
    return snap.balance_cents if snap else None


def _active_check(today: date) -> str:
    return "1st" if today.day < 15 else "15th"


def _unpaid_check_bills(session: Session, today: date) -> tuple[str, int]:
    """(check label, unpaid cents) for the check window we're currently in."""
    from .api import _check_of  # single source of truth for check assignment

    check = _active_check(today)
    period = today.strftime("%Y-%m")
    paid_ids = set(
        session.scalars(select(BillPayment.bill_id).where(BillPayment.period == period))
    )
    unpaid = 0
    for bill in session.scalars(select(Bill).where(Bill.active.is_(True))):
        if _check_of(bill.due_rule) == check and bill.id not in paid_ids:
            unpaid += bill.amount_cents
    return check, unpaid


def compose_digest(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    lines: list[str] = []
    priority = "default"
    tags = "moneybag"

    balances = []
    for name, label in [
        ("SCU Spending", "Spending"),
        ("SCU Bills", "Bills"),
        ("SCU E-Fund", "E-Fund"),
    ]:
        cents = _latest_balance(session, name)
        if cents is not None:
            balances.append(f"{label} ${from_cents(cents)}")
    if balances:
        lines.append(" · ".join(balances))

    check, unpaid = _unpaid_check_bills(session, today)
    spending = _latest_balance(session, "SCU Spending") or 0
    bills_env = _latest_balance(session, "SCU Bills") or 0
    if unpaid > 0:
        lines.append(f"{check}-check bills unpaid: ${from_cents(unpaid)}")
        if spending + bills_env < unpaid:
            lines.append(
                f"⚠ Spending+Bills (${from_cents(spending + bills_env)}) can't cover them — "
                "fund bills first or a courtesy fee is coming."
            )
            priority = "high"
            tags = "rotating_light"

    if today.day in (1, 15):
        lines.append(f"Payday day — run /payday. ${from_cents(unpaid)} rides on this check.")

    inbox_count = session.scalar(
        select(func.count()).where(Transaction.category_id.is_(None))
    )
    if inbox_count:
        lines.append(f"Inbox: {inbox_count} uncategorized.")

    transfer_ids = select(Category.id).where(Category.grp == "transfer")
    month_start = today.replace(day=1)
    spent = session.scalar(
        select(func.coalesce(func.sum(-Transaction.amount_cents), 0)).where(
            Transaction.amount_cents < 0,
            Transaction.posted_date >= month_start,
            Transaction.posted_date <= today,
            ~Transaction.category_id.in_(transfer_ids) | Transaction.category_id.is_(None),
        )
    )
    lines.append(f"Spent so far this month: ${from_cents(spent)}.")

    title = (
        "Money Hub — bills at risk"
        if priority == "high"
        else f"Money Hub — {today.strftime('%a %b %-d')}"
    )
    return {"title": title, "body": "\n".join(lines), "priority": priority, "tags": tags}


def send_digest(session: Session, today: date | None = None) -> dict:
    digest = compose_digest(session, today)
    send(digest["title"], digest["body"], digest["priority"], digest["tags"])
    return digest
