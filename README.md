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
- [x] **Phase 2** — category rules engine (first-match-wins, manual-sticky),
      uncategorized inbox, monthly budget report, manual quick-add
- [x] **Live sync** — SimpleFIN Bridge connector (`moneyhub sync`) feeding the
      same dedup pipeline; daily systemd user timer (`deploy/`). Sync is an
      accelerator — file imports remain the audit trail. Atlas is not covered
      by any aggregator and stays on the manual/PDF path.
- [x] **Phase 3** — FastAPI + the owner-designed "Nocturne" web UI (see
      `docs/UI-SPEC.md` and the design handoff): Dashboard, Budget,
      Transactions, Inbox, Bills (period checklist), Debts, and the
      Data & Rules admin surface (upload, accounts, rules manager,
      reconcile, import history) all wired to live data. Vanilla ES
      modules, no build step, served by FastAPI (`moneyhub-web`, port 8710,
      Tailscale only — the app has no auth by design).
- [x] **Phase 4** — transfer-pair linking (176 pairs on live data; unmatched
      legs surface accounts we can't see into), the Payday screen
      (per-check bills, deposit math, courtesy-fee early warning), and the
      one-way Notion projection (dated snapshot pages; needs NOTION_TOKEN).
      Known deferrals live in `docs/GAPS.md`.
- [ ] **Phase 5** — Notion inbox (phone-side writes), OFX, Atlas data source,
      mobile UI pass — see `docs/GAPS.md`

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

### Deploying for real

`deploy/install.sh` puts the app on an always-on Linux box as a system
service bound to the host's Tailscale IP, with a daily sync timer and a
nightly WAL-safe snapshot. One command, idempotent, re-run to upgrade:

```bash
curl -fsSL https://raw.githubusercontent.com/TheHunter75771/MoneyHub/main/deploy/install.sh | sudo bash
```

Full runbook, secrets handling, and restore steps: `docs/DEPLOY.md`.

### Live sync (optional)

Create an account at [SimpleFIN Bridge](https://bridge.simplefin.org), connect
your institutions there (bank credentials go to the bridge, never to this
app), and put the setup token in `.env` as `SIMPLEFIN_SETUP_TOKEN`. The first
`moneyhub sync` claims the token and stores the access URL back into `.env`.

### Phone notifications (optional)

Uses [ntfy](https://ntfy.sh): install the ntfy app, subscribe to the topic in
your `.env` (`NTFY_TOPIC` — long and random; the topic name is the secret).
`moneyhub notify` pushes a digest (balances, unpaid-bill risk, payday nudge,
inbox count); the sync service pushes it automatically after every run and
fires a high-priority alert if the sync itself fails.

### Web UI

```bash
moneyhub-web            # http://127.0.0.1:8710
```

The app itself has no auth — the Tailscale boundary is the boundary. Never
expose the port publicly. `deploy/install.sh` binds it to the Tailscale IP.

### Backups

```bash
moneyhub snapshot --dest backups --keep 14
```

The DB is WAL mode: a plain file copy silently drops uncommitted-to-main-file
pages. `snapshot` uses SQLite's online backup API and integrity-checks the
result. The deploy's nightly timer runs exactly this.

## Testing philosophy

Tests concentrate on the logic where an error costs real money: cents
conversion, dedup, rules, and the seed/reconcile math. Table-driven pytest,
run in CI on every push.
