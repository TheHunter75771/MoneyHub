# Known gaps & deferred work

Living list. Every item here is a conscious deferral, not an oversight —
ordered roughly by how much money/truth each one protects.

## Data coverage

1. **Atlas has no data source.** No aggregator covers it (verified against
   SimpleFIN's institution list); no export has been tested. Card-side
   spending is invisible except as SmartPay pulls on SCU Spending. Options:
   test an in-app CSV/statement export, or keep manual entry. Until then the
   Atlas SmartPay legs on the Transfer-pairs screen stay unmatched, and the
   "Atlas card (allotment)" category double-serves as both spend and payment.
2. **NFCU + Acorns aren't synced.** Balances are seed-era ($0/$0, $0.16,
   $402.06 stale since July). NFCU could be added to the SimpleFIN
   connection; Acorns likely unsupported → manual snapshots. Decide per
   account, or mark them so the dashboard stops implying freshness.
3. **SCU Apartment Savings has no starting balance.** One entry in the
   Data & Rules "record stated balance" form fixes it.
4. **August statements** (~Aug 1) should be imported when SCU posts them —
   they audit the sync era for the first time. Make this a monthly habit or
   automate a reminder.
5. **OFX parser** still pending a sample file from a second institution.

## Engine quality

6. **Transfer pairing is greedy amount+date matching.** With repeated equal
   amounts on the same day it can cross-wire partners (seen live: a Bills
   "Apple Cash" −$100 paired to a Spending "+$100 Transfer from Emergency
   Fund"). Net math is unaffected (transfers are excluded from spending
   either way) but the display lies about who paid whom. Fix: prefer
   description symmetry ("Transfer to X" ↔ "Transfer from Y" with X/Y
   matching account nicknames) before falling back to amount+date; consider
   optimal bipartite matching instead of greedy.
7. **Split overdraft sweeps don't pair.** SCU sometimes covers one overdraft
   from two sources (−$0.09 + −$0.15 → +$0.24). Pairing is strictly 1:1;
   many-to-one grouping is unhandled (three orphan legs from April).
8. **Apple Cash rows are classified as transfers** on SCU Bills, but the
   other side (Apple wallet) is external. Decide: own category ("Apple
   Cash"), or treat as spending. Currently they distort nothing but sit as
   unmatched legs.
9. **Bill auto-check** (`bills.match_pattern`) is schema-only. Wiring:
   when an import/sync lands a txn matching an unpaid bill's pattern +
   amount, create the BillPayment and surface the "matched to txn" hint the
   design reserved space for.
10. **Inbox has no undo** (the design handoff explicitly wished for one).
    Rule-assign and merge-approve are one-way today (recoverable via
    Data & Rules, but not one click).

## Plumbing

11. **Notion projection needs credentials from Noah**: internal integration
    token + shared parent page → `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID` in
    `.env`. Push state lives in `data/notion-state.json`; if the app grows
    more operational state, promote to a settings table. No scheduled push
    yet — manual button only (could hang off the sync timer).
12. **Sync failures aren't persisted.** A failed SimpleFIN pull shows once in
    the sidebar and is lost; the state endpoint only knows the last
    *successful* batch. Record failures (timestamp + reason) so "last tried /
    last succeeded" are distinct.
13. **No app auth** — by design, the Tailscale boundary is the boundary. If
    exposure ever widens (family use), add a bearer token before anything
    else.
14. **Payday deposit is typed in, not detected.** The screen could pre-fill
    from the latest TREAS 310 deposit and learn the per-check split
    (allotment modeling) instead of defaulting to $960.
15. **Mobile pass** — the Nocturne handoff is desktop-only (≥1180px); Noah
    has a separate mobile design ("Money Hub Mobile.dc.html") not yet
    implemented.

20. **ntfy rides on ntfy.sh with topic-as-secret.** Fine for now (long random
    topic, modest financial detail), but the digest contains balances —
    self-hosting ntfy on the Tailscale box removes the exposure entirely.
    Also: no quiet hours and no dedupe — an unchanged digest still pushes
    daily. Bill paid-state backfill (see #9) would stop month-tail digests
    from calling already-paid bills "unpaid".

## Housekeeping

16. **~56 uncategorized long-tail rows** await Noah's category decisions —
    School (Cengage, Solvely) and Business/Tools (Squarespace, Google
    Cloud) categories would close most of it.
17. **money-hub.html retirement**: parity reached 2026-07-31 and skills are
    repointed; archive the file (move under `Finances/archive/`) once Noah
    confirms he no longer opens it.
18. **budget-tracker/ (Teller v1)** is superseded reference code — decide
    keep-as-portfolio vs archive.
19. **CI runs on push but the repo has no remote yet** — create the GitHub
    repo (public, portfolio) and push; verify Actions go green.
