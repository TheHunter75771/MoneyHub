from __future__ import annotations

from datetime import date

from moneyhub.dedup import compute_dedup_hash, normalize_description


def test_normalize_collapses_whitespace_and_case():
    assert normalize_description("  Walmart   groceries \n") == "WALMART GROCERIES"


def test_hash_is_stable():
    a = compute_dedup_hash(1, date(2026, 7, 10), -2578, "Walmart groceries")
    b = compute_dedup_hash(1, date(2026, 7, 10), -2578, "WALMART  GROCERIES")
    assert a == b  # case/whitespace differences do not change identity


def test_hash_distinguishes_fields():
    base = compute_dedup_hash(1, date(2026, 7, 10), -2578, "Walmart")
    assert base != compute_dedup_hash(2, date(2026, 7, 10), -2578, "Walmart")  # account
    assert base != compute_dedup_hash(1, date(2026, 7, 11), -2578, "Walmart")  # date
    assert base != compute_dedup_hash(1, date(2026, 7, 10), -2577, "Walmart")  # amount
    assert base != compute_dedup_hash(1, date(2026, 7, 10), -2578, "Target")  # description


def test_hash_not_confused_by_delimiter_collisions():
    # Field-boundary safety: content containing the join character must not
    # collide with different field splits.
    a = compute_dedup_hash(1, date(2026, 7, 10), -100, "A|B")
    b = compute_dedup_hash(1, date(2026, 7, 10), -100, "A B")
    assert a != b
