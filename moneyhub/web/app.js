/* Money Hub — Nocturne UI. Vanilla ES module, no build step. */

const $ = (sel, el = document) => el.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const money = (c) =>
  (c < 0 ? "-$" : "$") +
  (Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money0 = (c) =>
  (c < 0 ? "-$" : "$") + Math.round(Math.abs(c) / 100).toLocaleString("en-US");
const signedMoney = (c) => (c > 0 ? "+" : "") + money(c);

const MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const monthLabel = (ym) => {
  const [y, m] = ym.split("-").map(Number);
  return `${MONTH_NAMES[m - 1]} ${y}`;
};
const shortDate = (iso) => {
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTH_NAMES[m - 1].slice(0, 3)} ${d}`;
};
const relTime = (iso) => {
  if (!iso) return "—";
  const mins = Math.round((Date.now() - new Date(iso + (iso.endsWith("Z") ? "" : "Z")).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} d ago`;
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------------------------------------------------------- store */

const store = {
  state: null,        // /api/state
  accounts: [],
  categories: [],
  month: null,        // "YYYY-MM" being viewed
  txFilters: { account_id: null, uncategorized: false, q: "", date_from: "", date_to: "" },
  txLimit: 100,
  syncing: false,
  billPeriod: new Date().toISOString().slice(0, 7),
  editingBill: null,
};

const NAV = [
  { id: "dash", label: "Dashboard", title: "Dashboard" },
  { id: "budget", label: "Budget", title: "Budget" },
  { id: "tx", label: "Transactions", title: "Transactions" },
  { id: "inbox", label: "Inbox", title: "Inbox", badge: true },
  { id: "bills", label: "Bills", title: "Bills" },
  { id: "debts", label: "Debts", title: "Debts" },
  { id: "data", label: "Data & Rules", title: "Data & Rules" },
  { id: "payday", label: "Payday", title: "Payday flow" },
  { id: "pairs", label: "Transfer pairs", title: "Transfer pairs" },
  { id: "notion", label: "Notion", title: "Notion projection" },
];

const STUBS = {
  bills: { phase: "Phase 3", desc: "The period checklist, with paid state and the check each bill belongs to.", bullets: ["Name, amount, due rule, category, paid state, 1st / 15th check assignment", "Totals: paid this period, remaining this period", 'Check / uncheck paid — with room for a "matched to txn" hint once auto-check lands', "Add, edit, deactivate a bill", "Period prev / next"] },
  debts: { phase: "Phase 3", desc: "Balances and payoff status, kept by hand until a payoff feed exists.", bullets: ["Rows: name, balance, note, status (open / paid off)", "Total owed", "Update a balance", "Add / edit a debt; mark paid off"] },
  data: { phase: "Phase 3", desc: "The admin surface: where imports, mappings, rules and reconciliation are inspected.", bullets: ["Accounts: name, institution, kind, SimpleFIN mapping state; add / edit / deactivate", "Import history: batches (file or sync, when, rows, new, dup)", "Rules in priority order: conditions → category, hit counts, active flag; add / edit / disable / delete; run categorize now", "Reconcile report per account: prior snapshot + txns = computed vs stated, drift", "Upload CSV or statement PDF → results panel (new, dup, cross-source, snapshots, merge candidates)", "Record a manual stated balance (creates a snapshot)"] },
  payday: { phase: "Phase 4", desc: "Reserved. The deposit-day routine, once Phase 3 is settled.", bullets: ["Per-check bill assignment", "Envelope funding plan", "Courtesy-fee early warning"] },
  pairs: { phase: "Phase 4", desc: "Reserved. Explorer for transfers that should cancel out.", bullets: ["Atlas autopay linking", "Envelope-to-envelope audit", "Unmatched transfer leg detection"] },
  notion: { phase: "Phase 4", desc: "Reserved. Status of the projection into Notion.", bullets: ["Last push time and result", "Drift between hub and Notion mirror", "Manual re-push"] },
};

const route = () => (location.hash.replace(/^#\/?/, "") || "dash").split("?")[0];
const go = (screen) => { location.hash = `#/${screen}`; };

/* ------------------------------------------------------------- sidebar */

function renderNav() {
  const nav = $("#nav");
  nav.replaceChildren();
  const active = route();
  for (const item of NAV) {
    const btn = el("button", "nav-item" + (active === item.id ? " active" : ""));
    btn.append(el("span", null, item.label));
    if (item.badge && store.state?.inbox_count > 0) {
      btn.append(el("span", "nav-badge num", String(store.state.inbox_count)));
    }
    if (item.later) btn.append(el("span", "nav-p4", "P4"));
    btn.addEventListener("click", () => go(item.id));
    nav.append(btn);
  }
  $("#sync-at").textContent = store.syncing
    ? "syncing…"
    : `Synced ${relTime(store.state?.sync_last_at)}`;
  const stale = $("#stale-note");
  const n = store.state?.stale_accounts || 0;
  if (store.state?.sync_failing) {
    // A failing sync outranks staleness: the sidebar must never imply health.
    stale.hidden = false;
    stale.textContent =
      `sync failing since ${relTime(store.state.sync_last_attempt_at)} — ` +
      (store.state.sync_error || "unknown error").slice(0, 90);
  } else {
    stale.hidden = n === 0;
    stale.textContent = n === 1 ? "1 account stale" : `${n} accounts stale`;
  }
  const btn = $("#btn-sync");
  btn.textContent = store.syncing ? "Syncing…" : "Sync now";
  btn.disabled = store.syncing;
}

async function refreshState() {
  store.state = await api("/api/state");
  if (!store.month && store.state.months.length) {
    store.month = store.state.months[store.state.months.length - 1];
  }
  renderNav();
}

async function doSync() {
  if (store.syncing) return;
  store.syncing = true;
  renderNav();
  const stale = $("#stale-note");
  try {
    await api("/api/sync", { method: "POST" });
    store.syncing = false;
    await refreshState();
    $("#sync-at").textContent = "Synced just now";
    render();
  } catch (e) {
    store.syncing = false;
    renderNav();
    stale.hidden = false;
    stale.textContent = `sync failed: ${e.message}`;
  }
}

/* ------------------------------------------------------------ dashboard */

async function renderDash(content) {
  content.append(el("div", "skeleton"));
  const [accounts, budget, recent] = await Promise.all([
    api("/api/accounts"),
    api(`/api/budget?month=${store.month}`),
    api("/api/recent"),
  ]);
  store.accounts = accounts;
  content.replaceChildren();
  const stack = el("div", "stack");

  // Needs attention
  const alerts = [];
  for (const a of accounts) {
    if (a.state.startsWith("stale")) alerts.push(`${a.name} has not synced in ${a.state.slice(6)}${a.state.endsWith("d") ? "ays" : ""}.`.replace("days.", " days."));
    if (a.state.startsWith("drift")) alerts.push(`${a.name} is off by ${a.state.slice(6)} against its stated balance.`);
  }
  const over = budget.categories.filter((c) => c.over).length;
  if (over > 0) alerts.push(`${over} categories are over budget this month.`);
  if (store.state.inbox_count > 0) alerts.push(`${store.state.inbox_count} transactions are waiting in the inbox.`);
  if (alerts.length) {
    const panel = el("div", "card alerts");
    panel.append(el("div", "alerts-title", "Needs attention"));
    const grid = el("div", "alerts-grid");
    alerts.forEach((a) => grid.append(el("div", null, a)));
    panel.append(grid);
    stack.append(panel);
  }

  // Account cards
  const grid = el("div", "acct-grid");
  for (const a of accounts) {
    if (a.balance_cents === null) continue;
    const card = el("button", "card acct-card");
    card.append(el("div", "acct-name", a.name));
    card.append(el("div", "acct-bal num", money(a.balance_cents)));
    card.append(el("div", "acct-sub", `${a.institution || a.kind} · as of ${shortDate(a.as_of)}`));
    if (a.warn) card.append(el("div", "pill-warn", a.state));
    card.addEventListener("click", () => {
      store.txFilters = { account_id: a.id, uncategorized: false, q: "", date_from: "", date_to: "" };
      go("tx");
    });
    grid.append(card);
  }
  stack.append(grid);

  // Two columns
  const cols = el("div", "dash-cols");
  const monthCard = el("div", "card month-card");
  const head = el("div", "month-head");
  head.append(el("div", "month-label", monthLabel(store.month)));
  const openBudget = el("button", "btn-text", "open budget");
  openBudget.addEventListener("click", () => go("budget"));
  head.append(openBudget);
  monthCard.append(head);

  const pct = budget.cap_total_cents > 0 ? Math.min(100, Math.round((budget.spending_cents / budget.cap_total_cents) * 100)) : 0;
  const progress = el("div", "progress");
  const fill = el("div", "progress-fill");
  fill.style.width = pct + "%";
  progress.append(fill);
  monthCard.append(progress);

  const cash = accounts
    .filter((a) => a.kind !== "credit" && a.balance_cents > 0)
    .reduce((n, a) => n + a.balance_cents, 0);
  const figures = el("div", "figures num");
  const fig = (label, value, cls = "") => {
    const box = el("div");
    box.append(el("div", "fig-label", label));
    box.append(el("div", `fig-value ${cls}`, value));
    return box;
  };
  const net = budget.income_cents - budget.spending_cents;
  figures.append(fig("Income", money0(budget.income_cents)));
  figures.append(fig("Spending", money0(budget.spending_cents)));
  figures.append(fig("Net", (net >= 0 ? "+" : "") + money0(net), "net"));
  figures.append(fig("Cash", money0(cash)));
  monthCard.append(figures);

  const tops = el("div", "topcats");
  for (const c of budget.categories.filter((c) => c.spent_cents > 0).slice(0, 5)) {
    const row = el("button", "topcat");
    row.append(el("div", "name", c.name));
    row.append(el("div", "spent num", `${money0(c.spent_cents)} / ${c.cap_cents !== null ? money0(c.cap_cents) : "—"}`));
    row.addEventListener("click", () => {
      store.txFilters = { account_id: null, uncategorized: false, q: c.name, date_from: "", date_to: "" };
      go("tx");
    });
    tops.append(row);
  }
  monthCard.append(tops);
  cols.append(monthCard);

  const right = el("div");
  const rhead = el("div", "recent-head");
  rhead.append(el("div", "sec-label", "Recent"));
  const allTx = el("button", "btn-text", "all transactions");
  allTx.addEventListener("click", () => {
    store.txFilters = { account_id: null, uncategorized: false, q: "", date_from: "", date_to: "" };
    go("tx");
  });
  rhead.append(allTx);
  right.append(rhead);
  for (const t of recent) {
    const row = el("div", "recent-row");
    const left = el("div");
    left.style.minWidth = "0";
    left.append(el("div", "recent-desc", t.description));
    left.append(el("div", "recent-meta", `${shortDate(t.date)} · ${t.account} · ${t.category}`));
    row.append(left);
    row.append(el("div", "recent-amt num", signedMoney(t.amount_cents)));
    right.append(row);
  }
  cols.append(right);
  stack.append(cols);
  content.append(stack);

  $("#page-sub").textContent = `${monthLabel(store.month)} · ${pct}% of budget consumed`;
}

/* --------------------------------------------------------------- budget */

async function renderBudget(content) {
  content.append(el("div", "skeleton"));
  const budget = await api(`/api/budget?month=${store.month}`);
  content.replaceChildren();
  const stack = el("div", "stack");
  const months = store.state.months;
  const idx = months.indexOf(store.month);

  const bar = el("div", "budget-bar");
  const nav = el("div", "month-nav");
  const prev = el("button", "btn-square", "←");
  prev.disabled = idx <= 0;
  prev.addEventListener("click", () => { store.month = months[idx - 1]; render(); });
  const next = el("button", "btn-square", "→");
  next.disabled = idx >= months.length - 1;
  next.addEventListener("click", () => { store.month = months[idx + 1]; render(); });
  nav.append(prev, el("div", "label", monthLabel(store.month)), next);
  bar.append(nav);
  const addBtn = el("button", "btn-accent", "Add category");
  addBtn.addEventListener("click", () => openCategoryDialog());
  bar.append(addBtn);
  stack.append(bar);

  const totals = el("div", "totals num");
  const total = (label, value, cls = "") => {
    const card = el("div", "card total-card");
    card.append(el("div", "fig-label", label));
    card.append(el("div", `fig-value ${cls}`, value));
    return card;
  };
  totals.append(total("Income", money0(budget.income_cents)));
  totals.append(total("Spending", money0(budget.spending_cents)));
  totals.append(total("Transfer volume (excl.)", money0(budget.transfer_cents)));
  totals.append(total("Uncategorized", money(budget.uncategorized_cents), "uncat"));
  stack.append(totals);

  const table = el("div");
  const header = el("div", "cat-header");
  ["Category", "Group", "Spent", "Cap", "Remaining", ""].forEach((h, i) => {
    const cell = el("div", null, h);
    if (i >= 2 && i <= 4) cell.classList.add("cell-right");
    header.append(cell);
  });
  table.append(header);

  for (const c of budget.categories) {
    const row = el("div", "cat-row num");
    const name = el("button", "cat-name", c.name);
    name.addEventListener("click", () => {
      store.txFilters = { account_id: null, uncategorized: false, q: c.name, date_from: "", date_to: "" };
      go("tx");
    });
    row.append(name);
    row.append(el("div", "cat-grp", c.grp));
    row.append(el("div", "cat-spent", money0(c.spent_cents)));

    const capCell = el("div", "cell-right");
    const cap = el("input", "cap-input");
    cap.type = "number";
    cap.min = "0";
    cap.value = c.cap_cents !== null ? (c.cap_cents / 100).toFixed(2).replace(/\.00$/, "") : "";
    cap.addEventListener("change", async () => {
      const cents = Math.round((Number(cap.value) || 0) * 100);
      c.cap_cents = cents;
      c.remaining_cents = cents - c.spent_cents;
      c.over = c.spent_cents > cents;
      rem.textContent = money0(Math.abs(c.remaining_cents));
      flag.replaceChildren();
      if (c.over) flag.append(el("span", "pill-over", "over budget"));
      try {
        await api(`/api/categories/${c.id}`, { method: "PATCH", body: { cap_cents: cents } });
      } catch (e) {
        flag.replaceChildren(el("span", "err", e.message));
      }
    });
    capCell.append(cap);
    row.append(capCell);

    const rem = el("div", "cat-rem", c.remaining_cents !== null ? money0(Math.abs(c.remaining_cents)) : "—");
    row.append(rem);

    const flag = el("div", "cell-right");
    if (c.over) flag.append(el("span", "pill-over", "over budget"));
    const menu = el("span", "row-menu");
    const rename = el("button", "btn-rule", "rename");
    rename.addEventListener("click", async () => {
      const name2 = prompt("Rename category", c.name);
      if (name2 && name2 !== c.name) {
        await api(`/api/categories/${c.id}`, { method: "PATCH", body: { name: name2 } });
        render();
      }
    });
    const retire = el("button", "btn-rule", "retire");
    retire.addEventListener("click", async () => {
      if (confirm(`Retire "${c.name}"? Its history stays; it stops appearing here.`)) {
        await api(`/api/categories/${c.id}`, { method: "PATCH", body: { active: false } });
        render();
      }
    });
    menu.append(rename, retire);
    flag.append(menu);
    row.append(flag);
    table.append(row);
  }
  stack.append(table);
  content.append(stack);

  const over = budget.categories.filter((c) => c.over).length;
  $("#page-sub").textContent = `${over} categories over cap`;
}

/* ----------------------------------------------------------- transactions */

async function renderTx(content) {
  content.append(el("div", "skeleton"));
  const f = store.txFilters;
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  if (f.account_id) params.set("account_id", f.account_id);
  if (f.uncategorized) params.set("uncategorized", "true");
  if (f.date_from) params.set("date_from", f.date_from);
  if (f.date_to) params.set("date_to", f.date_to);
  params.set("limit", String(store.txLimit));
  const [data, accounts] = await Promise.all([
    api(`/api/transactions?${params}`),
    store.accounts.length ? Promise.resolve(store.accounts) : api("/api/accounts"),
  ]);
  store.accounts = accounts;
  content.replaceChildren();
  const stack = el("div", "stack");
  stack.style.gap = "14px";

  // Filter bar
  const bar = el("div", "filter-bar");
  const search = el("input", "search");
  search.placeholder = "Search description…";
  search.value = f.q;
  let debounce;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { f.q = search.value; store.txLimit = 100; render(); }, 350);
  });
  bar.append(search);

  const chip = (label, on, handler) => {
    const b = el("button", "chip" + (on ? " on" : ""), label);
    b.addEventListener("click", handler);
    return b;
  };
  bar.append(chip("All", !f.account_id && !f.uncategorized, () => {
    f.account_id = null; f.uncategorized = false; store.txLimit = 100; render();
  }));
  bar.append(chip("Uncategorized", f.uncategorized, () => {
    f.uncategorized = !f.uncategorized; f.account_id = null; store.txLimit = 100; render();
  }));
  for (const a of accounts.filter((a) => a.balance_cents !== null)) {
    bar.append(chip(a.name.replace(/^SCU /, ""), f.account_id === a.id, () => {
      f.account_id = f.account_id === a.id ? null : a.id;
      f.uncategorized = false;
      store.txLimit = 100;
      render();
    }));
  }
  const from = el("input", "date-chip");
  from.type = "date";
  from.value = f.date_from;
  from.title = "From date";
  from.addEventListener("change", () => { f.date_from = from.value; store.txLimit = 100; render(); });
  const to = el("input", "date-chip");
  to.type = "date";
  to.value = f.date_to;
  to.title = "To date";
  to.addEventListener("change", () => { f.date_to = to.value; store.txLimit = 100; render(); });
  bar.append(from, to);

  const addBtn = el("button", "btn-accent", "Add manual");
  addBtn.addEventListener("click", () => openManualDialog());
  bar.append(addBtn);
  stack.append(bar);

  // Table
  const table = el("div");
  const header = el("div", "tx-header");
  ["Date", "Description", "Account", "Amount", "Category", "Source", "Notes"].forEach((h, i) => {
    const cell = el("div", null, h);
    if (i === 3) cell.classList.add("cell-right");
    header.append(cell);
  });
  table.append(header);

  for (const t of data.rows) {
    const row = el("div", "tx-row num");
    row.append(el("div", "tx-date", shortDate(t.date)));
    const desc = el("div", "tx-desc", t.description);
    desc.title = t.raw;
    row.append(desc);
    row.append(el("div", "tx-acct", t.account.replace(/^SCU /, "")));
    row.append(el("div", "tx-amt", signedMoney(t.amount_cents)));

    const catCell = el("div", "tx-catcell");
    const select = el("select", "cat-select");
    select.append(new Option("uncategorized", ""));
    for (const c of store.categories.filter((c) => c.active)) {
      select.append(new Option(c.name, String(c.id)));
    }
    select.value = t.category_id ? String(t.category_id) : "";
    const srcCell = el("div", "tx-src", t.source);
    select.addEventListener("change", async () => {
      try {
        await api(`/api/transactions/${t.id}`, {
          method: "PATCH",
          body: { set_category: true, category_id: select.value ? Number(select.value) : null },
        });
        srcCell.textContent = "manual";
        refreshState();
      } catch (e) {
        srcCell.replaceChildren(el("span", "err", e.message));
      }
    });
    catCell.append(select);
    const ruleBtn = el("button", "btn-rule", "rule");
    ruleBtn.addEventListener("click", () => openRuleDialog(t.description));
    catCell.append(ruleBtn);
    row.append(catCell);
    row.append(srcCell);

    const note = el("input", "note-input");
    note.placeholder = "—";
    note.value = t.notes;
    note.addEventListener("blur", async () => {
      if (note.value === t.notes) return;
      try {
        await api(`/api/transactions/${t.id}`, { method: "PATCH", body: { notes: note.value } });
        t.notes = note.value;
      } catch { note.value = t.notes; }
    });
    row.append(note);
    table.append(row);
  }
  stack.append(table);

  const foot = el("div", "tx-foot");
  foot.append(el("span", null, `${data.total} transactions`));
  if (data.rows.length < data.total) {
    const more = el("button", "btn-text", "load more");
    more.addEventListener("click", () => { store.txLimit += 100; render(); });
    foot.append(more);
  }
  stack.append(foot);
  content.append(stack);

  $("#page-sub").textContent = `${data.rows.length} rows shown`;
}

/* ---------------------------------------------------------------- inbox */

async function renderInbox(content) {
  content.append(el("div", "skeleton"));
  const data = await api("/api/inbox");
  content.replaceChildren();

  const cols = el("div", "inbox-cols");
  const left = el("div", "inbox-col");
  left.append(el("div", "sec-label", `Uncategorized by merchant · ${data.total} rows`));
  for (const g of data.groups) {
    const card = el("div", "card group-card");
    const head = el("div", "group-head");
    head.append(el("div", "group-key", g.key));
    head.append(el("div", "group-total num", money(g.net_cents)));
    card.append(head);
    card.append(el("div", "group-count", `${g.count} transactions`));
    card.append(el("div", "group-sample", g.sample));
    const actions = el("div", "group-actions");
    const select = el("select", "assign-select");
    select.append(new Option("Assign category & create rule…", ""));
    for (const c of store.categories.filter((c) => c.active)) {
      select.append(new Option(c.name, String(c.id)));
    }
    const errLine = el("div", "err");
    const assign = async (createRule) => {
      if (!select.value) return;
      try {
        await api("/api/inbox/assign", {
          method: "POST",
          body: { txn_ids: g.txn_ids, key: g.key, category_id: Number(select.value), create_rule: createRule },
        });
        await refreshState();
        render();
      } catch (e) { errLine.textContent = e.message; }
    };
    select.addEventListener("change", () => assign(true));
    actions.append(select);
    const justThese = el("button", "btn-text", "these rows only");
    justThese.style.fontSize = "12px";
    justThese.addEventListener("click", () => {
      if (!select.value) { errLine.textContent = "pick a category first"; return; }
      assign(false);
    });
    actions.append(justThese);
    card.append(actions, errLine);
    left.append(card);
  }
  cols.append(left);

  const right = el("div", "inbox-col");
  right.append(el("div", "sec-label", "Merge candidates"));
  for (const m of data.merges) {
    const card = el("div", "card merge-card");
    const lines = el("div", "merge-lines num");
    const line = (t) => `${t.description} · ${money(Math.abs(t.amount_cents))} · ${shortDate(t.date)} · ${t.source}`;
    lines.append(document.createTextNode(line(m.manual)));
    lines.append(el("br"));
    lines.append(document.createTextNode(line(m.imported)));
    card.append(lines);
    const actions = el("div", "merge-actions");
    const resolve = async (action) => {
      await api("/api/merges/resolve", { method: "POST", body: { manual_id: m.manual_id, action } });
      await refreshState();
      render();
    };
    const approve = el("button", "btn-accent", "Approve");
    approve.addEventListener("click", () => resolve("approve"));
    const dismiss = el("button", "btn-plain", "Dismiss");
    dismiss.addEventListener("click", () => resolve("dismiss"));
    actions.append(approve, dismiss);
    card.append(actions);
    right.append(card);
  }
  if (!data.groups.length && !data.merges.length) {
    const empty = el("div", "card empty-card");
    empty.append(el("div", "empty-title", "Nothing to review"));
    empty.append(el("div", "empty-sub", "Every transaction has a category."));
    right.append(empty);
  }
  cols.append(right);
  content.append(cols);

  $("#page-sub").textContent = `${data.total} uncategorized · ${data.merges.length} merge candidates`;
}

/* ---------------------------------------------------------------- stubs */

function renderStub(content, id) {
  const stub = STUBS[id];
  const box = el("div", "stub");
  box.append(el("div", "stub-phase", `${stub.phase} · not yet designed`));
  box.append(el("div", "stub-desc", stub.desc));
  for (const b of stub.bullets) box.append(el("div", "stub-bullet", b));
  content.append(box);
  $("#page-sub").textContent = "Spec coverage";
}

/* ---------------------------------------------------------------- bills */

const periodShift = (ym, delta) => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};

async function renderBills(content) {
  content.append(el("div", "skeleton"));
  const data = await api(`/api/bills?period=${store.billPeriod}`);
  content.replaceChildren();
  const stack = el("div", "stack");

  const bar = el("div", "budget-bar");
  const nav = el("div", "month-nav");
  const prev = el("button", "btn-square", "←");
  prev.addEventListener("click", () => { store.billPeriod = periodShift(store.billPeriod, -1); render(); });
  const next = el("button", "btn-square", "→");
  next.addEventListener("click", () => { store.billPeriod = periodShift(store.billPeriod, 1); render(); });
  nav.append(prev, el("div", "label", monthLabel(store.billPeriod)), next);
  bar.append(nav);
  const addBtn = el("button", "btn-accent", "Add bill");
  addBtn.addEventListener("click", () => openBillDialog(null));
  bar.append(addBtn);
  stack.append(bar);

  const totals = el("div", "totals num");
  const total = (label, value) => {
    const card = el("div", "card total-card");
    card.append(el("div", "fig-label", label));
    card.append(el("div", "fig-value", value));
    return card;
  };
  totals.append(total("Paid this period", money(data.paid_cents)));
  totals.append(total("Remaining this period", money(data.remaining_cents)));
  stack.append(totals);

  const table = el("div");
  const header = el("div", "bill-header");
  ["", "Bill", "Due", "Check", "Category", "Amount", ""].forEach((h, i) => {
    const cell = el("div", null, h);
    if (i === 5) cell.classList.add("cell-right");
    header.append(cell);
  });
  table.append(header);

  for (const b of data.rows) {
    const row = el("div", "bill-row num" + (b.paid ? " paid" : ""));
    const check = el("input", "checkbox");
    check.type = "checkbox";
    check.checked = b.paid;
    check.addEventListener("change", async () => {
      try {
        await api(`/api/bills/${b.id}/paid`, {
          method: "POST",
          body: { period: store.billPeriod, paid: check.checked },
        });
        render();
      } catch (e) { check.checked = !check.checked; }
    });
    const checkCell = el("div");
    checkCell.append(check);
    row.append(checkCell);
    const nameCell = el("div");
    nameCell.append(el("div", "bill-name", b.name));
    if (b.matched) {
      nameCell.append(
        el("div", "bill-matched",
          `✓ matched: ${b.matched.description} · ${shortDate(b.matched.date)}`)
      );
    }
    row.append(nameCell);
    row.append(el("div", "bill-due", b.due_rule));
    row.append(el("div", "bill-check", b.check));
    row.append(el("div", "bill-cat", b.category || "—"));
    row.append(el("div", "bill-amt", money(b.amount_cents)));
    const actions = el("div", "cell-right");
    const menu = el("span", "row-menu");
    const edit = el("button", "btn-rule", "edit");
    edit.addEventListener("click", () => openBillDialog(b));
    const retire = el("button", "btn-rule", "deactivate");
    retire.addEventListener("click", async () => {
      if (confirm(`Deactivate "${b.name}"? It disappears from the checklist.`)) {
        await api(`/api/bills/${b.id}`, { method: "PATCH", body: { active: false } });
        render();
      }
    });
    menu.append(edit, retire);
    actions.append(menu);
    row.append(actions);
    table.append(row);
  }
  stack.append(table);
  content.append(stack);

  const paidCount = data.rows.filter((r) => r.paid).length;
  $("#page-sub").textContent =
    `${paidCount} of ${data.rows.length} paid · ${money(data.remaining_cents)} remaining`;
}

/* ---------------------------------------------------------------- debts */

async function renderDebts(content) {
  content.append(el("div", "skeleton"));
  const data = await api("/api/debts");
  content.replaceChildren();
  const stack = el("div", "stack");

  const bar = el("div", "budget-bar");
  const totalCard = el("div", "card total-card num");
  totalCard.style.minWidth = "220px";
  totalCard.append(el("div", "fig-label", "Total owed"));
  totalCard.append(el("div", "fig-value", money(data.total_cents)));
  bar.append(totalCard);
  const addBtn = el("button", "btn-accent", "Add debt");
  addBtn.addEventListener("click", () => $("#dlg-debt").showModal());
  bar.append(addBtn);
  stack.append(bar);

  const table = el("div");
  const header = el("div", "debt-header");
  ["Debt", "Note", "Status", "Balance", ""].forEach((h, i) => {
    const cell = el("div", null, h);
    if (i === 3) cell.classList.add("cell-right");
    header.append(cell);
  });
  table.append(header);

  for (const d of data.rows) {
    const row = el("div", "debt-row num" + (d.status === "paid_off" ? " closed" : ""));
    row.append(el("div", "debt-name", d.name));
    const note = el("input", "note-input");
    note.placeholder = "—";
    note.value = d.note;
    note.addEventListener("blur", async () => {
      if (note.value !== d.note) {
        await api(`/api/debts/${d.id}`, { method: "PATCH", body: { note: note.value } });
        d.note = note.value;
      }
    });
    row.append(note);
    row.append(
      d.status === "paid_off"
        ? (() => { const p = el("div"); p.append(el("span", "pill-ok", "paid off")); return p; })()
        : el("div", "debt-note", "open")
    );
    const balCell = el("div", "cell-right");
    const bal = el("input", "balance-input");
    bal.type = "number";
    bal.min = "0";
    bal.step = "0.01";
    bal.value = (d.balance_cents / 100).toFixed(2);
    bal.addEventListener("change", async () => {
      await api(`/api/debts/${d.id}`, {
        method: "PATCH",
        body: { balance_cents: Math.round((Number(bal.value) || 0) * 100) },
      });
      render();
    });
    balCell.append(bal);
    row.append(balCell);
    const actions = el("div", "cell-right");
    const flip = el("button", "btn-rule", d.status === "paid_off" ? "reopen" : "mark paid off");
    flip.addEventListener("click", async () => {
      await api(`/api/debts/${d.id}`, {
        method: "PATCH",
        body: { status: d.status === "paid_off" ? "open" : "paid_off" },
      });
      render();
    });
    actions.append(flip);
    row.append(actions);
    table.append(row);
  }
  stack.append(table);
  content.append(stack);

  const open = data.rows.filter((r) => r.status === "open").length;
  $("#page-sub").textContent = `${open} open · ${money(data.total_cents)} owed`;
}

/* ----------------------------------------------------------- data & rules */

async function renderData(content) {
  content.append(el("div", "skeleton"));
  const [accounts, rules, imports, recon, syncs] = await Promise.all([
    api("/api/accounts"),
    api("/api/rules"),
    api("/api/imports"),
    api("/api/reconcile"),
    api("/api/sync/history"),
  ]);
  store.accounts = accounts;
  content.replaceChildren();
  const stack = el("div", "stack");

  const section = (title, headBtns = []) => {
    const box = el("div", "data-section");
    const head = el("div", "data-head");
    head.append(el("div", "sec-label", title));
    if (headBtns.length) {
      const wrap = el("div");
      wrap.style.display = "flex";
      wrap.style.gap = "10px";
      headBtns.forEach((b) => wrap.append(b));
      head.append(wrap);
    }
    box.append(head);
    return box;
  };

  // Upload
  const up = section("Import a file");
  const card = el("div", "card upload-card");
  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.accept = ".csv,.pdf";
  const acctSel = el("select");
  acctSel.append(new Option("account (CSV only)…", ""));
  for (const a of accounts) acctSel.append(new Option(a.name, String(a.id)));
  const goBtn = el("button", "btn-accent", "Import");
  const result = el("div", "result-block");
  goBtn.addEventListener("click", async () => {
    const file = fileInput.files[0];
    if (!file) { result.textContent = "pick a file first"; result.className = "err result-block"; return; }
    const form = new FormData();
    form.append("file", file);
    if (acctSel.value) form.append("account_id", acctSel.value);
    goBtn.disabled = true;
    goBtn.textContent = "Importing…";
    try {
      const res = await fetch("/api/import", { method: "POST", body: form });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || res.statusText);
      result.className = "result-block";
      result.replaceChildren();
      for (const s of body.sections) {
        result.append(el("div", null,
          `${s.account} — new: ${s.new}, duplicates: ${s.duplicates}, cross-source: ${s.cross_source}, snapshots: +${s.snapshots}${s.skipped ? " (unmapped, skipped)" : ""}`));
      }
      await refreshState();
    } catch (e) {
      result.className = "err result-block";
      result.textContent = e.message;
    } finally {
      goBtn.disabled = false;
      goBtn.textContent = "Import";
    }
  });
  card.append(fileInput, acctSel, goBtn, result);
  up.append(card);
  stack.append(up);

  // Accounts
  const addAcct = el("button", "btn-accent", "Add account");
  addAcct.addEventListener("click", () => $("#dlg-account").showModal());
  const acctSec = section("Accounts", [addAcct]);
  const acctHead = el("div", "mini-head acct");
  ["Name", "Institution", "Kind", "SimpleFIN", ""].forEach((h) => acctHead.append(el("div", null, h)));
  acctSec.append(acctHead);
  for (const a of accounts) {
    const row = el("div", "mini-row acct");
    row.append(el("div", null, a.name));
    row.append(el("div", "mini-dim", a.institution || "—"));
    row.append(el("div", "mini-dim", a.kind));
    row.append(el("div", "mini-dim", a.simplefin_mapped ? "mapped ✓" : "—"));
    const actions = el("div");
    const rename = el("button", "btn-rule", "rename");
    rename.addEventListener("click", async () => {
      const name2 = prompt("Rename account", a.name);
      if (name2 && name2 !== a.name) {
        await api(`/api/accounts/${a.id}`, { method: "PATCH", body: { name: name2 } });
        render();
      }
    });
    const off = el("button", "btn-rule", "deactivate");
    off.addEventListener("click", async () => {
      if (confirm(`Deactivate "${a.name}"?`)) {
        await api(`/api/accounts/${a.id}`, { method: "PATCH", body: { is_active: false } });
        render();
      }
    });
    const menu = el("span", "row-menu");
    menu.append(rename, off);
    actions.append(menu);
    row.append(actions);
    acctSec.append(row);
  }
  stack.append(acctSec);

  // Rules
  const runBtn = el("button", "btn-plain", "Run categorize now");
  runBtn.addEventListener("click", async () => {
    const stats = await api("/api/categorize", { method: "POST" });
    runBtn.textContent = `categorized ${stats.categorized}, released ${stats.released}`;
    await refreshState();
    setTimeout(() => { runBtn.textContent = "Run categorize now"; }, 2500);
  });
  const addRule = el("button", "btn-accent", "Add rule");
  addRule.addEventListener("click", () => openRuleDialog(""));
  const ruleSec = section(`Rules · ${rules.length}`, [runBtn, addRule]);
  const ruleHead = el("div", "mini-head rule");
  ["Prio", "Conditions", "Category", "Hits", ""].forEach((h) => ruleHead.append(el("div", null, h)));
  ruleSec.append(ruleHead);
  for (const r of rules) {
    const row = el("div", "mini-row rule num");
    if (!r.active) row.style.opacity = "0.5";
    row.append(el("div", "mini-dim", String(r.priority)));
    row.append(el("div", null, r.conditions || "(everything)"));
    row.append(el("div", "mini-dim", r.category));
    row.append(el("div", "mini-dim", String(r.hits)));
    const actions = el("div");
    const menu = el("span", "row-menu");
    const toggle = el("button", "btn-rule", r.active ? "disable" : "enable");
    toggle.addEventListener("click", async () => {
      await api(`/api/rules/${r.id}`, { method: "PATCH", body: { active: !r.active } });
      await refreshState();
      render();
    });
    const del = el("button", "btn-rule", "delete");
    del.addEventListener("click", async () => {
      if (confirm("Delete this rule? Its rows go back to the inbox.")) {
        await api(`/api/rules/${r.id}`, { method: "DELETE" });
        await refreshState();
        render();
      }
    });
    menu.append(toggle, del);
    actions.append(menu);
    row.append(actions);
    ruleSec.append(row);
  }
  stack.append(ruleSec);

  // Reconcile
  const snapForm = el("div", "inline-form");
  const snapAcct = el("select");
  for (const a of accounts) snapAcct.append(new Option(a.name, String(a.id)));
  const snapAmt = el("input");
  snapAmt.type = "number";
  snapAmt.step = "0.01";
  snapAmt.placeholder = "stated balance";
  const snapDate = el("input");
  snapDate.type = "date";
  snapDate.value = new Date().toISOString().slice(0, 10);
  const snapBtn = el("button", "btn-plain", "Record stated balance");
  snapBtn.addEventListener("click", async () => {
    if (snapAmt.value === "") return;
    await api("/api/snapshots", {
      method: "POST",
      body: {
        account_id: Number(snapAcct.value),
        as_of: snapDate.value,
        balance_cents: Math.round(Number(snapAmt.value) * 100),
      },
    });
    render();
  });
  snapForm.append(snapAcct, snapAmt, snapDate, snapBtn);
  const recSec = section("Reconcile");
  const recHead = el("div", "mini-head rec");
  ["Account", "Prior", "Txns", "Computed", "Stated", "Drift"].forEach((h) => recHead.append(el("div", null, h)));
  recSec.append(recHead);
  for (const r of recon) {
    const row = el("div", "mini-row rec num");
    row.append(el("div", null, r.account));
    if (r.error) {
      const msg = el("div", "mini-dim", r.error);
      msg.style.gridColumn = "2 / -1";
      row.append(msg);
    } else {
      row.append(el("div", "mini-dim", `${money(r.prior_cents)} (${shortDate(r.prior_as_of)})`));
      row.append(el("div", "mini-dim", String(r.txn_count)));
      row.append(el("div", "mini-dim", money(r.computed_cents)));
      row.append(el("div", "mini-dim", `${money(r.stated_cents)} (${shortDate(r.stated_as_of)})`));
      const drift = el("div", null, r.drift_cents === 0 ? "0.00 ✓" : money(r.drift_cents) + " ✗");
      if (r.drift_cents !== 0) drift.style.color = "var(--accent-bright)";
      row.append(drift);
    }
    recSec.append(row);
  }
  recSec.append(snapForm);
  stack.append(recSec);

  // Sync history — successes AND failures, so silence never reads as health
  const syncSec = section("Sync history");
  const syncHead = el("div", "mini-head imp");
  ["Started", "Status", "Detail", "New", "", ""].forEach((h) => syncHead.append(el("div", null, h)));
  syncSec.append(syncHead);
  for (const s of syncs.slice(0, 10)) {
    const row = el("div", "mini-row imp num");
    row.append(el("div", "mini-dim", s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : "—"));
    const status = el("div", null, s.status);
    if (s.status === "failed") status.style.color = "var(--accent-bright)";
    row.append(status);
    const detail = el("div", "mini-dim", s.message || "—");
    detail.style.gridColumn = "3 / 6";
    row.append(detail);
    row.append(el("div", "mini-dim", String(s.new_count)));
    syncSec.append(row);
  }
  if (!syncs.length) {
    syncSec.append(el("div", "mini-row imp", "no sync attempts recorded yet"));
  }
  stack.append(syncSec);

  // Import history
  const impSec = section("Import history");
  const impHead = el("div", "mini-head imp");
  ["File / sync", "Account", "When", "Rows", "New", "Dup"].forEach((h) => impHead.append(el("div", null, h)));
  impSec.append(impHead);
  for (const b of imports) {
    const row = el("div", "mini-row imp num");
    row.append(el("div", null, b.filename));
    row.append(el("div", "mini-dim", b.account || "multi"));
    row.append(el("div", "mini-dim", b.imported_at ? b.imported_at.slice(0, 16).replace("T", " ") : "—"));
    row.append(el("div", "mini-dim", String(b.rows)));
    row.append(el("div", "mini-dim", String(b.new)));
    row.append(el("div", "mini-dim", String(b.duplicates)));
    impSec.append(row);
  }
  stack.append(impSec);

  content.append(stack);
  $("#page-sub").textContent = `${rules.length} rules · ${accounts.length} accounts`;
}

/* ---------------------------------------------------------------- payday */

async function renderPayday(content) {
  content.append(el("div", "skeleton"));
  const check = store.paydayCheck || "1st";
  const period = store.billPeriod;
  const data = await api(`/api/payday?period=${period}&check=${check}`);
  content.replaceChildren();
  const stack = el("div", "stack");

  const bar = el("div", "budget-bar");
  const nav = el("div", "month-nav");
  const prev = el("button", "btn-square", "←");
  prev.addEventListener("click", () => { store.billPeriod = periodShift(period, -1); render(); });
  const next = el("button", "btn-square", "→");
  next.addEventListener("click", () => { store.billPeriod = periodShift(period, 1); render(); });
  nav.append(prev, el("div", "label", monthLabel(period)), next);
  bar.append(nav);
  const chips = el("div", "filter-bar");
  for (const c of ["1st", "15th"]) {
    const chip = el("button", "chip" + (check === c ? " on" : ""), `${c} check`);
    chip.addEventListener("click", () => { store.paydayCheck = c; render(); });
    chips.append(chip);
  }
  bar.append(chips);
  stack.append(bar);

  // Courtesy-fee early warning
  const spending = data.accounts.find((a) => a.name === "SCU Spending");
  const alerts = [];
  if (data.fees_60d_count > 0) {
    alerts.push(
      `${data.fees_60d_count} courtesy-pay fee(s) — ${money(data.fees_60d_cents)} — in the last 60 days.`
    );
  }
  if (spending && spending.balance_cents !== null && spending.balance_cents < data.unpaid_cents) {
    alerts.push(
      `Spending holds ${money(spending.balance_cents)} against ${money(data.unpaid_cents)} of unpaid ${check}-check bills — fund bills first or a fee is coming.`
    );
  }
  if (alerts.length) {
    const panel = el("div", "card alerts");
    panel.append(el("div", "alerts-title", "Courtesy-fee watch"));
    const grid = el("div", "alerts-grid");
    alerts.forEach((a) => grid.append(el("div", null, a)));
    panel.append(grid);
    stack.append(panel);
  }

  // Deposit vs bills
  const figures = el("div", "totals num");
  const pc = data.paycheck || {};
  const depositCard = el("div", "card total-card");
  depositCard.append(el("div", "fig-label", "This deposit"));
  const depositInput = el("input", "balance-input");
  depositInput.type = "number";
  depositInput.step = "0.01";
  depositInput.style.width = "130px";
  depositInput.style.marginTop = "4px";
  const prefill =
    store.paydayDeposit ??
    (pc.detected_cents != null
      ? (pc.detected_cents / 100).toFixed(2)
      : pc.typical_cents != null
        ? (pc.typical_cents / 100).toFixed(2)
        : "");
  depositInput.value = prefill;
  depositCard.append(depositInput);
  if (pc.detected_cents != null) {
    const hint = el("div", "bill-matched");
    hint.textContent =
      `detected ${money(pc.detected_cents)} · ${shortDate(pc.detected_date)}` +
      ` (${pc.detected_days_ago}d ago)`;
    depositCard.append(hint);
  }
  if (pc.typical_cents != null) {
    const useTypical = el("button", "btn-rule", `use typical ${money(pc.typical_cents)}`);
    useTypical.style.marginTop = "4px";
    useTypical.addEventListener("click", () => {
      depositInput.value = (pc.typical_cents / 100).toFixed(2);
      depositInput.dispatchEvent(new Event("input"));
    });
    depositCard.append(useTypical);
  }
  figures.append(depositCard);

  const total = (label, value, cls = "") => {
    const card = el("div", "card total-card");
    card.append(el("div", "fig-label", label));
    card.append(el("div", `fig-value ${cls}`, value));
    return card;
  };
  figures.append(total(`Unpaid ${check}-check bills`, money(data.unpaid_cents)));
  const afterCard = total("Left after bills", "");
  const afterValue = afterCard.querySelector(".fig-value");
  const recompute = () => {
    const cents = Math.round((Number(depositInput.value) || 0) * 100);
    const left = cents - data.unpaid_cents;
    afterValue.textContent = money(left);
    afterValue.classList.toggle("uncat", left < 0);
    store.paydayDeposit = depositInput.value;
  };
  depositInput.addEventListener("input", recompute);
  figures.append(afterCard);
  figures.append(total("Income seen this month", money(data.income_detected_cents)));
  recompute();
  stack.append(figures);

  // Bills for this check (checkable, same behavior as Bills screen)
  const table = el("div");
  const header = el("div", "bill-header");
  ["", "Bill", "Due", "Check", "Category", "Amount", ""].forEach((h, i) => {
    const cell = el("div", null, h);
    if (i === 5) cell.classList.add("cell-right");
    header.append(cell);
  });
  table.append(header);
  for (const b of data.bills.concat(data.irregular_bills)) {
    const row = el("div", "bill-row num" + (b.paid ? " paid" : ""));
    const cb = el("input", "checkbox");
    cb.type = "checkbox";
    cb.checked = b.paid;
    cb.addEventListener("change", async () => {
      await api(`/api/bills/${b.id}/paid`, {
        method: "POST",
        body: { period, paid: cb.checked },
      });
      render();
    });
    const cbCell = el("div");
    cbCell.append(cb);
    row.append(cbCell);
    row.append(el("div", "bill-name", b.name));
    row.append(el("div", "bill-due", b.due_rule));
    row.append(el("div", "bill-check", b.check));
    row.append(el("div", "bill-cat", b.category || "—"));
    row.append(el("div", "bill-amt", money(b.amount_cents)));
    row.append(el("div"));
    table.append(row);
  }
  stack.append(table);

  // Envelope balances strip
  const strip = el("div", "acct-grid");
  for (const a of data.accounts.filter((a) => a.balance_cents !== null && a.kind === "envelope")) {
    const card = el("div", "card acct-card");
    card.append(el("div", "acct-name", a.name));
    card.append(el("div", "acct-bal num", money(a.balance_cents)));
    card.append(el("div", "acct-sub", `as of ${shortDate(a.as_of)}`));
    strip.append(card);
  }
  stack.append(el("div", "sec-label", "Envelopes right now"));
  stack.append(strip);

  content.append(stack);
  $("#page-sub").textContent =
    `${check} check · ${money(data.unpaid_cents)} in unpaid bills · Claude directs, Noah moves the money`;
}

/* ------------------------------------------------------------- transfers */

async function renderPairs(content) {
  content.append(el("div", "skeleton"));
  const data = await api("/api/transfers");
  content.replaceChildren();
  const stack = el("div", "stack");

  const bar = el("div", "budget-bar");
  bar.append(el("div", "sec-label", `${data.groups.length} linked pairs · ${data.unmatched.length} unmatched legs`));
  const pairBtn = el("button", "btn-accent", "Run pairing");
  pairBtn.addEventListener("click", async () => {
    pairBtn.disabled = true;
    pairBtn.textContent = "Pairing…";
    const res = await api("/api/transfers/pair", { method: "POST" });
    pairBtn.textContent = `+${res.groups_created} pairs`;
    setTimeout(render, 700);
  });
  bar.append(pairBtn);
  stack.append(bar);

  const cols = el("div", "inbox-cols");
  const left = el("div", "inbox-col");
  left.append(el("div", "sec-label", "Linked pairs"));
  for (const g of data.groups.slice(0, 40)) {
    const card = el("div", "card group-card");
    const head = el("div", "group-head");
    head.append(el("div", "group-key", `${g.from_account || "?"} → ${g.to_account || "?"}`));
    head.append(el("div", "group-total num", money(g.amount_cents)));
    card.append(head);
    card.append(el("div", "group-count", shortDate(g.date)));
    for (const leg of g.legs) {
      card.append(el("div", "group-sample", `${leg.account} · ${signedMoney(leg.amount_cents)} · ${leg.description}`));
    }
    left.append(card);
  }
  cols.append(left);

  const right = el("div", "inbox-col");
  right.append(el("div", "sec-label", "Unmatched legs — the other side is invisible"));
  if (!data.unmatched.length) {
    const empty = el("div", "card empty-card");
    empty.append(el("div", "empty-title", "Every transfer has its pair"));
    empty.append(el("div", "empty-sub", "Both legs of every movement are linked."));
    right.append(empty);
  }
  for (const t of data.unmatched) {
    const card = el("div", "card merge-card");
    const lines = el("div", "merge-lines num");
    lines.textContent = `${t.account} · ${shortDate(t.date)} · ${signedMoney(t.amount_cents)} · ${t.description}`;
    card.append(lines);
    right.append(card);
  }
  cols.append(right);
  stack.append(cols);
  content.append(stack);

  $("#page-sub").textContent = `${data.groups.length} pairs · ${data.unmatched.length} unmatched`;
}

/* ---------------------------------------------------------------- notion */

async function renderNotion(content) {
  content.append(el("div", "skeleton"));
  const status = await api("/api/notion/status");
  content.replaceChildren();
  const stack = el("div", "stack");
  stack.style.maxWidth = "720px";

  const card = el("div", "card month-card");
  card.append(el("div", "month-label", "One-way projection"));
  const desc = el("div", "stub-desc");
  desc.style.marginTop = "8px";
  desc.textContent =
    "Money Hub pushes a read-only snapshot (balances, budget, debts) as a dated page in Notion so phone workflows can read it. Notion is never a source of truth.";
  card.append(desc);
  stack.append(card);

  const state = el("div", "card month-card");
  if (!status.configured) {
    state.append(el("div", "stub-phase", "not configured"));
    const help = el("div", "stub-desc");
    help.style.marginTop = "8px";
    help.textContent =
      "Create an internal integration at notion.so/my-integrations, share a parent page with it, then put NOTION_TOKEN and NOTION_PARENT_PAGE_ID in money-hub/.env and restart the web service.";
    state.append(help);
  } else {
    state.append(el("div", "sec-label", "Status"));
    state.append(el("div", "stub-desc",
      `Last push: ${status.last_push_at ? relTime(status.last_push_at) : "never"} · ${status.last_result || "—"}`));
    if (status.last_page_url) {
      const link = el("a", "btn-text", "open last snapshot in Notion");
      link.href = status.last_page_url;
      link.target = "_blank";
      state.append(link);
    }
    const pushBtn = el("button", "btn-accent", "Push snapshot now");
    pushBtn.style.marginTop = "12px";
    const errLine = el("div", "err");
    pushBtn.addEventListener("click", async () => {
      pushBtn.disabled = true;
      pushBtn.textContent = "Pushing…";
      try {
        await api("/api/notion/push", { method: "POST" });
        render();
      } catch (e) {
        errLine.textContent = e.message;
        pushBtn.disabled = false;
        pushBtn.textContent = "Push snapshot now";
      }
    });
    state.append(pushBtn, errLine);
  }
  stack.append(state);
  content.append(stack);
  $("#page-sub").textContent = status.configured ? "configured" : "awaiting credentials";
}

/* --------------------------------------------------------------- dialogs */

function fillCategorySelect(select, includeEmpty = false) {
  select.replaceChildren();
  if (includeEmpty) select.append(new Option("uncategorized", ""));
  for (const c of store.categories.filter((c) => c.active)) {
    select.append(new Option(c.name, String(c.id)));
  }
}

function openRuleDialog(matchText) {
  const dlg = $("#dlg-rule");
  const form = $("#form-rule");
  form.match.value = (matchText || "").toLowerCase();
  form.priority.value = "50";
  $("#rule-err").textContent = "";
  fillCategorySelect($("#rule-cat"));
  dlg.showModal();
}

function openManualDialog() {
  const dlg = $("#dlg-manual");
  const form = $("#form-manual");
  form.reset();
  form.date.value = new Date().toISOString().slice(0, 10);
  $("#manual-err").textContent = "";
  const acct = $("#manual-acct");
  acct.replaceChildren();
  for (const a of store.accounts) acct.append(new Option(a.name, String(a.id)));
  fillCategorySelect($("#manual-cat"), true);
  dlg.showModal();
}

function openBillDialog(bill) {
  store.editingBill = bill;
  const form = $("#form-bill");
  form.reset();
  $("#bill-title").textContent = bill ? "Edit bill" : "Add bill";
  $("#bill-err").textContent = "";
  const sel = $("#bill-cat");
  sel.replaceChildren();
  sel.append(new Option("—", ""));
  for (const c of store.categories.filter((c) => c.active)) {
    sel.append(new Option(c.name, String(c.id)));
  }
  if (bill) {
    form.name.value = bill.name;
    form.amount.value = (bill.amount_cents / 100).toFixed(2);
    form.due.value = bill.due_rule;
    form.pattern.value = bill.match_pattern || "";
    const match = store.categories.find((c) => c.name === bill.category);
    sel.value = match ? String(match.id) : "";
  }
  $("#dlg-bill").showModal();
}

function openCategoryDialog() {
  const form = $("#form-category");
  form.reset();
  $("#category-err").textContent = "";
  $("#dlg-category").showModal();
}

function wireDialogs() {
  $("#form-rule").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    try {
      await api("/api/rules", {
        method: "POST",
        body: {
          match_text: form.match.value,
          category_id: Number($("#rule-cat").value),
          priority: Number(form.priority.value) || 50,
        },
      });
      $("#dlg-rule").close();
      await refreshState();
      render();
    } catch (err) { $("#rule-err").textContent = err.message; }
  });

  $("#form-manual").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    try {
      await api("/api/transactions", {
        method: "POST",
        body: {
          account_id: Number($("#manual-acct").value),
          amount_cents: Math.round(Number(form.amount.value) * 100),
          posted_date: form.date.value,
          description: form.description.value,
          category_id: $("#manual-cat").value ? Number($("#manual-cat").value) : null,
        },
      });
      $("#dlg-manual").close();
      await refreshState();
      render();
    } catch (err) { $("#manual-err").textContent = err.message; }
  });

  $("#form-bill").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    const body = {
      name: form.name.value,
      amount_cents: Math.round(Number(form.amount.value) * 100),
      due_rule: form.due.value,
      category_id: $("#bill-cat").value ? Number($("#bill-cat").value) : null,
      match_pattern: form.pattern.value,
    };
    try {
      if (store.editingBill) {
        await api(`/api/bills/${store.editingBill.id}`, {
          method: "PATCH",
          body: { ...body, set_category: true },
        });
      } else {
        await api("/api/bills", { method: "POST", body });
      }
      $("#dlg-bill").close();
      render();
    } catch (err) { $("#bill-err").textContent = err.message; }
  });

  $("#form-debt").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    try {
      await api("/api/debts", {
        method: "POST",
        body: {
          name: form.name.value,
          balance_cents: Math.round(Number(form.balance.value) * 100),
          note: form.note.value,
        },
      });
      $("#dlg-debt").close();
      form.reset();
      render();
    } catch (err) { $("#debt-err").textContent = err.message; }
  });

  $("#form-account").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    try {
      await api("/api/accounts", {
        method: "POST",
        body: {
          name: form.name.value,
          institution: form.institution.value,
          kind: form.kind.value,
        },
      });
      $("#dlg-account").close();
      form.reset();
      render();
    } catch (err) { $("#account-err").textContent = err.message; }
  });

  $("#form-category").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = e.target;
    try {
      await api("/api/categories", {
        method: "POST",
        body: {
          name: form.name.value,
          grp: form.grp.value,
          cap_cents: form.cap.value ? Math.round(Number(form.cap.value) * 100) : null,
        },
      });
      $("#dlg-category").close();
      store.categories = await api("/api/categories");
      render();
    } catch (err) { $("#category-err").textContent = err.message; }
  });
}

/* --------------------------------------------------------------- router */

async function render() {
  const screen = route();
  const item = NAV.find((n) => n.id === screen) || NAV[0];
  $("#page-title").textContent = item.title;
  $("#page-sub").textContent = "";
  renderNav();
  const content = $("#content");
  content.replaceChildren();
  try {
    if (screen === "dash") await renderDash(content);
    else if (screen === "budget") await renderBudget(content);
    else if (screen === "tx") await renderTx(content);
    else if (screen === "inbox") await renderInbox(content);
    else if (screen === "bills") await renderBills(content);
    else if (screen === "debts") await renderDebts(content);
    else if (screen === "data") await renderData(content);
    else if (screen === "payday") await renderPayday(content);
    else if (screen === "pairs") await renderPairs(content);
    else if (screen === "notion") await renderNotion(content);
    else renderStub(content, screen);
  } catch (e) {
    content.replaceChildren(el("div", "err", `Failed to load: ${e.message}`));
  }
}

async function boot() {
  wireDialogs();
  $("#btn-sync").addEventListener("click", doSync);
  window.addEventListener("hashchange", () => { store.txLimit = 100; render(); });
  await refreshState();
  store.categories = await api("/api/categories");
  await render();
}

boot();
