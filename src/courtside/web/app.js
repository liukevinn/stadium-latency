// Courtside phone client. No framework: one WebSocket, a few screens, big buttons.
// The server owns all state; this file only sends commands and renders snapshots.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "courtside.token";

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
  engine: null, // last engine state
  quote: null,
  deadlineAt: null, // phone clock when the deadline hits
  deadlineTotal: null,
  lastQty: 0,
  sport: null,
  portfolio: null, // Kalshi account view, pushed every ~3 s
};

// ---------------------------------------------------------------------------
// token
// ---------------------------------------------------------------------------

function loadToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname); // keep it out of the URL bar
  }
  return localStorage.getItem(TOKEN_KEY);
}

// ---------------------------------------------------------------------------
// connection
// ---------------------------------------------------------------------------

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

function nowServer() {
  return Date.now() + S.clockOffset;
}

let seq = 0;
function send(type, fields = {}) {
  const id = `${Date.now().toString(36)}-${(seq++).toString(36)}`;
  const msg = { type, id, client_ts: Math.round(nowServer()), ...fields };
  const sentAt = performance.now();
  const p = new Promise((resolve) => {
    S.pending.set(id, { resolve, sentAt });
    setTimeout(() => {
      if (S.pending.delete(id)) resolve({ ok: false, error: "no ack (timeout)" });
    }, 8000);
  });
  if (S.ws && S.ws.readyState === WebSocket.OPEN && S.authed) {
    S.ws.send(JSON.stringify(msg));
  } else {
    // Fallback: plain HTTPS POST. keepalive lets it finish even if the page is backgrounded.
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
      S.engine = m.engine;
      S.quote = m.quote;
      S.sport = m.sport;
      S.portfolio = m.portfolio;
      onEngine(m.engine, false);
      render();
      break;
    case "state":
      onEngine(m, true);
      render();
      break;
    case "quote":
      S.quote = m;
      renderQuote();
      renderPnl();
      renderAccount();
      break;
    case "portfolio":
      S.portfolio = m;
      renderAccount();
      if (!$("portfolio").hidden) renderPortfolio();
      break;
    case "alert":
      showAlert(m);
      break;
    case "rejected":
      showAlert({ alert: `rejected: ${m.reason}` });
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

// ---------------------------------------------------------------------------
// engine state
// ---------------------------------------------------------------------------

function onEngine(e, live) {
  if (!e) return;
  S.engine = e;
  const qty = Number(e.qty || 0);
  if (live && qty !== S.lastQty) buzz(qty > S.lastQty ? [40, 40, 40] : [80]); // fill
  S.lastQty = qty;
  if (e.deadline_in_ms != null && qty > 0) {
    S.deadlineAt = Date.now() + e.deadline_in_ms;
    if (S.deadlineTotal == null || e.state === "WAITING") {
      S.deadlineTotal = Number(S.snap?.params?.deadline_ms || e.deadline_in_ms);
    }
  } else {
    S.deadlineAt = null;
  }
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function teamA() {
  const mk = S.snap && S.snap.market;
  return mk ? mk.teams[mk.team_a] : null;
}

function render() {
  const s = S.snap;
  if (!s) return;
  $("env").textContent = s.env.toUpperCase();
  const live = s.mode === "live";
  const main = $("main");
  main.classList.toggle("live", live);
  main.classList.toggle("paper", !live);
  const mode = $("mode");
  mode.textContent = live ? "LIVE" : "PAPER";
  mode.className = "mode " + (live ? "live" : "paper");
  mode.disabled = !s.trading_allowed && !live;

  const mk = s.market;
  $("title").textContent = mk ? mk.title : "No game selected — tap GAME";
  const team = teamA() || "TEAM A";
  $("team-a").textContent = team;
  $("up-label").textContent = live ? team : `MARK ${team}`;
  $("down-label").textContent = live ? team : `MARK ${team}`;
  $("up").disabled = $("down").disabled = !mk;
  $("flatten").disabled = !(s.engine);

  const e = S.engine;
  $("state").textContent = e ? e.state : live ? "–" : "PAPER";
  if (e && Number(e.qty) > 0) {
    const side = e.outcome === "yes" ? `${team} YES` : `${team} NO`;
    $("position").textContent = `${Number(e.qty)} ${side} @ ${e.avg_entry}`;
  } else {
    $("position").textContent = e ? "flat" : "paper only: taps log MARKs, no orders";
  }
  renderQuote();
  renderPnl();
  renderAccount();
}

// ---------------------------------------------------------------------------
// account / portfolio (Kalshi's view)
// ---------------------------------------------------------------------------

// The live quote is fresher than the 3 s portfolio poll, so use it for the current game.
function liveMark(h) {
  const mk = S.snap && S.snap.market;
  const q = S.quote;
  if (!mk || !q || h.ticker !== mk.team_a || q.yes_bid == null || q.yes_ask == null) {
    return h.mark_cents == null ? null : Number(h.mark_cents);
  }
  return h.outcome === "yes" ? q.yes_bid : 100 - q.yes_ask;
}

function holdingPnl(h) {
  const mark = liveMark(h);
  return mark == null ? null : (mark * Number(h.qty)) / 100 - Number(h.cost);
}

function describe(h) {
  const qty = Number(h.qty);
  return h.outcome === "yes" ? `LONG ${qty} ${h.team} YES (wins)` : `LONG ${qty} ${h.team} NO (loses)`;
}

function pnlSpan(x) {
  if (x == null) return '<span class="dim">no bid</span>';
  return `<span class="${x >= 0 ? "pos" : "neg"}">${fmtUsd(x)}</span>`;
}

function renderAccount() {
  const pf = S.portfolio;
  const card = $("account");
  if (!pf) {
    $("cash").textContent = "–";
    $("open-pnl").textContent = "–";
    return;
  }
  card.classList.toggle("stale-data", Date.now() - (pf.t_ms - S.clockOffset) > 15000);
  $("cash").textContent = `$${Number(pf.cash).toFixed(2)}`;
  const total = pf.holdings.reduce((acc, h) => acc + (holdingPnl(h) || 0), 0);
  $("open-pnl").innerHTML = pf.holdings.length ? pnlSpan(total) : "–";

  const mk = S.snap && S.snap.market;
  const here = pf.holdings.filter((h) => mk && h.event_ticker === mk.event_ticker);
  const others = pf.holdings.filter((h) => !here.includes(h));
  $("here").innerHTML = here.length
    ? here.map((h) => `${esc(describe(h))} @ ${Number(h.avg_cents)} · ${pnlSpan(holdingPnl(h))}`).join("<br>")
    : mk ? "no position in this game" : "";
  const otherPnl = others.reduce((acc, h) => acc + (holdingPnl(h) || 0), 0);
  $("others").innerHTML = others.length
    ? `+ ${others.length} other position${others.length > 1 ? "s" : ""} · ${pnlSpan(otherPnl)} · tap for all`
    : "tap for portfolio";
}

function renderPortfolio() {
  const pf = S.portfolio;
  if (!pf) return;
  const total = pf.holdings.reduce((acc, h) => acc + (holdingPnl(h) || 0), 0);
  const value = pf.holdings.reduce((acc, h) => {
    const m = liveMark(h);
    return acc + (m == null ? 0 : (m * Number(h.qty)) / 100);
  }, 0);
  $("pf-summary").innerHTML = `
    <div><span>Cash</span><b>$${Number(pf.cash).toFixed(2)}</b></div>
    <div><span>Positions value</span><b>$${value.toFixed(2)}</b></div>
    <div><span>Open P&amp;L</span><b>${pnlSpan(total)}</b></div>
    <div><span>Account</span><b>$${(Number(pf.cash) + value).toFixed(2)}</b></div>`;
  const mk = S.snap && S.snap.market;
  const list = $("pf-list");
  list.textContent = "";
  if (!pf.holdings.length) list.textContent = "No open positions.";
  for (const h of pf.holdings) {
    const el = document.createElement("div");
    el.className = "holding" + (mk && h.event_ticker === mk.event_ticker ? " current" : "");
    const mark = liveMark(h);
    el.innerHTML = `
      <div class="top"><span>${esc(describe(h))}</span>${pnlSpan(holdingPnl(h))}</div>
      <div class="small dim">${esc(h.ticker)}</div>
      <div class="grid">
        <div><span>avg</span><b>${Number(h.avg_cents)}¢</b></div>
        <div><span>bid now</span><b>${mark == null ? "–" : mark + "¢"}</b></div>
        <div><span>cost</span><b>$${Number(h.cost).toFixed(2)}</b></div>
        <div><span>realized</span><b>${fmtUsd(Number(h.realized))}</b></div>
        <div><span>fees</span><b>$${Number(h.fees).toFixed(2)}</b></div>
        <div><span>value</span><b>${h.value == null ? "–" : "$" + Number(h.value).toFixed(2)}</b></div>
      </div>`;
    list.append(el);
  }
  const age = Math.round((Date.now() - (pf.t_ms - S.clockOffset)) / 1000);
  $("pf-updated").textContent = `Kalshi account, updated ${Math.max(0, age)}s ago. Open P&L is before exit fees.`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function renderQuote() {
  const q = S.quote;
  $("bid").textContent = q && q.yes_bid != null ? q.yes_bid : "–";
  $("ask").textContent = q && q.yes_ask != null ? q.yes_ask : "–";
  $("market").classList.toggle("stale", !!q && !q.healthy);
}

function renderPnl() {
  const e = S.engine;
  if (!e) {
    $("pnl").textContent = "";
    return;
  }
  let text = `day ${fmtUsd(Number(e.realized_today))}`;
  const q = S.quote;
  const qty = Number(e.qty || 0);
  if (qty > 0 && q && q.yes_bid != null && q.yes_ask != null) {
    const ownBid = e.outcome === "yes" ? q.yes_bid : 100 - q.yes_ask;
    const open = ((ownBid - Number(e.avg_entry)) * qty) / 100;
    text = `open ${fmtUsd(open)} · ` + text;
  }
  $("pnl").textContent = text;
}

function fmtUsd(x) {
  const sign = x < 0 ? "-" : "+";
  return `${sign}$${Math.abs(x).toFixed(2)}`;
}

function tick() {
  // countdown to the deadline, driven locally between server updates
  if (S.deadlineAt && S.engine && Number(S.engine.qty) > 0) {
    const left = Math.max(0, S.deadlineAt - Date.now());
    $("countdown").textContent = (left / 1000).toFixed(1) + "s";
    const total = S.deadlineTotal || left || 1;
    $("deadline-bar").style.width = `${Math.min(100, (left / total) * 100)}%`;
  } else {
    $("countdown").textContent = "";
    $("deadline-bar").style.width = "0";
  }
  requestAnimationFrame(tick);
}

let alertTimer = null;
function showAlert(a) {
  const el = $("alert");
  const critical = ["not_flat", "position_mismatch", "engine_error", "daily_loss_limit"].includes(a.alert);
  el.textContent = `${a.alert.replaceAll("_", " ")}${a.qty ? ` (qty ${a.qty})` : ""}`;
  el.className = "alert" + (critical ? " critical" : "");
  el.hidden = false;
  buzz(critical ? [200, 100, 200] : [60]);
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => (el.hidden = true), critical ? 15000 : 5000);
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
  if (!ack.ok) return showAlert({ alert: ack.error || "tap failed" });
  const what = ack.traded ? "sent" : "marked";
  const up = ack.phone_to_server_ms != null ? `${ack.phone_to_server_ms}ms up` : "";
  $("latency").textContent = `last ${dir === "up" ? "▲" : "▼"} ${what} · ${up} · ack ${ack.rtt_ms}ms`;
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
      if (!ack.ok) showAlert({ alert: ack.error || "flatten failed" });
    }, 600);
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, cancel));
}

async function toggleMode() {
  const live = S.snap && S.snap.mode === "live";
  if (!live && !confirm("Switch to LIVE? ▲/▼ will place real orders.")) return;
  const ack = await send("mode", { mode: live ? "paper" : "live" });
  if (!ack.ok) showAlert({ alert: ack.error });
}

// ---------------------------------------------------------------------------
// screens: picker and settings
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
  const ack = await send("search", { q: $("search").value.trim() });
  const box = $("games");
  box.textContent = "";
  if (!ack.ok) return (box.textContent = ack.error);
  if (!ack.games.length) return (box.textContent = "No open games match.");
  const current = S.snap && S.snap.market ? S.snap.market.team_a : null;
  for (const g of ack.games) {
    const card = document.createElement("div");
    card.className = "game";
    const title = document.createElement("div");
    title.textContent = g.title;
    const teams = document.createElement("div");
    teams.className = "teams";
    for (const m of g.markets) {
      const b = document.createElement("button");
      b.textContent = `${m.team}  ${fmtPx(m.yes_bid)}/${fmtPx(m.yes_ask)}`;
      if (m.ticker === current) b.classList.add("current");
      b.onclick = () => selectGame(g, m.ticker);
      teams.append(b);
    }
    card.append(title, teams);
    box.append(card);
  }
}

function fmtPx(d) {
  return d ? Math.round(Number(d) * 100) : "–";
}

async function selectGame(g, ticker) {
  const chosen = document.querySelector("#sport-chips .on");
  const sport = chosen ? chosen.dataset.sport : guessSport(g.series);
  $("games").textContent = "Connecting to the market…";
  const ack = await send("select", { event_ticker: g.event_ticker, team_a: ticker, sport: sport || null });
  if (!ack.ok) {
    $("games").textContent = ack.error;
    return;
  }
  showScreen("main");
}

const FIELDS = [
  ["size", "Contracts per tap"],
  ["chase_cents", "Chase ¢", "max above pre-play ask"],
  ["lookback_ms", "Lookback ms", "pre-play price this long before tap"],
  ["min_move_cents", "Min move ¢", "bid gain = caught up"],
  ["deadline_ms", "Deadline ms", "flat this long after fill"],
  ["wait_max_ms", "Max wait ms", "blank = deadline / 2"],
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
    const l = document.createElement("label");
    l.textContent = label;
    if (hint) {
      const small = document.createElement("small");
      small.textContent = hint;
      l.append(small);
    }
    const input = document.createElement("input");
    input.name = key;
    input.inputMode = "decimal";
    input.value = p[key] == null ? "" : p[key];
    form.append(l, input);
  }
  $("settings-msg").textContent = S.sport ? `Sport preset: ${S.sport}` : "";
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

// ---------------------------------------------------------------------------
// wake lock & startup
// ---------------------------------------------------------------------------

let wakeLock = null;
async function keepAwake() {
  try {
    if ("wakeLock" in navigator && document.visibilityState === "visible") {
      wakeLock = await navigator.wakeLock.request("screen");
    }
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
  $("open-settings").onclick = openSettings;
  $("account").onclick = () => {
    showScreen("portfolio");
    renderPortfolio();
    send("portfolio");
  };
  $("portfolio-refresh").onclick = () => send("portfolio");
  $("settings-save").onclick = saveSettings;
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
