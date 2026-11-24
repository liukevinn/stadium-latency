// Courtside phone client. No framework: one WebSocket, a few screens.
// The server owns all state; this file only sends commands and renders what it pushes.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "courtside.token";
const THEME_KEY = "courtside.theme";

const LEAGUES = {
  KXNFLGAME: "NFL", KXNCAAFGAME: "College Football", KXNBAGAME: "NBA",
  KXNCAAMBGAME: "College Basketball (M)", KXNCAAWBGAME: "College Basketball (W)",
  KXWNBAGAME: "WNBA", KXNHLGAME: "NHL", KXMLBGAME: "MLB",
};

const S = {
  token: null,
  ws: null,
  authed: false,
  backoff: 500,
  clockOffset: 0, // server clock - phone clock (ms), from the lowest-RTT ping
  bestRtt: Infinity,
  lastPong: 0,
  pending: new Map(), // command id -> {resolve, sentAt}
  snap: null, // last full snapshot
  engine: null,
  quotes: {}, // ticker -> latest quote
  deadlineAt: null,
  deadlineTotal: null,
  lastQty: 0,
  portfolio: null,
  rowsFor: null, // event ticker the market rows were built for
};

// ---------------------------------------------------------------------------
// connection
// ---------------------------------------------------------------------------

function loadToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname); // keep it out of the URL bar
  }
  return localStorage.getItem(TOKEN_KEY);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  setConn("wait");
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: S.token }));
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = (e) => {
    S.authed = false;
    setConn("down");
    if (e.code === 4401) {
      localStorage.removeItem(TOKEN_KEY);
      return showScreen("auth");
    }
    setTimeout(connect, S.backoff);
    S.backoff = Math.min(S.backoff * 2, 5000);
  };
}

function setConn(state) {
  $("conn").className = "dot " + state;
}

let seq = 0;
function send(type, fields = {}) {
  const id = `${Date.now().toString(36)}-${(seq++).toString(36)}`;
  const msg = { type, id, client_ts: Math.round(Date.now() + S.clockOffset), ...fields };
  const sentAt = performance.now();
  const p = new Promise((resolve) => {
    S.pending.set(id, { resolve, sentAt });
    setTimeout(() => {
      if (S.pending.delete(id)) resolve({ ok: false, error: "no response (timeout)" });
    }, 8000);
  });
  if (S.ws && S.ws.readyState === WebSocket.OPEN && S.authed) {
    S.ws.send(JSON.stringify(msg));
  } else {
    // Fallback: plain HTTP POST. keepalive lets it finish even if the page is backgrounded.
    fetch("/api/command", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${S.token}` },
      body: JSON.stringify(msg),
    })
      .then((r) => r.json())
      .then(onMessage)
      .catch(() => {});
  }
  return p;
}

function ping() {
  if (S.ws && S.ws.readyState === WebSocket.OPEN && S.authed) {
    S.ws.send(JSON.stringify({ type: "ping", client_ts: Date.now() }));
  }
  if (S.authed && Date.now() - S.lastPong > 6000) setConn("wait");
}

function onMessage(m) {
  switch (m.type) {
    case "snapshot":
      if (!S.authed) {
        S.authed = true;
        S.backoff = 500;
        ping();
      }
      setConn("ok");
      S.snap = m;
      S.quotes = m.quotes || {};
      S.portfolio = m.portfolio;
      onEngine(m.engine, false);
      render();
      break;
    case "state":
      onEngine(m, true);
      renderEngine();
      renderAccount();
      break;
    case "quote":
      onQuote(m);
      break;
    case "portfolio":
      S.portfolio = m;
      renderAccount();
      if (!$("portfolio").hidden) renderPortfolio();
      break;
    case "alert":
      toast(m.alert.replaceAll("_", " ") + (m.qty ? ` (qty ${m.qty})` : ""), isCritical(m.alert));
      break;
    case "rejected":
      toast(`Rejected: ${m.reason.replaceAll("_", " ")}`);
      break;
    case "pong": {
      const now = Date.now();
      const rtt = now - m.client_ts;
      // Keep the offset from the fastest round trip: it has the least network noise.
      if (rtt <= S.bestRtt * 1.5 || now - S.lastPong > 30000) {
        S.bestRtt = Math.min(S.bestRtt, rtt);
        S.clockOffset = m.server_ts - (m.client_ts + rtt / 2);
      }
      S.lastPong = now;
      setConn("ok");
      break;
    }
    case "ack": {
      const p = S.pending.get(m.id);
      if (p) {
        S.pending.delete(m.id);
        m.rtt_ms = Math.round(performance.now() - p.sentAt);
        p.resolve(m);
      }
      break;
    }
  }
}

function isCritical(alert) {
  return ["not_flat", "position_mismatch", "engine_error", "daily_loss_limit"].includes(alert);
}

// ---------------------------------------------------------------------------
// engine state
// ---------------------------------------------------------------------------

function onEngine(e, live) {
  if (!e) {
    S.engine = null;
    return;
  }
  S.engine = e;
  const qty = Number(e.qty || 0);
  if (live && qty !== S.lastQty) buzz(qty > S.lastQty ? [40, 40, 40] : [80]); // a fill
  S.lastQty = qty;
  if (e.deadline_in_ms != null && qty > 0) {
    S.deadlineAt = Date.now() + e.deadline_in_ms;
    if (e.state === "WAITING" || S.deadlineTotal == null) {
      S.deadlineTotal = Number((S.snap && S.snap.params && S.snap.params.deadline_ms) || e.deadline_in_ms);
    }
  } else {
    S.deadlineAt = null;
    S.deadlineTotal = null;
  }
}

// ---------------------------------------------------------------------------
// rendering: header, market rows (live), engine card, positions
// ---------------------------------------------------------------------------

const market = () => S.snap && S.snap.market;
const teamAName = () => (market() ? market().teams[market().team_a] : "Team A");
const isLive = () => S.snap && S.snap.mode === "live";

function render() {
  const s = S.snap;
  if (!s) return;
  const live = isLive();
  $("env").textContent = s.env.toUpperCase();
  $("main").classList.toggle("paper", !live);
  $("live-banner").hidden = !live;
  const mode = $("mode");
  mode.textContent = live ? "Live" : "Paper";
  mode.classList.toggle("live", live);
  mode.disabled = !s.trading_allowed && !live;

  const mk = market();
  $("league").textContent = mk ? LEAGUES[mk.series] || mk.series : "";
  $("title").textContent = mk ? mk.title : "No game selected";
  const sport = s.sport ? s.sport[0].toUpperCase() + s.sport.slice(1) : "No sport preset";
  const deadline = s.params ? `${s.params.deadline_ms / 1000}s exit` : "";
  $("subtitle").textContent = mk
    ? `${live ? "Live trading" : "Paper: taps log marks"} · ${sport} · ${deadline}`
    : "Tap Games to pick a market.";
  buildRows();
  renderAllQuotes();
  renderEngine();
  renderAccount();
}

// Market rows like Kalshi's list: team, chance, Yes ¢ / No ¢ pills. Team A first.
function buildRows() {
  const mk = market();
  const box = $("markets");
  if (!mk) {
    box.textContent = "";
    S.rowsFor = null;
    return;
  }
  const key = mk.event_ticker + "|" + mk.team_a;
  if (S.rowsFor === key) return;
  S.rowsFor = key;
  box.textContent = "";
  const tickers = Object.keys(mk.teams).sort((a, b) => (a === mk.team_a ? -1 : b === mk.team_a ? 1 : 0));
  for (const t of tickers) {
    const row = document.createElement("div");
    row.className = "mrow";
    row.dataset.ticker = t;
    row.innerHTML = `
      <div class="mrow-head">
        <span class="team">${esc(mk.teams[t])}${t === mk.team_a ? '<span class="badge-a">TEAM A</span>' : ""}</span>
        <span class="chance" data-k="chance">–</span>
      </div>
      <div class="pills">
        <div class="pill yes" data-k="yespill">Yes <b data-k="yes">–</b></div>
        <div class="pill no" data-k="nopill">No <b data-k="no">–</b></div>
      </div>
      <div class="depth" data-k="depth"></div>`;
    box.append(row);
  }
}

function onQuote(q) {
  const prev = S.quotes[q.ticker];
  S.quotes[q.ticker] = q;
  renderQuote(q.ticker, prev);
  const mk = market();
  if (mk && q.ticker === mk.team_a) {
    renderActions();
    renderEnginePnl();
  }
  renderAccount();
}

function renderAllQuotes() {
  for (const t of Object.keys(S.quotes)) renderQuote(t, null);
  renderActions();
}

// Prices shown Kalshi-style: Yes = cost to buy YES (the ask), No = cost to buy NO (100 - bid).
function renderQuote(ticker, prev) {
  const row = document.querySelector(`.mrow[data-ticker="${ticker}"]`);
  const q = S.quotes[ticker];
  if (!row || !q) return;
  const k = (name) => row.querySelector(`[data-k="${name}"]`);
  const yes = q.yes_ask, no = q.yes_bid != null ? 100 - q.yes_bid : null;
  const chance = q.yes_bid != null && q.yes_ask != null ? Math.round((q.yes_bid + q.yes_ask) / 2) : q.yes_bid ?? q.yes_ask;
  k("chance").textContent = chance != null ? `${chance}%` : "–";
  k("yes").textContent = yes != null ? `${yes}¢` : "–";
  k("no").textContent = no != null ? `${no}¢` : "–";
  const sz = (x) => (x ? Math.round(Number(x)).toLocaleString() : "0");
  k("depth").textContent =
    q.yes_bid != null || q.yes_ask != null
      ? `Bid ${q.yes_bid ?? "–"}¢ × ${sz(q.yes_bid_size)}  ·  Ask ${q.yes_ask ?? "–"}¢ × ${sz(q.yes_ask_size)}`
      : "No orders on the book";
  row.classList.toggle("stale", !q.healthy);
  if (prev) {
    flash(k("yespill"), prev.yes_ask, q.yes_ask);
    flash(k("nopill"), prev.yes_bid != null ? 100 - prev.yes_bid : null, no);
  }
}

function flash(el, before, after) {
  if (before == null || after == null || before === after) return;
  el.classList.remove("flash-up", "flash-down");
  void el.offsetWidth; // restart the fade
  el.classList.add(after > before ? "flash-up" : "flash-down");
  setTimeout(() => el.classList.remove("flash-up", "flash-down"), 120);
}

function renderActions() {
  const mk = market();
  const live = isLive();
  const team = teamAName();
  $("up-label").textContent = team;
  $("down-label").textContent = team;
  const q = mk ? S.quotes[mk.team_a] : null;
  const yes = q && q.yes_ask != null ? `${q.yes_ask}¢` : "–";
  const no = q && q.yes_bid != null ? `${100 - q.yes_bid}¢` : "–";
  $("up-sub").textContent = live ? `Buy Yes · ${yes}` : `Mark good · Yes ${yes}`;
  $("down-sub").textContent = live ? `Buy No · ${no}` : `Mark bad · No ${no}`;
  $("up").disabled = $("down").disabled = !mk;
  $("flatten").disabled = !(S.snap && S.snap.engine);
}

function renderEngine() {
  renderActions();
  const e = S.engine;
  const chip = $("state");
  const state = e ? e.state : "PAPER";
  chip.textContent = state.replace("_", " ");
  chip.className = "state-chip" + (["ENTERING", "WAITING"].includes(state) ? " active" : state.startsWith("EXITING") ? " exiting" : "");
  if (e && Number(e.qty) > 0) {
    const side = e.outcome === "yes" ? "Yes" : "No";
    $("position").innerHTML = `<span class="chip ${e.outcome}">${side}</span> ${Number(e.qty)} ${esc(teamAName())} @ ${Number(e.avg_entry).toFixed(1)}¢`;
  } else {
    $("position").textContent = e ? "Flat" : "Paper only. Taps log marks, no orders.";
  }
  renderEnginePnl();
}

function renderEnginePnl() {
  const e = S.engine;
  if (!e) return ($("pnl").textContent = "");
  let html = `Today ${usd(Number(e.realized_today), true)}`;
  const mk = market();
  const q = mk ? S.quotes[mk.team_a] : null;
  const qty = Number(e.qty || 0);
  if (qty > 0 && q && q.yes_bid != null && q.yes_ask != null) {
    const ownBid = e.outcome === "yes" ? q.yes_bid : 100 - q.yes_ask;
    html = `Open ${usd(((ownBid - Number(e.avg_entry)) * qty) / 100, true)} · ` + html;
  }
  $("pnl").innerHTML = html;
}

function tick() {
  // countdown to the deadline, driven locally between server updates
  if (S.deadlineAt && S.engine && Number(S.engine.qty) > 0) {
    const left = Math.max(0, S.deadlineAt - Date.now());
    $("countdown").textContent = (left / 1000).toFixed(1) + "s";
    $("deadline-bar").style.width = `${Math.min(100, (left / (S.deadlineTotal || left || 1)) * 100)}%`;
  } else {
    $("countdown").textContent = "";
    $("deadline-bar").style.width = "0";
  }
  requestAnimationFrame(tick);
}

// ---- account / portfolio (Kalshi's view of the account) -----------------------------

// The live quote is fresher than the 3 s portfolio poll, so prefer it when we have one.
function liveMark(h) {
  const q = S.quotes[h.ticker];
  if (q && q.yes_bid != null && q.yes_ask != null) return h.outcome === "yes" ? q.yes_bid : 100 - q.yes_ask;
  return h.mark_cents == null ? null : Number(h.mark_cents);
}

function holdingPnl(h) {
  const mark = liveMark(h);
  return mark == null ? null : (mark * Number(h.qty)) / 100 - Number(h.cost);
}

function sideChip(h) {
  return `<span class="chip ${h.outcome}">${h.outcome === "yes" ? "Yes" : "No"}</span>`;
}

function renderAccount() {
  const pf = S.portfolio;
  if (!pf) return;
  $("cash").textContent = usd(Number(pf.cash));
  const total = pf.holdings.reduce((a, h) => a + (holdingPnl(h) || 0), 0);
  $("open-pnl").innerHTML = pf.holdings.length ? usd(total, true) : "–";
  const mk = market();
  const here = pf.holdings.filter((h) => mk && h.event_ticker === mk.event_ticker);
  const others = pf.holdings.filter((h) => !here.includes(h));
  $("here").innerHTML = here.length
    ? here
        .map((h) => `${sideChip(h)} ${Number(h.qty)} ${esc(h.team)} @ ${Number(h.avg_cents)}¢ · ${usd(holdingPnl(h), true)}`)
        .join("<br>")
    : mk ? '<span class="muted">No position in this game</span>' : "";
  const otherPnl = others.reduce((a, h) => a + (holdingPnl(h) || 0), 0);
  $("others").innerHTML = others.length
    ? `${others.length} other position${others.length > 1 ? "s" : ""} · ${usd(otherPnl, true)} · View all ›`
    : "View portfolio ›";
}

function renderPortfolio() {
  const pf = S.portfolio;
  if (!pf) return;
  const total = pf.holdings.reduce((a, h) => a + (holdingPnl(h) || 0), 0);
  const value = pf.holdings.reduce((a, h) => {
    const m = liveMark(h);
    return a + (m == null ? 0 : (m * Number(h.qty)) / 100);
  }, 0);
  $("pf-summary").innerHTML = `
    <div class="kv"><span>Portfolio</span><b>${usd(Number(pf.cash) + value)}</b></div>
    <div class="kv"><span>Cash</span><b>${usd(Number(pf.cash))}</b></div>
    <div class="kv"><span>Positions</span><b>${usd(value)}</b></div>
    <div class="kv"><span>Open P&amp;L</span><b>${pf.holdings.length ? usd(total, true) : "–"}</b></div>`;
  const mk = market();
  const list = $("pf-list");
  list.textContent = "";
  if (!pf.holdings.length) list.innerHTML = '<p class="hint">No open positions.</p>';
  for (const h of pf.holdings) {
    const el = document.createElement("div");
    el.className = "holding" + (mk && h.event_ticker === mk.event_ticker ? " current" : "");
    const mark = liveMark(h);
    el.innerHTML = `
      <div class="row"><span class="name">${esc(h.team)}</span>${usd(holdingPnl(h), true)}</div>
      <div class="small muted">${sideChip(h)} ${Number(h.qty)} contracts · pays if ${esc(h.team)} ${h.outcome === "yes" ? "wins" : "loses"}</div>
      <div class="ticker">${esc(h.ticker)}</div>
      <div class="grid">
        <div class="kv"><span>Avg price</span><b>${Number(h.avg_cents)}¢</b></div>
        <div class="kv"><span>Bid now</span><b>${mark == null ? "–" : mark + "¢"}</b></div>
        <div class="kv"><span>Cost</span><b>${usd(Number(h.cost))}</b></div>
        <div class="kv"><span>Value</span><b>${mark == null ? "–" : usd((mark * Number(h.qty)) / 100)}</b></div>
        <div class="kv"><span>Realized</span><b>${usd(Number(h.realized), true)}</b></div>
        <div class="kv"><span>Fees paid</span><b>${usd(Number(h.fees))}</b></div>
      </div>`;
    list.append(el);
  }
  const age = Math.max(0, Math.round((Date.now() - (pf.t_ms - S.clockOffset)) / 1000));
  $("pf-updated").textContent = `From your Kalshi account, updated ${age}s ago. Open P&L is before exit fees.`;
}

function usd(x, signed = false) {
  if (x == null || Number.isNaN(x)) return '<span class="muted">–</span>';
  const s = `$${Math.abs(x).toFixed(2)}`;
  if (!signed) return (x < 0 ? "-" : "") + s;
  return `<span class="${x >= 0 ? "pos" : "neg"}">${x >= 0 ? "+" : "-"}${s}</span>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

let toastTimer = null;
function toast(text, critical = false) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast" + (critical ? " critical" : "");
  el.hidden = false;
  buzz(critical ? [200, 100, 200] : [60]);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), critical ? 15000 : 4000);
}

// ---------------------------------------------------------------------------
// input
// ---------------------------------------------------------------------------

function buzz(pattern) {
  if (navigator.vibrate) navigator.vibrate(pattern);
  else $("haptic-label").click(); // iOS 18+: haptic tick
}

async function tap(dir, btn) {
  buzz(25);
  btn.classList.add("pressed");
  setTimeout(() => btn.classList.remove("pressed"), 120);
  const ack = await send("tap", { dir });
  if (!ack.ok) return toast(ack.error || "Tap failed");
  const what = ack.traded ? "sent" : "marked";
  const up = ack.phone_to_server_ms != null ? ` · ${ack.phone_to_server_ms}ms to server` : "";
  $("latency").textContent = `Last ${dir === "up" ? "▲" : "▼"} ${what}${up} · ack ${ack.rtt_ms}ms`;
  if (ack.traded) setTimeout(showOrderLatency, 1500);
}

function showOrderLatency() {
  const lt = S.engine && S.engine.last_trade;
  if (lt && lt.order_rtt_ms != null) $("latency").textContent += ` · order ${lt.order_rtt_ms}ms`;
}

function bindPad(id, dir) {
  const btn = $(id);
  // pointerdown fires on touch-start: no click delay, no waiting for release
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (!btn.disabled) tap(dir, btn);
  });
}

function bindFlatten() {
  const btn = $("flatten");
  let timer = null;
  const cancel = () => {
    clearTimeout(timer);
    btn.classList.remove("holding");
  };
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    btn.classList.add("holding");
    buzz(15);
    timer = setTimeout(async () => {
      btn.classList.remove("holding");
      buzz([80, 40, 80]);
      const ack = await send("flatten");
      if (!ack.ok) toast(ack.error || "Flatten failed", true);
    }, 600);
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, cancel));
}

async function toggleMode() {
  const live = isLive();
  if (!live && !confirm("Switch to LIVE? ▲ and ▼ will place real orders.")) return;
  const ack = await send("mode", { mode: live ? "paper" : "live" });
  if (!ack.ok) toast(ack.error);
}

// ---------------------------------------------------------------------------
// screens: picker, settings, portfolio
// ---------------------------------------------------------------------------

function showScreen(name) {
  for (const id of ["auth", "main", "picker", "settings", "portfolio"]) $(id).hidden = id !== name;
}

function guessSport(series) {
  if (/NFL|NCAAF/.test(series)) return "football";
  if (/NBA|NCAAMB|NCAAWB|WNBA/.test(series)) return "basketball";
  if (/MLB/.test(series)) return "baseball";
  return "";
}

let searchTimer = null;
async function doSearch() {
  const box = $("games");
  const ack = await send("search", { q: $("search").value.trim() });
  box.textContent = "";
  if (!ack.ok) return (box.innerHTML = `<p class="hint">${esc(ack.error)}</p>`);
  if (!ack.games.length) return (box.innerHTML = '<p class="hint">No open games match.</p>');
  const mk = market();
  for (const g of ack.games) {
    const card = document.createElement("div");
    card.className = "game";
    card.innerHTML = `<div class="game-league">${esc(LEAGUES[g.series] || g.series)}</div><div class="game-title">${esc(g.title)}</div>`;
    for (const m of g.markets) {
      const b = document.createElement("button");
      b.className = "pick" + (mk && m.ticker === mk.team_a ? " current" : "");
      const bid = m.yes_bid ? Math.round(Number(m.yes_bid) * 100) : null;
      const ask = m.yes_ask ? Math.round(Number(m.yes_ask) * 100) : null;
      const chance = bid != null && ask != null && ask < 100 ? `${Math.round((bid + ask) / 2)}%` : "–";
      b.innerHTML = `<span>${esc(m.team)}</span><span class="chance">${chance}</span>`;
      b.onclick = () => selectGame(g, m.ticker);
      card.append(b);
    }
    box.append(card);
  }
}

async function selectGame(g, ticker) {
  const chosen = document.querySelector("#sport-chips .on");
  const sport = chosen && chosen.dataset.sport ? chosen.dataset.sport : guessSport(g.series);
  $("games").innerHTML = '<p class="hint">Connecting to the market…</p>';
  const ack = await send("select", { event_ticker: g.event_ticker, team_a: ticker, sport: sport || null });
  if (!ack.ok) return ($("games").innerHTML = `<p class="hint">${esc(ack.error)}</p>`);
  showScreen("main");
}

const FIELDS = [
  ["size", "Contracts per tap"],
  ["chase_cents", "Chase ¢", "Max above the pre-play ask"],
  ["lookback_ms", "Lookback ms", "Pre-play price this long before the tap"],
  ["min_move_cents", "Min move ¢", "Bid gain that counts as caught up"],
  ["deadline_ms", "Deadline ms", "Flat this long after the fill"],
  ["wait_max_ms", "Max wait ms", "Blank = half the deadline"],
  ["stop_cents", "Auto-stop ¢", "0 = off"],
  ["max_position", "Max position"],
  ["max_price", "Max price ¢"],
  ["daily_loss_limit", "Daily loss limit $"],
];

function openSettings() {
  const form = $("settings-form");
  form.textContent = "";
  const p = (S.snap && S.snap.params) || {};
  for (const [key, label, hint] of FIELDS) {
    const row = document.createElement("div");
    row.className = "setting";
    row.innerHTML = `<label>${label}${hint ? `<small>${hint}</small>` : ""}</label>`;
    const input = document.createElement("input");
    input.name = key;
    input.inputMode = "decimal";
    input.value = p[key] == null ? "" : p[key];
    row.append(input);
    form.append(row);
  }
  $("settings-msg").textContent = S.snap && S.snap.sport ? `Sport preset: ${S.snap.sport}` : "";
  markTheme();
  showScreen("settings");
}

async function saveSettings() {
  const params = {};
  for (const input of $("settings-form").querySelectorAll("input")) {
    params[input.name] = input.value === "" ? null : input.value;
  }
  const ack = await send("params", { params });
  $("settings-msg").textContent = ack.ok ? "Saved." : ack.error;
  if (ack.ok) setTimeout(() => showScreen("main"), 400);
}

function setTheme(t) {
  localStorage.setItem(THEME_KEY, t);
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  markTheme();
}

function markTheme() {
  const t = localStorage.getItem(THEME_KEY) || "system";
  for (const b of document.querySelectorAll("#theme button")) b.classList.toggle("on", b.dataset.theme === t);
}

// ---------------------------------------------------------------------------
// wake lock & startup
// ---------------------------------------------------------------------------

async function keepAwake() {
  try {
    if ("wakeLock" in navigator && document.visibilityState === "visible") await navigator.wakeLock.request("screen");
  } catch (e) {
    /* needs HTTPS; not fatal */
  }
}

function init() {
  S.token = loadToken();
  $("token-save").onclick = () => {
    const t = $("token-input").value.trim();
    if (!t) return;
    localStorage.setItem(TOKEN_KEY, t);
    S.token = t;
    showScreen("main");
    connect();
  };
  $("forget-token").onclick = () => {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
  };
  bindPad("up", "up");
  bindPad("down", "down");
  bindFlatten();
  $("mode").onclick = toggleMode;
  $("open-picker").onclick = () => {
    showScreen("picker");
    doSearch();
  };
  const openPortfolio = () => {
    showScreen("portfolio");
    renderPortfolio();
    send("portfolio");
  };
  $("open-portfolio").onclick = openPortfolio;
  $("account").onclick = openPortfolio;
  $("portfolio-refresh").onclick = () => send("portfolio");
  $("open-settings").onclick = openSettings;
  $("settings-save").onclick = saveSettings;
  for (const b of document.querySelectorAll("#theme button")) b.onclick = () => setTheme(b.dataset.theme);
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, 250);
  });
  for (const chip of document.querySelectorAll("#sport-chips button")) {
    chip.onclick = () => {
      document.querySelectorAll("#sport-chips button").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
    };
  }
  for (const b of document.querySelectorAll("[data-close]")) b.onclick = () => showScreen("main");
  document.addEventListener("visibilitychange", keepAwake);
  document.addEventListener("contextmenu", (e) => e.preventDefault()); // no long-press menu

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  if (!S.token) return showScreen("auth");
  showScreen("main");
  keepAwake();
  connect();
  setInterval(ping, 3000);
  requestAnimationFrame(tick);
}

init();
