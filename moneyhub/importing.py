"""File importers with layered deduplication.

Dedup layers, in order:
1. File level — a file whose sha256 was already imported is rejected outright.
2. FITID — rows carrying the bank's stable transaction id are skipped when
   (account_id, fitid) already exists. Exact and authoritative.
3. Row hash — rows without a FITID dedup by stable content hash, with
   count-matching so two legitimately identical purchases on the same day
   both survive while re-imported overlaps do not.
4. Fuzzy — imported rows that look like existing *manual* entries (same
   amount, date within ±3 days) are reported as merge candidates for human
   review, never silently merged.

Supported formats:
- Service CU CSV export ("Transaction ID","Posting Date",...). The export's
  Reference Number is used as the FITID and the running Balance column of the
  newest row becomes an import-sourced balance snapshot.
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .dedup import compute_dedup_hash
from .models import Account, BalanceSnapshot, ImportBatch, Transaction
from .money import to_cents

SCU_HEADER_FIELDS = {"Transaction ID", "Posting Date", "Amount", "Description"}
FUZZY_MATCH_WINDOW_DAYS = 3


class ImportError_(RuntimeError):
    """Import rejected (unknown format, duplicate file, bad rows)."""


@dataclass
class ParsedRow:
    posted_date: date
    amount_cents: int
    description_raw: str
    merchant: str = ""
    fitid: str | None = None
    bank_category: str = ""
    balance_after_cents: int | None = None
    status: str = "posted"


@dataclass
class MergeCandidate:
    imported: Transaction
    manual: Transaction


@dataclass
class ImportResult:
    batch: ImportBatch
    new_count: int
    duplicate_count: int
    merge_candidates: list[MergeCandidate] = field(default_factory=list)
    snapshot: BalanceSnapshot | None = None


def detect_format(text: str) -> str:
    header = text.lstrip().splitlines()[0] if text.strip() else ""
    fields = {f.strip().strip('"') for f in next(csv.reader(io.StringIO(header)), [])}
    if SCU_HEADER_FIELDS <= fields:
        return "scu-csv"
    raise ImportError_(
        "unrecognized file format — expected a Service CU CSV export "
        f"(missing columns: {sorted(SCU_HEADER_FIELDS - fields)})"
    )


def parse_scu_csv(text: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    for record in csv.DictReader(io.StringIO(text)):
        if not record.get("Posting Date"):
            continue
        extended = (record.get("Extended Description") or "").strip()
        description = (record.get("Description") or "").strip()
        balance = (record.get("Balance") or "").strip()
        fitid = (record.get("Reference Number") or "").strip() or (
            record.get("Transaction ID") or ""
        ).strip() or None
        rows.append(
            ParsedRow(
                posted_date=datetime.strptime(record["Posting Date"], "%m/%d/%Y").date(),
                amount_cents=to_cents(record["Amount"]),
                description_raw=extended or description,
                merchant=description,
                fitid=fitid,
                bank_category=(record.get("Transaction Category") or "").strip(),
                balance_after_cents=to_cents(balance) if balance else None,
                status=(
                    "posted" if record.get("Posting Status", "Posted") == "Posted" else "pending"
                ),
            )
        )
    return rows


def _existing_fitids(session: Session, account_id: int, fitids: list[str]) -> set[str]:
    if not fitids:
        return set()
    return set(
        session.scalars(
            select(Transaction.fitid).where(
                Transaction.account_id == account_id, Transaction.fitid.in_(fitids)
            )
        )
    )


def _existing_hash_counts(session: Session, account_id: int, hashes: list[str]) -> Counter[str]:
    if not hashes:
        return Counter()
    rows = session.execute(
        select(Transaction.dedup_hash, func.count())
        .where(Transaction.account_id == account_id, Transaction.dedup_hash.in_(hashes))
        .group_by(Transaction.dedup_hash)
    ).all()
    return Counter(dict(rows))


def apply_rows(
    session: Session, account: Account, rows: list[ParsedRow], import_id: int | None
) -> tuple[list[Transaction], int]:
    """Insert rows through dedup layers 2 and 3. Returns (inserted, duplicates)."""
    known_fitids = _existing_fitids(session, account.id, [r.fitid for r in rows if r.fitid])

    no_fitid = [r for r in rows if not r.fitid]
    hashed = [
        compute_dedup_hash(account.id, r.posted_date, r.amount_cents, r.description_raw)
        for r in no_fitid
    ]
    budget = _existing_hash_counts(session, account.id, hashed)  # already-present per hash
    seen_this_file: Counter[str] = Counter()

    inserted: list[Transaction] = []
    duplicates = 0
    for row in rows:
        if row.fitid:
            if row.fitid in known_fitids:
                duplicates += 1
                continue
            known_fitids.add(row.fitid)
            row_hash = compute_dedup_hash(
                account.id, row.posted_date, row.amount_cents, row.description_raw
            )
        else:
            row_hash = compute_dedup_hash(
                account.id, row.posted_date, row.amount_cents, row.description_raw
            )
            seen_this_file[row_hash] += 1
            if seen_this_file[row_hash] <= budget[row_hash]:
                duplicates += 1  # covered by an existing identical row
                continue
        txn = Transaction(
            account_id=account.id,
            posted_date=row.posted_date,
            amount_cents=row.amount_cents,
            description_raw=row.description_raw,
            merchant_norm=row.merchant,
            status=row.status,
            import_id=import_id,
            fitid=row.fitid,
            dedup_hash=row_hash,
            bank_category=row.bank_category,
            balance_after_cents=row.balance_after_cents,
        )
        session.add(txn)
        inserted.append(txn)
    return inserted, duplicates


def find_merge_candidates(
    session: Session, account: Account, inserted: list[Transaction]
) -> list[MergeCandidate]:
    """Imported rows that plausibly duplicate an existing manual entry."""
    manual = session.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id, Transaction.import_id.is_(None)
        )
    ).all()
    window = timedelta(days=FUZZY_MATCH_WINDOW_DAYS)
    candidates = []
    for txn in inserted:
        for entry in manual:
            if (
                entry.amount_cents == txn.amount_cents
                and abs(entry.posted_date - txn.posted_date) <= window
            ):
                candidates.append(MergeCandidate(imported=txn, manual=entry))
    return candidates


def import_file(session: Session, path: Path, account: Account) -> ImportResult:
    content = path.read_bytes()
    file_sha = hashlib.sha256(content).hexdigest()
    already = session.scalars(
        select(ImportBatch).where(ImportBatch.file_sha256 == file_sha)
    ).first()
    if already is not None:
        raise ImportError_(
            f"this exact file was already imported on {already.imported_at:%Y-%m-%d} "
            f"as batch #{already.id} ({already.filename})"
        )

    text = content.decode("utf-8-sig")
    detect_format(text)
    rows = parse_scu_csv(text)
    if not rows:
        raise ImportError_("no transactions found in file")

    batch = ImportBatch(filename=path.name, file_sha256=file_sha, account_id=account.id)
    session.add(batch)
    session.flush()

    inserted, duplicates = apply_rows(session, account, rows, batch.id)
    batch.row_count = len(rows)
    batch.new_count = len(inserted)
    batch.duplicate_count = duplicates

    candidates = find_merge_candidates(session, account, inserted)

    snapshot = None
    newest = max(rows, key=lambda r: r.posted_date)
    if newest.balance_after_cents is not None:
        snapshot = BalanceSnapshot(
            account_id=account.id,
            as_of=newest.posted_date,
            balance_cents=newest.balance_after_cents,
            source="import",
        )
        session.add(snapshot)

    session.commit()
    return ImportResult(
        batch=batch,
        new_count=len(inserted),
        duplicate_count=duplicates,
        merge_candidates=candidates,
        snapshot=snapshot,
    )
