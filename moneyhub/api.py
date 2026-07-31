"""FastAPI layer: JSON API + the Nocturne web UI (moneyhub/web).

Exposure model: bind to localhost (or a Tailscale address) only. There is no
application auth — the network boundary IS the boundary, per the project's
Tailscale-only rule. Never expose this publicly.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import make_engine, make_session_factory
from .importing import FUZZY_MATCH_WINDOW_DAYS, sync_simplefin
from .models import (
    CATEGORY_GROUPS,
    CATEGORY_SOURCE_MANUAL,
    Account,
    BalanceSnapshot,
    Category,
    ImportBatch,
    Rule,
    Transaction,
)
from .money import from_cents
from .reconcile import ReconcileError, reconcile
from .reports import budget_report, inbox
from .rules import apply_rules, set_category_manual
from .simplefin import SimpleFinError

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
