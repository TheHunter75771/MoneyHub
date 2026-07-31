from __future__ import annotations

from datetime import date

import pytest

from moneyhub.models import Account, Category, Rule, Transaction
from moneyhub.rules import apply_rules, rule_matches, set_category_manual


@pytest.fixture
def world(session):
    account = Account(name="Spending", kind="envelope")
    other = Account(name="Bills", kind="envelope")
    dining = Category(name="Dining", grp="want")
    transfer = Category(name="Transfer", grp="transfer")
    session.add_all([account, other, dining, transfer])
    session.commit()
    return {"account": account, "other": other, "dining": dining, "transfer": transfer}


def _txn(session, account, desc, cents, bank_category="", merchant=""):
    txn = Transaction(
        account_id=account.id,
        posted_date=date(2026, 7, 1),
        amount_cents=cents,
        description_raw=desc,
        merchant_norm=merchant,
        bank_category=bank_category,
        dedup_hash=f"h-{desc}-{cents}",
    )
    session.add(txn)
    session.commit()
    return txn


def test_condition_matching(world, session):
    txn = _txn(session, world["account"], "ZAXBY'S #12101 SC", -1570, bank_category="Fast Food")

    def rule(**kw):
        return Rule(category_id=world["dining"].id, **kw)

    assert rule_matches(rule(match_text="zaxby"), txn)
    assert not rule_matches(rule(match_text="wendy"), txn)
    assert rule_matches(rule(bank_category="Fast Food"), txn)
    assert not rule_matches(rule(bank_category="Coffee"), txn)
    assert rule_matches(rule(account_id=world["account"].id), txn)
    assert not rule_matches(rule(account_id=world["other"].id), txn)
    assert rule_matches(rule(direction="out"), txn)
    assert not rule_matches(rule(direction="in"), txn)
    assert rule_matches(rule(match_text="ZAXBY", direction="out", bank_category="Fast Food"), txn)


def test_matches_merchant_norm_too(world, session):
    txn = _txn(session, world["account"], "raw text only", -100, merchant="Waffle House")
    assert rule_matches(Rule(category_id=1, match_text="waffle house"), txn)


def test_first_match_wins_by_priority(world, session):
    txn = _txn(session, world["account"], "Transfer from Bills zaxby", 500)
    session.add(Rule(priority=10, match_text="transfer from", category_id=world["transfer"].id))
    session.add(Rule(priority=50, match_text="zaxby", category_id=world["dining"].id))
    session.commit()

    stats = apply_rules(session)
    assert stats.categorized == 1
    assert txn.category_id == world["transfer"].id
    assert txn.category_source.startswith("rule:")


def test_manual_is_sticky(world, session):
    txn = _txn(session, world["account"], "ZAXBY'S", -1570)
    set_category_manual(session, txn, world["transfer"].id)  # deliberately "wrong"
    session.add(Rule(match_text="zaxby", category_id=world["dining"].id))
    session.commit()

    stats = apply_rules(session)
    assert stats.examined == 0  # manual rows are never even looked at
    assert txn.category_id == world["transfer"].id
    assert txn.category_source == "manual"


def test_rule_edits_recompute_and_deletion_releases(world, session):
    txn = _txn(session, world["account"], "ZAXBY'S", -1570)
    rule = Rule(match_text="zaxby", category_id=world["transfer"].id)  # wrong target
    session.add(rule)
    session.commit()
    apply_rules(session)
    assert txn.category_id == world["transfer"].id

    rule.category_id = world["dining"].id  # fix the rule
    session.commit()
    apply_rules(session)
    assert txn.category_id == world["dining"].id  # row followed the fix

    session.delete(rule)
    session.commit()
    stats = apply_rules(session)
    assert stats.cleared == 1
    assert txn.category_id is None  # released back to the inbox
    assert txn.category_source is None


def test_inactive_rules_do_not_fire(world, session):
    txn = _txn(session, world["account"], "ZAXBY'S", -1570)
    session.add(Rule(match_text="zaxby", category_id=world["dining"].id, active=False))
    session.commit()
    apply_rules(session)
    assert txn.category_id is None
