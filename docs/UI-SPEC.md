# Money Hub — UI Functional Spec (Phase 3 design handoff)

Owner designs all visuals, layout, naming, and navigation. This document is
the contract: every screen's required data and interactables, mapped to
backend capability that already exists. Tabs may be merged, split, or
renamed freely — coverage is what matters. Primary consumption is **phone
over Tailscale**, so mobile-first; desktop is secondary.

Global (visible from anywhere)
- Last-sync indicator: time + success/failure of the most recent SimpleFIN sync.
- "Sync now" action.
- Inbox badge: count of uncategorized transactions.

## 1. Dashboard
Shows:
- Card per account: name, latest balance, as-of date, reconcile state
  (OK / drift $X / stale).
- Total cash (non-credit accounts).
- Current month at a glance: income, spending, net, % of budget consumed.
- Recent transactions (last ~10, read-only).
Controls:
- Sync now.
- Tap account -> Transactions filtered to it.
- Tap month summary -> Budget tab.

## 2. Budget
Shows:
- Month being viewed; per-category rows: name, group (need/want/debt/...),
  spent, cap, remaining, over-budget flag.
- Totals: income, spending, transfer volume (excluded), uncategorized amount.
Controls:
- Month prev/next.
- Edit a category's monthly cap.
- Add / rename / retire a category.
- Tap category -> Transactions filtered to it.

## 3. Transactions
Shows:
- Paged table: date, merchant/description, account, amount, category chip,
  source badge (sync / csv / statement / manual), notes.
Controls:
- Filters: account, date range, category (incl. "uncategorized"), text search.
- Tap category chip -> change category (manual = sticky), with inline
  "create rule from this" shortcut.
- Edit notes.
- Add manual transaction: account, signed amount, date, description,
  optional category (for cash/Atlas-side spending).

## 4. Inbox (review queue)
Shows:
- Uncategorized transactions grouped by merchant: key, count, net total,
  sample raw description; biggest money first.
- Merge candidates: imported-vs-manual lookalike pairs awaiting a ruling.
Controls:
- Per group: assign category once (creates rule, prefilled match text +
  priority) OR categorize just these rows manually.
- Per merge candidate: approve (remove manual duplicate) or dismiss (keep both).
- Empty state when clean.

## 5. Bills
Shows:
- This period's checklist: name, amount, due rule, category, paid state,
  which check it belongs to (1st / 15th) where known.
- Totals: paid, remaining this period.
Controls:
- Check/uncheck paid (manual for now; auto-check on matched import is a
  planned wire-up — leave room for a "matched to txn" hint).
- Add / edit / deactivate a bill.
- Period navigation.

## 6. Debts
Shows:
- Debt rows: name, balance, note, status (open / paid off); total owed.
Controls:
- Update a balance.
- Add / edit a debt; mark paid off.

## 7. Data & Rules (admin)
Shows:
- Accounts: name, institution, kind, SimpleFIN mapping state.
- Import history: batches (file/sync, when, rows, new, dup).
- Rules list in priority order: conditions -> category, hit counts, active flag.
- Reconcile report per account: prior snapshot + txns = computed vs stated,
  drift.
Controls:
- Upload a file (CSV / statement PDF) -> import results panel (new, dup,
  cross-source, snapshots, merge candidates).
- Map an unmapped SimpleFIN account to a local account.
- Add / edit / disable / delete a rule; "run categorize now".
- Record a manual stated balance for an account (creates snapshot).
- Add / edit / deactivate an account.

## Reserved for Phase 4 (design space optional now)
- Payday flow: per-check bill assignment, envelope funding plan, courtesy-fee
  early-warning.
- Transfer-pair explorer (Atlas autopay linking, envelope-to-envelope audit).
- Notion projection status.

## API note
Every item above maps to existing backend modules (importing, rules,
reports, reconcile, simplefin). The FastAPI layer will be built to match the
approved design 1:1; anything the design adds that needs new data is a
conversation, not a blocker.
