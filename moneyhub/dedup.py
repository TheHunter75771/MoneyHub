"""Row-identity hashing for deduplication.

Layered dedup strategy (Phase 1 implements the full pipeline):
1. File level: reject a file whose sha256 was already imported (ImportBatch).
2. FITID (OFX): exact match on (account_id, fitid) — authoritative.
3. Row hash (CSV): the hash computed here. Overlapping date-range exports
   dedup cleanly; legitimate same-day identical purchases are handled by
   count-matching within the incoming file.
4. Fuzzy: pending->posted updates and manual-vs-import merges go to a review
   queue, never silently guessed.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

_WHITESPACE = re.compile(r"\s+")


def normalize_description(raw: str) -> str:
    """Canonical form of a bank description for hashing/matching.

    Uppercases and collapses whitespace only. Merchant-name normalization
    (stripping POS prefixes, store numbers) is the rules engine's job and is
    deliberately NOT part of row identity — it can evolve without changing
    hashes.
    """
    return _WHITESPACE.sub(" ", raw.strip()).upper()


def compute_dedup_hash(
    account_id: int, posted_date: date, amount_cents: int, description_raw: str
) -> str:
    """Stable identity for a transaction row within an account."""
    payload = "|".join(
        [
            str(account_id),
            posted_date.isoformat(),
            str(amount_cents),
            normalize_description(description_raw),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
