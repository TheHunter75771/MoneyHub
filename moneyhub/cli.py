"""Command-line interface: `moneyhub import`, `moneyhub reconcile`, `moneyhub accounts`."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import make_engine, make_session_factory
from .dedup import compute_dedup_hash
from .importing import ImportError_, import_file, import_statement_file
from .models import CATEGORY_SOURCE_MANUAL, Account, Category, Rule, Transaction
from .money import from_cents, to_cents
from .reconcile import ReconcileError, reconcile
from .reports import budget_report, inbox
from .rules import apply_rules, set_category_manual
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


def _category(session, name: str) -> Category:
    category = session.scalars(select(Category).where(Category.name == name)).first()
    if category is None:
        names = ", ".join(session.scalars(select(Category.name)))
        sys.exit(f"no category named {name!r}. Known: {names}")
    return category


def cmd_rules_list(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        for r in session.scalars(select(Rule).order_by(Rule.priority, Rule.id)):
            conds = []
            if r.match_text:
                conds.append(f"text~{r.match_text!r}")
            if r.bank_category:
                conds.append(f"bank={r.bank_category!r}")
            if r.account_id:
                conds.append(f"account#{r.account_id}")
            if r.direction:
                conds.append(r.direction)
            state = "" if r.active else "  [disabled]"
            print(
                f"  #{r.id:<3} p{r.priority:<4} {' & '.join(conds) or '(everything)':<44}"
                f" -> {r.category.name}{state}"
            )


def cmd_rules_add(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        category = _category(session, args.category)
        rule = Rule(
            priority=args.priority,
            name=args.name or "",
            match_text=args.match or "",
            bank_category=args.bank_category or "",
            account_id=_account(session, args.account).id if args.account else None,
            direction=args.direction or "",
            category_id=category.id,
        )
        if not (rule.match_text or rule.bank_category or rule.account_id or rule.direction):
            sys.exit("refusing to add a rule with no conditions — it would match everything")
        session.add(rule)
        session.commit()
        print(f"added rule #{rule.id} -> {category.name}")


def cmd_rules_rm(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        rule = session.get(Rule, args.rule_id)
        if rule is None:
            sys.exit(f"no rule #{args.rule_id}")
        session.delete(rule)
        session.commit()
        print(f"deleted rule #{args.rule_id} — run `moneyhub categorize` to release its rows")


def cmd_categorize(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        stats = apply_rules(session)
        total = session.query(Transaction).count()
        print(
            f"examined {stats.examined} non-manual transactions: "
            f"{stats.categorized} categorized, {stats.cleared} released to inbox"
        )
        uncat = session.scalars(
            select(Transaction).where(Transaction.category_id.is_(None))
        ).all()
        print(f"inbox: {len(uncat)} of {total} transactions uncategorized")


def cmd_inbox(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        groups, total = inbox(session)
    if not total:
        print("inbox empty — every transaction is categorized")
        return
    print(f"{total} uncategorized transaction(s), grouped by merchant:\n")
    for g in groups:
        print(f"  {g.key:<28} x{g.count:<3} {from_cents(g.net_cents):>10}   e.g. {g.sample!r}")
    print("\ncategorize with: moneyhub rules add --match '<text>' --category '<name>'")


def cmd_budget(args: argparse.Namespace) -> None:
    year, month = (int(p) for p in args.month.split("-"))
    with _session(args.db) as session:
        report = budget_report(session, year, month)
    print(f"budget {report.year}-{report.month:02d}")
    print(f"  income:   {from_cents(report.income_cents):>10}")
    print(f"  spending: {from_cents(report.spending_cents):>10}")
    print(f"  transfers moved (excluded): {from_cents(report.transfer_volume_cents)}\n")
    print(f"  {'category':<32} {'grp':<6} {'spent':>9} {'budget':>9} {'left':>9}")
    for line in report.lines:
        budget = from_cents(line.budget_cents) if line.budget_cents is not None else "-"
        left = from_cents(line.remaining_cents) if line.remaining_cents is not None else "-"
        over = "  OVER" if line.remaining_cents is not None and line.remaining_cents < 0 else ""
        print(
            f"  {line.category:<32} {line.grp:<6} {from_cents(line.spent_cents):>9}"
            f" {budget:>9} {left:>9}{over}"
        )
    if report.uncategorized_count:
        print(
            f"  {'(uncategorized)':<32} {'?':<6} {from_cents(-report.uncategorized_cents):>9}"
            f"   ({report.uncategorized_count} rows — see `moneyhub inbox`)"
        )


def cmd_add(args: argparse.Namespace) -> None:
    from datetime import date as date_

    with _session(args.db) as session:
        account = _account(session, args.account)
        amount = to_cents(args.amount)
        posted = date_.fromisoformat(args.date) if args.date else date_.today()
        category = _category(session, args.category) if args.category else None
        txn = Transaction(
            account_id=account.id,
            posted_date=posted,
            amount_cents=amount,
            description_raw=args.description,
            category_id=category.id if category else None,
            category_source=CATEGORY_SOURCE_MANUAL if category else None,
            dedup_hash=compute_dedup_hash(account.id, posted, amount, args.description),
        )
        session.add(txn)
        session.commit()
        print(
            f"added #{txn.id}: {posted} {from_cents(amount)} {args.description!r}"
            f" [{category.name if category else 'uncategorized'}] on {account.name}"
        )


def cmd_set_category(args: argparse.Namespace) -> None:
    with _session(args.db) as session:
        txn = session.get(Transaction, args.txn_id)
        if txn is None:
            sys.exit(f"no transaction #{args.txn_id}")
        category = _category(session, args.category)
        set_category_manual(session, txn, category.id)
        print(f"#{txn.id} {txn.description_raw!r} -> {category.name} (manual, sticky)")


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

    p_rules = sub.add_parser("rules", help="manage categorization rules")
    rules_sub = p_rules.add_subparsers(dest="rules_command", required=True)
    r_list = rules_sub.add_parser("list")
    r_list.set_defaults(func=cmd_rules_list)
    r_add = rules_sub.add_parser("add")
    r_add.add_argument("--match", help="case-insensitive substring of merchant/description")
    r_add.add_argument("--bank-category", help="exact bank-supplied category")
    r_add.add_argument("--account", help="only transactions on this account")
    r_add.add_argument("--direction", choices=["in", "out"])
    r_add.add_argument("--category", required=True, help="category to assign")
    r_add.add_argument("--priority", type=int, default=100, help="lower fires first")
    r_add.add_argument("--name", help="optional label")
    r_add.set_defaults(func=cmd_rules_add)
    r_rm = rules_sub.add_parser("rm")
    r_rm.add_argument("rule_id", type=int)
    r_rm.set_defaults(func=cmd_rules_rm)

    p_cat = sub.add_parser("categorize", help="run the rules engine over all transactions")
    p_cat.set_defaults(func=cmd_categorize)

    p_inbox = sub.add_parser("inbox", help="uncategorized transactions grouped by merchant")
    p_inbox.set_defaults(func=cmd_inbox)

    p_budget = sub.add_parser("budget", help="per-category spend vs budget for a month")
    p_budget.add_argument("--month", required=True, help="YYYY-MM")
    p_budget.set_defaults(func=cmd_budget)

    p_add = sub.add_parser("add", help="manually add a transaction (e.g. cash spend)")
    p_add.add_argument("--account", required=True)
    p_add.add_argument("--amount", required=True, help="signed: -12.34 spend, 12.34 inflow")
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--date", help="YYYY-MM-DD (default today)")
    p_add.add_argument("--category")
    p_add.set_defaults(func=cmd_add)

    p_set = sub.add_parser("set-category", help="manually categorize one transaction (sticky)")
    p_set.add_argument("txn_id", type=int)
    p_set.add_argument("category")
    p_set.set_defaults(func=cmd_set_category)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
