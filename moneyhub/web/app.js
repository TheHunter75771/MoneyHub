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
};

const NAV = [
  { id: "dash", label: "Dashboard", title: "Dashboard" },
  { id: "budget", label: "Budget", title: "Budget" },
  { id: "tx", label: "Transactions", title: "Transactions" },
  { id: "inbox", label: "Inbox", title: "Inbox", badge: true },
  { id: "bills", label: "Bills", title: "Bills" },
  { id: "debts", label: "Debts", title: "Debts" },
  { id: "data", label: "Data & Rules", title: "Data & Rules" },
  { id: "payday", label: "Payday", title: "Payday flow", later: true },
  { id: "pairs", label: "Transfer pairs", title: "Transfer pairs", later: true },
  { id: "notion", label: "Notion", title: "Notion projection", later: true },
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
  $("#sync-at").textContent = store.syncing ? "syncing…" : `Synced ${relTime(store.state?.sync_last_at)}`;
  const stale = $("#stale-note");
  const n = store.state?.stale_accounts || 0;
  stale.hidden = n === 0;
  stale.textContent = n === 1 ? "1 account stale" : `${n} accounts stale`;
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
