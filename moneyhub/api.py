"""FastAPI layer: JSON API + the Nocturne web UI (moneyhub/web).

Exposure model: bind to localhost (or a Tailscale address) only. There is no
application auth — the network boundary IS the boundary, per the project's
Tailscale-only rule. Never expose this publicly.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import make_engine, make_session_factory
from .importing import (
    FUZZY_MATCH_WINDOW_DAYS,
    ImportError_,
    import_file,
    import_statement_file,
    sync_simplefin,
)
from .models import (
    ACCOUNT_KINDS,
    CATEGORY_GROUPS,
    CATEGORY_SOURCE_MANUAL,
    Account,
    BalanceSnapshot,
    Bill,
    BillPayment,
    Category,
    Debt,
    ImportBatch,
    Rule,
    Transaction,
)
from .money import from_cents
from .reconcile import ReconcileError, reconcile
from .reports import budget_report, inbox
from .rules import apply_rules, set_category_manual
from .simplefin import SimpleFinError
from .statements import StatementParseError

WEB_DIR = Path(__file__).parent / "web"
STALE_AFTER_DAYS = 2  # synced accounts older than this are flagged


# --- request bodies ---------------------------------------------------------


class CategoryPatch(BaseModel):
    cap_cents: int | None = None
    name: str | None = None
    active: bool | None = None


class CategoryIn(BaseModel):
    name: str
    grp: str
    cap_cents: int | None = None


class TransactionPatch(BaseModel):
    category_id: int | None = None
    set_category: bool = False  # distinguishes "clear category" from "not sent"
    notes: str | None = None


class TransactionIn(BaseModel):
    account_id: int
    amount_cents: int
    posted_date: date
    description: str
    category_id: int | None = None


class RuleIn(BaseModel):
    match_text: str
    category_id: int
    priority: int = 50


class InboxAssign(BaseModel):
    txn_ids: list[int]
    key: str
    category_id: int
    create_rule: bool = False


class MergeResolve(BaseModel):
    manual_id: int
    action: str  # approve | dismiss


class BillIn(BaseModel):
    name: str
    amount_cents: int
    due_rule: str
    category_id: int | None = None
    match_pattern: str = ""


class BillPatch(BaseModel):
    name: str | None = None
    amount_cents: int | None = None
    due_rule: str | None = None
    category_id: int | None = None
    set_category: bool = False
    active: bool | None = None
    match_pattern: str | None = None


class BillPaidIn(BaseModel):
    period: str  # YYYY-MM
    paid: bool


class DebtIn(BaseModel):
    name: str
    balance_cents: int
    note: str = ""


class DebtPatch(BaseModel):
    balance_cents: int | None = None
    note: str | None = None
    status: str | None = None  # open | paid_off


class AccountIn(BaseModel):
    name: str
    institution: str = ""
    kind: str


class AccountPatch(BaseModel):
    name: str | None = None
    institution: str | None = None
    is_active: bool | None = None
    simplefin_id: str | None = None
    set_simplefin: bool = False


class RulePatch(BaseModel):
    active: bool | None = None
    priority: int | None = None
    match_text: str | None = None
    category_id: int | None = None


class SnapshotIn(BaseModel):
    account_id: int
    as_of: date
    balance_cents: int


_DAY_RE = re.compile(r"^(\d{1,2})(st|nd|rd|th)")


def _check_of(due_rule: str) -> str:
    m = _DAY_RE.match(due_rule.strip())
    if not m:
        return "—"
    return "1st" if int(m.group(1)) <= 14 else "15th"


# --- helpers ----------------------------------------------------------------


def _latest_snapshots(session: Session) -> dict[int, BalanceSnapshot]:
    out: dict[int, BalanceSnapshot] = {}
    for snap in session.scalars(
        select(BalanceSnapshot).order_by(BalanceSnapshot.as_of, BalanceSnapshot.id)
    ):
        out[snap.account_id] = snap
    return out


def _source_of(txn: Transaction, batch_names: dict[int, str]) -> str:
    if txn.fitid and txn.fitid.startswith("sfin:"):
        return "sync"
    if txn.import_id is not None:
        name = batch_names.get(txn.import_id, "")
        if name.endswith(".pdf"):
            return "statement"
        if "sync" in name:
            return "sync"
        return "csv"
    return "manual"


def _account_states(session: Session) -> list[dict]:
    snaps = _latest_snapshots(session)
    today = date.today()
    out = []
    for account in session.scalars(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
    ):
        snap = snaps.get(account.id)
        state, warn = "", False
        if snap is None:
            state = "no data"
        else:
            age = (today - snap.as_of).days
            if account.simplefin_id and age >= STALE_AFTER_DAYS:
                state, warn = f"stale {age}d", True
            else:
                try:
                    report = reconcile(
                        session, account, stated_cents=snap.balance_cents, stated_as_of=snap.as_of
                    )
                    if report.drift_cents != 0:
                        state, warn = f"drift ${from_cents(abs(report.drift_cents))}", True
                except ReconcileError:
                    pass  # single snapshot — nothing to check against yet
        out.append(
            {
                "id": account.id,
                "name": account.name,
                "institution": account.institution,
                "kind": account.kind,
                "balance_cents": snap.balance_cents if snap else None,
                "as_of": snap.as_of.isoformat() if snap else None,
                "state": state,
                "warn": warn,
                "simplefin_mapped": account.simplefin_id is not None,
            }
        )
    return out


def _months(session: Session) -> list[str]:
    rows = session.execute(
        select(func.strftime("%Y-%m", Transaction.posted_date)).distinct()
    ).scalars()
    return sorted(rows)


def _merge_candidates(session: Session) -> list[dict]:
    manual = session.scalars(
        select(Transaction).where(
            Transaction.import_id.is_(None),
            Transaction.fitid.is_(None),
            Transaction.merge_dismissed.is_(False),
        )
    ).all()
    if not manual:
        return []
    batch_names = dict(session.execute(select(ImportBatch.id, ImportBatch.filename)).all())
    window = timedelta(days=FUZZY_MATCH_WINDOW_DAYS)
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    out = []
    for entry in manual:
        twin = session.scalars(
            select(Transaction).where(
                Transaction.account_id == entry.account_id,
                Transaction.amount_cents == entry.amount_cents,
                Transaction.id != entry.id,
                (Transaction.import_id.is_not(None)) | (Transaction.fitid.is_not(None)),
                Transaction.posted_date >= entry.posted_date - window,
                Transaction.posted_date <= entry.posted_date + window,
            )
        ).first()
        if twin is not None:
            out.append(
                {
                    "manual_id": entry.id,
                    "manual": _txn_dict(entry, accounts, batch_names),
                    "imported": _txn_dict(twin, accounts, batch_names),
                }
            )
    return out


def _txn_dict(txn: Transaction, accounts: dict[int, str], batch_names: dict[int, str]) -> dict:
    return {
        "id": txn.id,
        "date": txn.posted_date.isoformat(),
        "description": txn.merchant_norm or txn.description_raw,
        "raw": txn.description_raw,
        "account": accounts.get(txn.account_id, "?"),
        "account_id": txn.account_id,
        "amount_cents": txn.amount_cents,
        "category_id": txn.category_id,
        "source": _source_of(txn, batch_names),
        "notes": txn.notes,
    }


# --- app --------------------------------------------------------------------


def create_app(db_path: str | Path = "data/moneyhub.db") -> FastAPI:
    engine = make_engine(db_path)
    factory = make_session_factory(engine)

    def get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI(title="Money Hub", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/state")
    def state(db: Session = Depends(get_db)) -> dict:
        last_sync = db.scalars(
            select(ImportBatch)
            .where(ImportBatch.filename.like("simplefin sync%"))
            .order_by(ImportBatch.imported_at.desc())
        ).first()
        inbox_count = db.scalar(
            select(func.count()).where(Transaction.category_id.is_(None))
        )
        accounts = _account_states(db)
        stale = sum(1 for a in accounts if a["state"].startswith("stale"))
        return {
            "sync_last_at": last_sync.imported_at.isoformat() if last_sync else None,
            "stale_accounts": stale,
            "inbox_count": inbox_count,
            "months": _months(db),
        }

    @app.post("/api/sync")
    def run_sync(db: Session = Depends(get_db)) -> dict:
        try:
            results, unmapped = sync_simplefin(db)
        except SimpleFinError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if any(r.new_count for r in results):
            apply_rules(db)
        return {
            "new": sum(r.new_count for r in results),
            "accounts": len(results),
            "unmapped": len(unmapped),
        }

    @app.get("/api/accounts")
    def accounts(db: Session = Depends(get_db)) -> list[dict]:
        return _account_states(db)

    @app.get("/api/categories")
    def categories(db: Session = Depends(get_db)) -> list[dict]:
        return [
            {
                "id": c.id,
                "name": c.name,
                "grp": c.grp,
                "cap_cents": c.monthly_budget_cents,
                "active": c.active,
            }
            for c in db.scalars(select(Category).order_by(Category.name))
        ]

    @app.post("/api/categories", status_code=201)
    def add_category(body: CategoryIn, db: Session = Depends(get_db)) -> dict:
        if body.grp not in CATEGORY_GROUPS:
            raise HTTPException(422, f"grp must be one of {CATEGORY_GROUPS}")
        if db.scalars(select(Category).where(Category.name == body.name)).first():
            raise HTTPException(409, "category already exists")
        category = Category(name=body.name, grp=body.grp, monthly_budget_cents=body.cap_cents)
        db.add(category)
        db.commit()
        return {"id": category.id}

    @app.patch("/api/categories/{category_id}")
    def patch_category(
        category_id: int, body: CategoryPatch, db: Session = Depends(get_db)
    ) -> dict:
        category = db.get(Category, category_id)
        if category is None:
            raise HTTPException(404, "no such category")
        if body.cap_cents is not None:
            category.monthly_budget_cents = body.cap_cents
        if body.name is not None:
            category.name = body.name
        if body.active is not None:
            category.active = body.active
        db.commit()
        return {"ok": True}

    @app.get("/api/budget")
    def budget(month: str, db: Session = Depends(get_db)) -> dict:
        year, mon = (int(p) for p in month.split("-"))
        report = budget_report(db, year, mon)
        cats = {
            c.name: c
            for c in db.scalars(select(Category).where(Category.active.is_(True)))
        }
        spent_by_name = {line.category: line for line in report.lines}
        rows = []
        for name, c in sorted(cats.items()):
            if c.grp in ("transfer", "income"):
                continue
            line = spent_by_name.get(name)
            spent = line.spent_cents if line else 0
            cap = c.monthly_budget_cents
            rows.append(
                {
                    "id": c.id,
                    "name": name,
                    "grp": c.grp,
                    "spent_cents": spent,
                    "cap_cents": cap,
                    "remaining_cents": (cap - spent) if cap is not None else None,
                    "over": cap is not None and spent > cap,
                }
            )
        rows.sort(key=lambda r: -r["spent_cents"])
        cap_total = sum(r["cap_cents"] or 0 for r in rows)
        return {
            "month": month,
            "income_cents": report.income_cents,
            "spending_cents": report.spending_cents,
            "transfer_cents": report.transfer_volume_cents,
            "uncategorized_cents": report.uncategorized_cents,
            "uncategorized_count": report.uncategorized_count,
            "cap_total_cents": cap_total,
            "categories": rows,
        }

    @app.get("/api/transactions")
    def transactions(
        q: str = "",
        account_id: int | None = None,
        category_id: int | None = None,
        uncategorized: bool = False,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 100,
        offset: int = 0,
        db: Session = Depends(get_db),
    ) -> dict:
        stmt = select(Transaction)
        if account_id is not None:
            stmt = stmt.where(Transaction.account_id == account_id)
        if date_from is not None:
            stmt = stmt.where(Transaction.posted_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(Transaction.posted_date <= date_to)
        if uncategorized:
            stmt = stmt.where(Transaction.category_id.is_(None))
        elif category_id is not None:
            stmt = stmt.where(Transaction.category_id == category_id)
        if q:
            like = f"%{q}%"
            matching_cats = select(Category.id).where(Category.name.like(like))
            stmt = stmt.where(
                Transaction.description_raw.like(like)
                | Transaction.merchant_norm.like(like)
                | Transaction.category_id.in_(matching_cats)
            )
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = db.scalars(
            stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
            .limit(min(limit, 500))
            .offset(offset)
        ).all()
        accounts = {a.id: a.name for a in db.scalars(select(Account))}
        batch_names = dict(db.execute(select(ImportBatch.id, ImportBatch.filename)).all())
        return {"total": total, "rows": [_txn_dict(t, accounts, batch_names) for t in rows]}

    @app.patch("/api/transactions/{txn_id}")
    def patch_transaction(
        txn_id: int, body: TransactionPatch, db: Session = Depends(get_db)
    ) -> dict:
        txn = db.get(Transaction, txn_id)
        if txn is None:
            raise HTTPException(404, "no such transaction")
        if body.set_category:
            set_category_manual(db, txn, body.category_id)
        if body.notes is not None:
            txn.notes = body.notes
        db.commit()
        return {"ok": True}

    @app.post("/api/transactions", status_code=201)
    def add_transaction(body: TransactionIn, db: Session = Depends(get_db)) -> dict:
        from .dedup import compute_dedup_hash

        if db.get(Account, body.account_id) is None:
            raise HTTPException(422, "no such account")
        txn = Transaction(
            account_id=body.account_id,
            posted_date=body.posted_date,
            amount_cents=body.amount_cents,
            description_raw=body.description,
            category_id=body.category_id,
            category_source=CATEGORY_SOURCE_MANUAL if body.category_id else None,
            dedup_hash=compute_dedup_hash(
                body.account_id, body.posted_date, body.amount_cents, body.description
            ),
        )
        db.add(txn)
        db.commit()
        return {"id": txn.id}

    @app.post("/api/rules", status_code=201)
    def add_rule(body: RuleIn, db: Session = Depends(get_db)) -> dict:
        if db.get(Category, body.category_id) is None:
            raise HTTPException(422, "no such category")
        if not body.match_text.strip():
            raise HTTPException(422, "match_text required")
        rule = Rule(
            match_text=body.match_text.strip(),
            category_id=body.category_id,
            priority=body.priority,
        )
        db.add(rule)
        db.commit()
        stats = apply_rules(db)
        return {"id": rule.id, "categorized": stats.categorized}

    @app.get("/api/inbox")
    def inbox_view(db: Session = Depends(get_db)) -> dict:
        groups, total = inbox(db)
        return {
            "total": total,
            "groups": [
                {
                    "key": g.key,
                    "count": g.count,
                    "net_cents": g.net_cents,
                    "sample": g.sample,
                    "txn_ids": g.txn_ids,
                }
                for g in groups
            ],
            "merges": _merge_candidates(db),
        }

    @app.post("/api/inbox/assign")
    def inbox_assign(body: InboxAssign, db: Session = Depends(get_db)) -> dict:
        if db.get(Category, body.category_id) is None:
            raise HTTPException(422, "no such category")
        for txn_id in body.txn_ids:
            txn = db.get(Transaction, txn_id)
            if txn is not None:
                txn.category_id = body.category_id
                txn.category_source = CATEGORY_SOURCE_MANUAL
        db.commit()
        rule_id = None
        if body.create_rule and body.key.strip():
            rule = Rule(match_text=body.key.strip().lower(), category_id=body.category_id)
            db.add(rule)
            db.commit()
            apply_rules(db)
            rule_id = rule.id
        return {"ok": True, "rule_id": rule_id}

    @app.post("/api/merges/resolve")
    def resolve_merge(body: MergeResolve, db: Session = Depends(get_db)) -> dict:
        txn = db.get(Transaction, body.manual_id)
        if txn is None:
            raise HTTPException(404, "no such transaction")
        if body.action == "approve":
            db.delete(txn)  # the imported twin is the surviving truth
        elif body.action == "dismiss":
            txn.merge_dismissed = True
        else:
            raise HTTPException(422, "action must be approve or dismiss")
        db.commit()
        return {"ok": True}

    @app.get("/api/recent")
    def recent(db: Session = Depends(get_db)) -> list[dict]:
        rows = db.scalars(
            select(Transaction)
            .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
            .limit(10)
        ).all()
        accounts = {a.id: a.name for a in db.scalars(select(Account))}
        batch_names = dict(db.execute(select(ImportBatch.id, ImportBatch.filename)).all())
        categories = {c.id: c.name for c in db.scalars(select(Category))}
        out = []
        for t in rows:
            d = _txn_dict(t, accounts, batch_names)
            d["category"] = categories.get(t.category_id, "uncategorized")
            out.append(d)
        return out

    # --- bills ---------------------------------------------------------

    @app.get("/api/bills")
    def bills(period: str, db: Session = Depends(get_db)) -> dict:
        payments = {
            p.bill_id: p
            for p in db.scalars(select(BillPayment).where(BillPayment.period == period))
        }
        matched_txn_ids = [
            p.transaction_id for p in payments.values() if p.transaction_id is not None
        ]
        txn_info = {
            t.id: t
            for t in db.scalars(
                select(Transaction).where(Transaction.id.in_(matched_txn_ids))
            )
        } if matched_txn_ids else {}
        categories = {c.id: c.name for c in db.scalars(select(Category))}
        rows = []
        paid_total = due_total = 0
        active_bills = db.scalars(
            select(Bill).where(Bill.active.is_(True)).order_by(Bill.due_rule, Bill.name)
        )
        for b in active_bills:
            payment = payments.get(b.id)
            paid = payment is not None
            if paid:
                paid_total += b.amount_cents
            else:
                due_total += b.amount_cents
            matched = None
            if payment is not None and payment.transaction_id in txn_info:
                t = txn_info[payment.transaction_id]
                matched = {
                    "description": t.merchant_norm or t.description_raw,
                    "date": t.posted_date.isoformat(),
                    "amount_cents": t.amount_cents,
                }
            rows.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "amount_cents": b.amount_cents,
                    "due_rule": b.due_rule,
                    "category": categories.get(b.category_id),
                    "check": _check_of(b.due_rule),
                    "paid": paid,
                    "match_pattern": b.match_pattern,
                    "matched": matched,
                }
            )
        rows.sort(key=lambda r: ({"1st": 0, "15th": 1}.get(r["check"], 2), r["name"]))
        return {
            "period": period,
            "rows": rows,
            "paid_cents": paid_total,
            "remaining_cents": due_total,
        }

    @app.post("/api/bills", status_code=201)
    def add_bill(body: BillIn, db: Session = Depends(get_db)) -> dict:
        bill = Bill(
            name=body.name,
            amount_cents=body.amount_cents,
            due_rule=body.due_rule,
            category_id=body.category_id,
            match_pattern=body.match_pattern.strip(),
        )
        db.add(bill)
        db.commit()
        return {"id": bill.id}

    @app.post("/api/bills/autocheck")
    def bills_autocheck(db: Session = Depends(get_db)) -> dict:
        from .billmatch import auto_check_bills

        created = auto_check_bills(db)
        return {"checked": created}

    @app.patch("/api/bills/{bill_id}")
    def patch_bill(bill_id: int, body: BillPatch, db: Session = Depends(get_db)) -> dict:
        bill = db.get(Bill, bill_id)
        if bill is None:
            raise HTTPException(404, "no such bill")
        if body.name is not None:
            bill.name = body.name
        if body.amount_cents is not None:
            bill.amount_cents = body.amount_cents
        if body.due_rule is not None:
            bill.due_rule = body.due_rule
        if body.set_category:
            bill.category_id = body.category_id
        if body.active is not None:
            bill.active = body.active
        if body.match_pattern is not None:
            bill.match_pattern = body.match_pattern.strip()
        db.commit()
        return {"ok": True}

    @app.post("/api/bills/{bill_id}/paid")
    def set_bill_paid(bill_id: int, body: BillPaidIn, db: Session = Depends(get_db)) -> dict:
        if db.get(Bill, bill_id) is None:
            raise HTTPException(404, "no such bill")
        existing = db.scalars(
            select(BillPayment).where(
                BillPayment.bill_id == bill_id, BillPayment.period == body.period
            )
        ).first()
        if body.paid and existing is None:
            db.add(BillPayment(bill_id=bill_id, period=body.period))
        elif not body.paid and existing is not None:
            db.delete(existing)
        db.commit()
        return {"ok": True}

    # --- debts ---------------------------------------------------------

    @app.get("/api/debts")
    def debts(db: Session = Depends(get_db)) -> dict:
        rows = [
            {
                "id": d.id,
                "name": d.name,
                "balance_cents": d.balance_cents,
                "note": d.note,
                "status": d.status,
            }
            for d in db.scalars(select(Debt).order_by(Debt.balance_cents.desc()))
        ]
        total = sum(r["balance_cents"] for r in rows if r["status"] == "open")
        return {"rows": rows, "total_cents": total}

    @app.post("/api/debts", status_code=201)
    def add_debt(body: DebtIn, db: Session = Depends(get_db)) -> dict:
        if db.scalars(select(Debt).where(Debt.name == body.name)).first():
            raise HTTPException(409, "debt already exists")
        debt = Debt(name=body.name, balance_cents=body.balance_cents, note=body.note)
        db.add(debt)
        db.commit()
        return {"id": debt.id}

    @app.patch("/api/debts/{debt_id}")
    def patch_debt(debt_id: int, body: DebtPatch, db: Session = Depends(get_db)) -> dict:
        debt = db.get(Debt, debt_id)
        if debt is None:
            raise HTTPException(404, "no such debt")
        if body.balance_cents is not None:
            debt.balance_cents = body.balance_cents
        if body.note is not None:
            debt.note = body.note
        if body.status is not None:
            if body.status not in ("open", "paid_off"):
                raise HTTPException(422, "status must be open or paid_off")
            debt.status = body.status
        db.commit()
        return {"ok": True}

    # --- admin: accounts, imports, rules, reconcile, snapshots ----------

    @app.post("/api/accounts", status_code=201)
    def add_account(body: AccountIn, db: Session = Depends(get_db)) -> dict:
        if body.kind not in ACCOUNT_KINDS:
            raise HTTPException(422, f"kind must be one of {ACCOUNT_KINDS}")
        if db.scalars(select(Account).where(Account.name == body.name)).first():
            raise HTTPException(409, "account already exists")
        account = Account(name=body.name, institution=body.institution, kind=body.kind)
        db.add(account)
        db.commit()
        return {"id": account.id}

    @app.patch("/api/accounts/{account_id}")
    def patch_account(
        account_id: int, body: AccountPatch, db: Session = Depends(get_db)
    ) -> dict:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        if body.name is not None:
            account.name = body.name
        if body.institution is not None:
            account.institution = body.institution
        if body.is_active is not None:
            account.is_active = body.is_active
        if body.set_simplefin:
            account.simplefin_id = body.simplefin_id
        db.commit()
        return {"ok": True}

    @app.get("/api/imports")
    def imports(db: Session = Depends(get_db)) -> list[dict]:
        accounts = {a.id: a.name for a in db.scalars(select(Account))}
        return [
            {
                "id": b.id,
                "filename": b.filename,
                "account": accounts.get(b.account_id),
                "imported_at": b.imported_at.isoformat() if b.imported_at else None,
                "rows": b.row_count,
                "new": b.new_count,
                "duplicates": b.duplicate_count,
            }
            for b in db.scalars(
                select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(50)
            )
        ]

    @app.get("/api/rules")
    def rules(db: Session = Depends(get_db)) -> list[dict]:
        hits = dict(
            db.execute(
                select(Transaction.category_source, func.count())
                .where(Transaction.category_source.like("rule:%"))
                .group_by(Transaction.category_source)
            ).all()
        )
        categories = {c.id: c.name for c in db.scalars(select(Category))}
        accounts = {a.id: a.name for a in db.scalars(select(Account))}
        out = []
        for r in db.scalars(select(Rule).order_by(Rule.priority, Rule.id)):
            conds = []
            if r.match_text:
                conds.append(f"text ~ {r.match_text!r}")
            if r.bank_category:
                conds.append(f"bank = {r.bank_category!r}")
            if r.account_id:
                conds.append(f"account = {accounts.get(r.account_id, '?')}")
            if r.direction:
                conds.append(r.direction)
            out.append(
                {
                    "id": r.id,
                    "priority": r.priority,
                    "conditions": " & ".join(conds),
                    "category": categories.get(r.category_id, "?"),
                    "hits": hits.get(f"rule:{r.id}", 0),
                    "active": r.active,
                }
            )
        return out

    @app.patch("/api/rules/{rule_id}")
    def patch_rule(rule_id: int, body: RulePatch, db: Session = Depends(get_db)) -> dict:
        rule = db.get(Rule, rule_id)
        if rule is None:
            raise HTTPException(404, "no such rule")
        if body.active is not None:
            rule.active = body.active
        if body.priority is not None:
            rule.priority = body.priority
        if body.match_text is not None:
            rule.match_text = body.match_text
        if body.category_id is not None:
            rule.category_id = body.category_id
        db.commit()
        stats = apply_rules(db)
        return {"ok": True, "categorized": stats.categorized}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> dict:
        rule = db.get(Rule, rule_id)
        if rule is None:
            raise HTTPException(404, "no such rule")
        db.delete(rule)
        db.commit()
        stats = apply_rules(db)
        return {"ok": True, "released": stats.cleared}

    @app.post("/api/categorize")
    def categorize_now(db: Session = Depends(get_db)) -> dict:
        stats = apply_rules(db)
        return {
            "examined": stats.examined,
            "categorized": stats.categorized,
            "released": stats.cleared,
        }

    @app.get("/api/reconcile")
    def reconcile_all(db: Session = Depends(get_db)) -> list[dict]:
        snaps = _latest_snapshots(db)
        out = []
        for account in db.scalars(select(Account).where(Account.is_active.is_(True))):
            snap = snaps.get(account.id)
            if snap is None:
                continue
            try:
                report = reconcile(
                    db, account, stated_cents=snap.balance_cents, stated_as_of=snap.as_of
                )
                out.append(
                    {
                        "account": account.name,
                        "prior_as_of": report.prior_as_of.isoformat(),
                        "prior_cents": report.prior_balance_cents,
                        "txn_count": report.txn_count,
                        "txn_sum_cents": report.txn_sum_cents,
                        "computed_cents": report.computed_cents,
                        "stated_as_of": report.stated_as_of.isoformat(),
                        "stated_cents": report.stated_balance_cents,
                        "drift_cents": report.drift_cents,
                    }
                )
            except ReconcileError as exc:
                out.append({"account": account.name, "error": str(exc)})
        return out

    @app.post("/api/snapshots", status_code=201)
    def add_snapshot(body: SnapshotIn, db: Session = Depends(get_db)) -> dict:
        if db.get(Account, body.account_id) is None:
            raise HTTPException(422, "no such account")
        snap = BalanceSnapshot(
            account_id=body.account_id,
            as_of=body.as_of,
            balance_cents=body.balance_cents,
            source="manual",
        )
        db.add(snap)
        db.commit()
        return {"id": snap.id}

    @app.post("/api/import")
    async def upload_import(
        file: UploadFile = File(...),
        account_id: int | None = Form(default=None),
        db: Session = Depends(get_db),
    ) -> dict:
        suffix = Path(file.filename or "upload").suffix.lower()
        content = await file.read()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            if suffix == ".pdf":
                import hashlib

                results = import_statement_file(db, tmp_path)
                batch = db.scalars(
                    select(ImportBatch).where(
                        ImportBatch.file_sha256 == hashlib.sha256(content).hexdigest()
                    )
                ).first()
                if batch is not None:
                    batch.filename = file.filename or batch.filename
                    db.commit()
                sections = [
                    {
                        "account": r.account_name,
                        "new": r.new_count,
                        "duplicates": r.duplicate_count,
                        "cross_source": r.cross_source_count,
                        "snapshots": r.snapshots_added,
                        "skipped": r.skipped_unmapped,
                    }
                    for r in results
                ]
            else:
                if account_id is None:
                    raise HTTPException(422, "CSV imports need an account")
                account = db.get(Account, account_id)
                if account is None:
                    raise HTTPException(422, "no such account")
                result = import_file(db, tmp_path, account)
                # The batch records the temp filename; keep the real one.
                result.batch.filename = file.filename or result.batch.filename
                db.commit()
                sections = [
                    {
                        "account": account.name,
                        "new": result.new_count,
                        "duplicates": result.duplicate_count,
                        "cross_source": 0,
                        "snapshots": 1 if result.snapshot else 0,
                        "merge_candidates": len(result.merge_candidates),
                    }
                ]
            apply_rules(db)
        except (ImportError_, StatementParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        return {"sections": sections}

    # --- phase 4: payday, transfer pairs, notion -------------------------

    @app.get("/api/payday")
    def payday(period: str, check: str = "1st", db: Session = Depends(get_db)) -> dict:
        bills_data = bills(period, db)
        mine = [r for r in bills_data["rows"] if r["check"] == check]
        other = [r for r in bills_data["rows"] if r["check"] == "—"]
        unpaid = sum(r["amount_cents"] for r in mine if not r["paid"])

        year, mon = (int(p) for p in period.split("-"))
        start, end = date(year, mon, 1), (
            date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
        )
        income_cat_ids = select(Category.id).where(Category.grp == "income")
        income = db.scalar(
            select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
                Transaction.category_id.in_(income_cat_ids),
                Transaction.posted_date >= start,
                Transaction.posted_date < end,
                Transaction.amount_cents > 0,
            )
        )
        fee_window = date.today() - timedelta(days=60)
        fee_like = "%courtesy pay%"
        fee_sum, fee_count = db.execute(
            select(func.coalesce(func.sum(Transaction.amount_cents), 0), func.count()).where(
                Transaction.description_raw.like(fee_like),
                Transaction.posted_date >= fee_window,
            )
        ).one()
        return {
            "period": period,
            "check": check,
            "bills": mine,
            "irregular_bills": other,
            "unpaid_cents": unpaid,
            "income_detected_cents": income,
            "accounts": _account_states(db),
            "fees_60d_cents": -fee_sum,
            "fees_60d_count": fee_count,
        }

    @app.get("/api/transfers")
    def transfers_view(db: Session = Depends(get_db)) -> dict:
        from .transfers import _transfer_txns, transfer_groups

        accounts = {a.id: a.name for a in db.scalars(select(Account))}
        groups = transfer_groups(db)
        for g in groups:
            g["from_account"] = accounts.get(g.pop("from_account_id"))
            g["to_account"] = accounts.get(g.pop("to_account_id"))
            for leg in g["legs"]:
                leg["account"] = accounts.get(leg.pop("account_id"))
        unmatched = [
            {
                "id": t.id,
                "account": accounts.get(t.account_id),
                "date": t.posted_date.isoformat(),
                "amount_cents": t.amount_cents,
                "description": t.description_raw,
            }
            for t in _transfer_txns(db, unlinked_only=True)
        ]
        return {"groups": groups, "unmatched": unmatched}

    @app.post("/api/transfers/pair")
    def transfers_pair(db: Session = Depends(get_db)) -> dict:
        from .transfers import pair_transfers

        result = pair_transfers(db)
        return {
            "groups_created": result.groups_created,
            "legs_linked": result.legs_linked,
            "unmatched": len(result.unmatched),
        }

    @app.get("/api/notion/status")
    def notion_status() -> dict:
        from .notion import status as notion_status_fn

        return notion_status_fn()

    @app.post("/api/notion/push")
    def notion_push(db: Session = Depends(get_db)) -> dict:
        from .notion import NotionError, push_snapshot

        try:
            return push_snapshot(db)
        except NotionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/{path:path}", include_in_schema=False)
        def index(path: str) -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


def main() -> None:  # pragma: no cover - thin runner
    import uvicorn

    host = os.environ.get("MONEYHUB_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("MONEYHUB_WEB_PORT", "8710"))
    uvicorn.run(
        create_app(os.environ.get("MONEYHUB_DB_PATH", "data/moneyhub.db")),
        host=host,
        port=port,
    )
