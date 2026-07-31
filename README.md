# Money Hub

Self-hosted personal finance hub: file-based transaction ingestion (CSV/OFX)
with deduplication, a category rules engine, envelope budgeting, and
balance reconciliation. Single user, SQLite, reachable only over a private
Tailscale network.

## Why this exists

The previous system was a single HTML file backed by browser localStorage,
with every balance and purchase typed in by hand. Reconciling two envelope
accounts manually took 20 minutes and surfaced ~$72 of unrecorded spending, a
forgotten $5 subscription, and a $100/month budgeting error caused by a
credit card that auto-pays its balance out of a checking envelope (spending
counted twice: once at the card, once at the autopay).

Money Hub replaces that workflow with imported data, deterministic
categorization, and a drift report that makes the books prove themselves.

## Design decisions

| Decision | Rationale |
|---|---|
| **SQLite, integer cents** | Single user, single writer, trivial backup. Money is never a float. |
| **CSV/OFX import is the primary ingestion path** | Live bank aggregators don't reliably cover the institutions that matter here. File export always works. OFX preferred where available (`FITID` makes dedup exact). |
| **Manual entry is first-class** | Phone-logged purchases go through the same pipeline and later reconcile against imports instead of double-counting. |
| **One source of truth** | SQLite holds everything. Any cloud mirror (Notion) is a one-way projection, never authoritative — no split-brain. |
| **Card autopay is a transfer** | Linked via `transfer_group_id`, excluded from spending math. Encodes the fix for the $100/month error class. |
| **Tailscale only** | No public exposure, no tunnel, no open ports. |
| **Secrets & personal data never committed** | `.gitignore` was the first commit. Real balances live in gitignored `data/`; the repo ships a fake `examples/seed.example.json`. |

## Architecture

```
bank exports (CSV/OFX)      manual entry
        \                      /
         importer  ── dedup (file hash → FITID → row hash → fuzzy)
                │
        category rules engine (first-match-wins, manual always sticky)
                │
             SQLite  ←── Alembic migrations
                │
    ┌───────────┼─────────────┐
 reports    FastAPI (later)  Notion projection (later, one-way)
 (budget,   over Tailscale
 drift)
```

## Build phases

- [x] **Phase 0** — repo scaffold, schema + initial migration, seed from the
      legacy money-hub data, tests, CI
- [x] **Phase 1** — importer CLI with layered dedup + balance-drift reconcile
      report. Formats: Service CU transaction CSV and Service CU member
      statement PDFs (multi-account, self-verifying via the running-balance
      chain, cross-source dedup against CSV imports). OFX lands when a sample
      from a second institution is available.
- [ ] **Phase 2** — category rules engine, uncategorized inbox, budget report,
      manual quick-add
- [ ] **Phase 3** — FastAPI + web UI (owner-designed) over Tailscale
- [ ] **Phase 4** — transfer linking (card autopay model), payday flow
- [ ] **Phase 5** — Notion one-way projection + inbox; optional live bank
      connector

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Create and seed a local database:

```bash
mkdir -p data
cp examples/seed.example.json data/seed.local.json   # then edit with real data
alembic upgrade head
python scripts/seed_from_money_hub.py
```

The database and `data/seed.local.json` are gitignored.

## Testing philosophy

Tests concentrate on the logic where an error costs real money: cents
conversion, dedup, rules, and the seed/reconcile math. Table-driven pytest,
run in CI on every push.
