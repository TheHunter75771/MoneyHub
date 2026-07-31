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
from datetime import UTC, date, datetime, timedelta
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


def filter_cross_source_duplicates(
    session: Session,
    account: Account,
    rows: list[ParsedRow],
    exclude_fitid_prefix: str | None = None,
    include_fitidless: bool = False,
) -> tuple[list[ParsedRow], int]:
    """Drop rows already present from another source (e.g. CSV vs statement).

    A row is considered already-imported when an existing imported
    transaction on the same account has the same amount and a posted date
    within ±1 day (posting vs effective date skew). Each existing transaction
    can absorb at most one incoming row. exclude_fitid_prefix restricts the
    match pool to rows from OTHER id systems (a source's own re-imports are
    handled exactly by the FITID layer instead); include_fitidless extends
    the pool to imported rows without any bank id (statement rows), which
    matters for sources whose window can overlap statement history.
    """
    amounts = {r.amount_cents for r in rows}
    if not amounts:
        return rows, 0
    stmt = select(Transaction).where(
        Transaction.account_id == account.id,
        Transaction.amount_cents.in_(amounts),
    )
    if include_fitidless:
        # Any imported row counts (a bank id implies imported); manual rows
        # stay with the merge-candidate flow.
        stmt = stmt.where(
            (Transaction.import_id.is_not(None)) | (Transaction.fitid.is_not(None))
        )
        if exclude_fitid_prefix:
            stmt = stmt.where(
                (Transaction.fitid.is_(None))
                | (Transaction.fitid.not_like(f"{exclude_fitid_prefix}%"))
            )
    else:
        stmt = stmt.where(Transaction.fitid.is_not(None))
        if exclude_fitid_prefix:
            stmt = stmt.where(Transaction.fitid.not_like(f"{exclude_fitid_prefix}%"))
    existing = session.scalars(stmt).all()
    unclaimed: dict[int, list[Transaction]] = {}
    for txn in existing:
        unclaimed.setdefault(txn.amount_cents, []).append(txn)

    kept: list[ParsedRow] = []
    skipped = 0
    for row in rows:
        pool = unclaimed.get(row.amount_cents, [])
        match = next(
            (t for t in pool if abs((t.posted_date - row.posted_date).days) <= 1), None
        )
        if match is not None:
            pool.remove(match)
            skipped += 1
        else:
            kept.append(row)
    return kept, skipped


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


@dataclass
class SectionResult:
    account_name: str
    section_name: str
    new_count: int
    duplicate_count: int
    cross_source_count: int
    merge_candidates: list[MergeCandidate]
    snapshots_added: int
    skipped_unmapped: bool = False


def _add_snapshot_if_new(
    session: Session, account_id: int, as_of, balance_cents: int
) -> bool:
    exists = session.scalars(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account_id,
            BalanceSnapshot.as_of == as_of,
            BalanceSnapshot.source == "import",
            BalanceSnapshot.balance_cents == balance_cents,
        )
    ).first()
    if exists is not None:
        return False
    session.add(
        BalanceSnapshot(
            account_id=account_id, as_of=as_of, balance_cents=balance_cents, source="import"
        )
    )
    return True


def import_statement_file(
    session: Session, path: Path, share_map: dict[str, str] | None = None
) -> list[SectionResult]:
    """Import a Service CU member statement PDF: every mapped share section."""
    from .statements import (
        DEFAULT_SHARE_MAP,
        extract_pdf_pages,
        parse_statement_pages,
        statement_snapshot_dates,
    )

    share_map = DEFAULT_SHARE_MAP if share_map is None else share_map

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

    statement = parse_statement_pages(extract_pdf_pages(path))
    batch = ImportBatch(filename=path.name, file_sha256=file_sha, account_id=None)
    session.add(batch)
    session.flush()

    results: list[SectionResult] = []
    total_rows = total_new = total_dup = 0
    for suffix, section in statement.sections.items():
        account_name = share_map.get(suffix)
        if account_name is None:
            results.append(
                SectionResult(
                    account_name=f"(unmapped share ...{suffix})",
                    section_name=section.name,
                    new_count=0,
                    duplicate_count=0,
                    cross_source_count=0,
                    merge_candidates=[],
                    snapshots_added=0,
                    skipped_unmapped=True,
                )
            )
            continue
        account = session.scalars(
            select(Account).where(Account.name == account_name)
        ).one()

        rows = [
            ParsedRow(
                posted_date=r.eff_date,
                amount_cents=r.amount_cents,
                description_raw=r.description,
                balance_after_cents=r.balance_after_cents,
            )
            for r in section.rows
        ]
        total_rows += len(rows)
        rows, cross_source = filter_cross_source_duplicates(session, account, rows)
        inserted, duplicates = apply_rows(session, account, rows, batch.id)
        candidates = find_merge_candidates(session, account, inserted)
        snaps = sum(
            _add_snapshot_if_new(session, account.id, as_of, balance)
            for as_of, balance in statement_snapshot_dates(section)
        )
        total_new += len(inserted)
        total_dup += duplicates + cross_source
        results.append(
            SectionResult(
                account_name=account_name,
                section_name=section.name,
                new_count=len(inserted),
                duplicate_count=duplicates,
                cross_source_count=cross_source,
                merge_candidates=candidates,
                snapshots_added=snaps,
            )
        )

    batch.row_count = total_rows
    batch.new_count = total_new
    batch.duplicate_count = total_dup
    session.commit()
    return results


def sync_simplefin(session: Session, days: int = 45) -> tuple[list[SectionResult], list[dict]]:
    """Pull from SimpleFIN and run everything through the normal pipeline.

    Returns (results, unmapped) where unmapped lists SimpleFIN accounts that
    have no Money Hub account assigned yet.
    """
    import json
    from datetime import date as date_

    from .simplefin import (
        SFIN_FITID_PREFIX,
        SimpleFinError,
        auto_map_account,
        claim_setup_token,
        fetch_accounts,
        load_env,
        persist_access_url,
        rows_from_account,
    )

    env = load_env()
    access_url = env.get("SIMPLEFIN_ACCESS_URL")
    if not access_url:
        setup_token = env.get("SIMPLEFIN_SETUP_TOKEN")
        if not setup_token:
            raise SimpleFinError(
                "no SIMPLEFIN_ACCESS_URL or SIMPLEFIN_SETUP_TOKEN in .env — "
                "sign up at bridge.simplefin.org and put the setup token in .env"
            )
        access_url = claim_setup_token(setup_token)
        persist_access_url(access_url)

    body = fetch_accounts(access_url, days=days)
    payload = json.loads(body)
    for error in payload.get("errors") or []:
        print(f"  bridge warning: {error}")

    body_sha = hashlib.sha256(body).hexdigest()
    if session.scalars(select(ImportBatch).where(ImportBatch.file_sha256 == body_sha)).first():
        return [], []  # identical payload already processed — nothing changed

    batch = ImportBatch(
        filename=f"simplefin sync {date_.today().isoformat()}",
        file_sha256=body_sha,
        account_id=None,
    )
    session.add(batch)
    session.flush()

    results: list[SectionResult] = []
    unmapped: list[dict] = []
    total_rows = total_new = total_dup = 0
    for raw in payload.get("accounts", []):
        account = session.scalars(
            select(Account).where(Account.simplefin_id == raw["id"])
        ).first() or auto_map_account(session, raw)
        if account is None:
            org = (raw.get("org") or {}).get("name", "?")
            unmapped.append({"id": raw["id"], "name": raw.get("name", "?"), "org": org})
            continue

        rows = rows_from_account(raw)
        total_rows += len(rows)
        rows, cross_source = filter_cross_source_duplicates(
            session,
            account,
            rows,
            exclude_fitid_prefix=SFIN_FITID_PREFIX,
            include_fitidless=True,
        )
        inserted, duplicates = apply_rows(session, account, rows, batch.id)
        candidates = find_merge_candidates(session, account, inserted)

        snaps = 0
        if raw.get("balance") is not None and raw.get("balance-date"):
            from datetime import datetime

            from .money import to_cents

            as_of = datetime.fromtimestamp(raw["balance-date"], tz=UTC).date()
            snaps = int(
                _add_snapshot_if_new(session, account.id, as_of, to_cents(raw["balance"]))
            )
        total_new += len(inserted)
        total_dup += duplicates + cross_source
        results.append(
            SectionResult(
                account_name=account.name,
                section_name=raw.get("name", ""),
                new_count=len(inserted),
                duplicate_count=duplicates,
                cross_source_count=cross_source,
                merge_candidates=candidates,
                snapshots_added=snaps,
            )
        )

    batch.row_count = total_rows
    batch.new_count = total_new
    batch.duplicate_count = total_dup
    session.commit()
    return results, unmapped
