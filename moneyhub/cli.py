"""Command-line interface: `moneyhub import`, `moneyhub reconcile`, `moneyhub accounts`."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import make_engine, make_session_factory
from .importing import ImportError_, import_file, import_statement_file
from .models import Account
from .money import from_cents, to_cents
from .reconcile import ReconcileError, reconcile
from .statements import StatementParseError

DEFAULT_DB = "data/moneyhub.db"


def _session(db: str) -> Session:
    path = Path(db)
    if not path.exists():
        sys.exit(f"database not found: {path} — run `alembic upgrade head` first")
    return make_session_factory(make_engine(path))()


def _account(session: Session, name: str) -> Account:
    account = session.scalars(select(Account).where(Account.name == name)).first()
    if account is None:
        names = ", ".join(session.scalars(select(Account.name)))
        sys.exit(f"no account named {name!r}. Known accounts: {names}")
    return account


def cmd_accounts(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        for account in session.scalars(select(Account).order_by(Account.name)):
            print(f"  {account.name}  ({account.institution}, {account.kind})")


def _print_candidates(candidates) -> None:
    print(f"\n  NEEDS REVIEW — {len(candidates)} imported row(s) look like")
    print("  existing manual entries (same amount, within 3 days). Not merged:")
    for c in candidates:
        print(
            f"    imported {c.imported.posted_date} "
            f"{from_cents(c.imported.amount_cents):>10}  {c.imported.merchant_norm!r}"
            f"  ~  manual #{c.manual.id} {c.manual.posted_date} "
            f"{c.manual.description_raw!r}"
        )


def cmd_import_statement(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        try:
            results = import_statement_file(session, Path(args.file))
        except (ImportError_, StatementParseError) as exc:
            sys.exit(f"import refused: {exc}")
    print(f"imported statement {args.file}")
    for r in results:
        if r.skipped_unmapped:
            print(f"  {r.section_name!r} {r.account_name}: skipped (no account mapping)")
            continue
        print(
            f"  {r.account_name:<24} new: {r.new_count:>3}   dup: {r.duplicate_count:>3}"
            f"   cross-source dup: {r.cross_source_count:>3}   snapshots: +{r.snapshots_added}"
        )
        if r.merge_candidates:
            _print_candidates(r.merge_candidates)


def cmd_import(args: argparse.Namespace) -> None:
    if args.file.lower().endswith(".pdf"):
        cmd_import_statement(args)
        return
    if not args.account:
        sys.exit("--account is required for CSV imports")
    with _session(args.db) as session:
        account = _account(session, args.account)
        try:
            result = import_file(session, Path(args.file), account)
        except ImportError_ as exc:
            sys.exit(f"import refused: {exc}")

        print(f"imported {args.file} into {account.name} (batch #{result.batch.id})")
        print(f"  rows in file:  {result.batch.row_count}")
        print(f"  new:           {result.new_count}")
        print(f"  duplicates:    {result.duplicate_count}")
        if result.snapshot:
            print(
                f"  balance snapshot: {from_cents(result.snapshot.balance_cents)} "
                f"as of {result.snapshot.as_of} (from export)"
            )
        if result.merge_candidates:
            _print_candidates(result.merge_candidates)


def cmd_reconcile(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        account = _account(session, args.account)
        stated = to_cents(args.stated) if args.stated is not None else None
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        try:
            report = reconcile(session, account, stated_cents=stated, stated_as_of=as_of)
        except ReconcileError as exc:
            sys.exit(f"cannot reconcile: {exc}")

    print(f"reconcile {report.account_name}")
    print(
        f"  prior balance:   {from_cents(report.prior_balance_cents):>10}"
        f"   (end of {report.prior_as_of})"
    )
    print(f"  + transactions:  {from_cents(report.txn_sum_cents):>10}   ({report.txn_count} rows)")
    print(f"  = computed:      {from_cents(report.computed_cents):>10}")
    print(
        f"  stated balance:  {from_cents(report.stated_balance_cents):>10}"
        f"   (end of {report.stated_as_of})"
    )
    if report.drift_cents == 0:
        print("  drift:                 0.00   ✔ books fully explain the balance")
    else:
        print(f"  drift:           {from_cents(report.drift_cents):>10}   ✘ unrecorded activity")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="moneyhub", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database path (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_accounts = sub.add_parser("accounts", help="list accounts")
    p_accounts.set_defaults(func=cmd_accounts)

    p_import = sub.add_parser(
        "import", help="import a bank export (CSV needs --account; PDF statements do not)"
    )
    p_import.add_argument("file")
    p_import.add_argument("--account", help="account name for CSV imports (see `accounts`)")
    p_import.set_defaults(func=cmd_import)

    p_rec = sub.add_parser("reconcile", help="check that transactions explain the balance")
    p_rec.add_argument("--account", required=True)
    p_rec.add_argument("--stated", help="stated balance, e.g. 128.18 (default: newest snapshot)")
    p_rec.add_argument("--as-of", help="date of the stated balance, YYYY-MM-DD")
    p_rec.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
