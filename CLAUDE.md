# CLAUDE.md — Courtside (working name)

This is the main context file for this project. Read all of it before writing code. If anything here conflicts with what Kalshi's API actually does, trust the live docs (https://docs.kalshi.com), change the code to match, and update this file.

---

## 1. What we're building, in one paragraph

A personal tool for trading Kalshi **sports win markets from inside the stadium**. The owner is in the stands and sees plays before the market reacts. When something swings the game, he taps one big button on his phone. A server buys whatever offers are still sitting at the pre-play price, waits for the market to catch up, and then sells. The whole round trip takes seconds. **This is a latency trade, not a view on who wins.** Positions should almost never last longer than one "swing moment."

It must work for **any sport** (football, basketball, baseball, and so on). Above all it must be **fast and easy to use one-handed in a loud stadium.**

---

## 2. Motive and background

- Owner: Kevin Liu, a Duke CS & Statistics senior. He has a full-time quant developer offer at IMC (starts Aug 2027). He is comfortable with Python, market microstructure, and Kalshi's API concepts. He built a Kalshi market-making engine before (Python, Avellaneda-Stoikov), so ask him whether there's auth/client code to reuse.
- Origin: at the Duke–Clemson football game (Fri Nov 20, 2026) he traded by hand in the Kalshi mobile app. He'd enter the size, hold his finger on the "slide to trade" control, and release it the moment a play resolved (for example, a 3rd-down conversion late in a close game). He went from **$25 to $37**. **Many orders were rejected with "price is changing"**, but the trades that did go through were almost always profitable.
- Thesis: at games where market makers are decent but not perfect, there will always be some **stale orders** to pick off right after a swing. The mobile app is too slow and too easily rejected. A purpose-built tool should get more fills, buy at better prices, and handle the exit automatically.
- How Kevin works: skeptical and evidence-first. He wants **explicit stop criteria** and measurement built in from day one. Keep communication concise and direct.

---

## 3. Known risks (keep these visible; don't design around them silently)

1. **Kalshi may delay retail orders.** In late 2025 Kalshi reportedly filed with the CFTC to add order-execution delays for some sports orders, aimed at in-stadium traders. The current status and who it applies to are **unverified**. If a delay is applied on Kalshi's end, no client speed helps. **Milestone 0 checks this.**
2. **The biggest source of latency is the stadium cell network**, not the app. That's why the phone only sends a tiny trigger and the server does everything else.
3. **The pros see official data feeds fast.** We only get fills when market makers are slow. Expect a low fill rate; it's a feature of the edge, not a bug.
4. **Thin books.** Profit is capped by how much is resting at stale prices. Keep size small.
5. **Plays get reversed** (flags, replay reviews, foul calls). This is the largest single risk. It's handled by a manual FLATTEN plus an optional automatic stop.
6. **Compliance.** Kevin joins IMC in Aug 2027. IMC's personal-trading policy will very likely restrict this. Not a code concern, but don't build anything that assumes long-term use without that check.
7. Legal: this is not legal advice. Trading on what you can see from the stands is generally not considered insider trading, but venue rules can restrict it. Read Kalshi's current rulebook and API terms.

---

## 4. The trading idea in plain terms

- Every trade is the same regardless of sport: *something just happened that makes Team A more (or less) likely to win, and the order book hasn't updated yet.*
- **▲ = good for Team A → buy YES on "Team A wins."**
- **▼ = bad for Team A → buy NO on "Team A wins"** (equivalently, bet against Team A).
- The profit is the gap between the stale price we pay and where the book settles once market makers catch up. It's **realized by selling after the market reprices**. Holding beyond that is a coin flip on the next play, so we don't.

---

## 5. Architecture

```
[Phone PWA] --(persistent WebSocket, tiny messages)--> [Execution server] --(REST + WebSocket)--> [Kalshi]
   big buttons, state display                      holds API key, live book, state machine, logs
```

- **Execution server** (Python 3.11+, asyncio). Suggested stack: FastAPI/Starlette for the phone-facing HTTP and WebSocket, `websockets` or `aiohttp` for the Kalshi WebSocket, and `httpx` (HTTP/2, keep-alive) for Kalshi REST. Deploy on a small always-on VM, ideally **AWS us-east-2 (Ohio)**, which third-party measurements suggest is close to Kalshi. Run it locally during development.
- **Phone client:** a single-page PWA served by the same server, added to the home screen. Use no framework, or something very light. It keeps one WebSocket open to the server, sends triggers over it, and receives state. If the WebSocket is down, it falls back to an HTTP POST.
- **The Kalshi API key/private key lives ONLY on the server**, never on the phone. The phone authenticates to the server with a long random token (or a better scheme) over HTTPS/WSS.
- **The server must handle the entire exit without the phone.** Phone signal will drop; trades must still finish cleanly.

### Kalshi integration (verify every item against the docs)
- Production REST base: `https://api.elections.kalshi.com/trade-api/v2` (serves all markets, despite the name). Docs also list `https://external-api.kalshi.com/trade-api/v2`. WebSocket: `wss://api.elections.kalshi.com/trade-api/ws/v2`.
- **Demo environment** exists (`demo-api.kalshi.co`). **All development and testing happens there first.**
- Authentication: RSA-signed headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` (timestamp + method + path). Sign at send time.
- Orders: `POST /portfolio/orders` with ticker, side (yes/no), action (buy/sell), count, type=limit, price, `client_order_id`. **Check exactly how immediate-or-cancel / fill-or-kill / expiration are specified now.** The API has changed field names and price formats (cents integers vs. dollar strings like `"0.5600"`), so confirm the current schema.
- Things to confirm and record in §11: rate limits, whether cancel-on-disconnect exists, the current fee formula (taker fee is believed to be roughly `ceil(0.07 × contracts × P × (1−P))`, highest near 50¢; **put fees in a single function**), maker fees, position netting rules when selling YES vs. buying NO, and any order-delay rule for sports.
- Market discovery: find a game's win market by searching events/markets. The UI needs a search box ("Duke", "Clemson") that resolves to a market ticker and shows which team is "Team A" (the YES side).

---

## 6. The trade lifecycle (the core of the system)

One active market and one position at a time (v1). Every trade follows this state machine:

```
IDLE --tap ▲/▼--> ENTERING --filled--> WAITING --repriced or wait timeout--> EXITING_PASSIVE --deadline--> EXITING_FORCED --> IDLE
                     |                    \______________ FLATTEN / opposite tap / stop hit ______________/
                     \--no fill--> IDLE                                   (to EXITING_FORCED)
```

**Step 0 — Record the book (always on).** The server subscribes to the selected market's order book and keeps a rolling buffer of best bid/ask with timestamps (≥60 s). *Why:* when a tap arrives, we need the price from just **before** the play, which is the stale price we're trying to catch.

**Step 1 — ENTERING (buy).** On tap at server receive time `t_tap`:
- `anchor_ask` = best ask on the chosen side at `t_tap − lookback_ms` (default 1500 ms, configurable).
- `limit = min(anchor_ask + chase_cents, max_price)`; `max_price` defaults to 97¢.
- Send a limit buy that **fills immediately or cancels the rest** for `size` contracts.
- *Why:* anchoring to the pre-play price means that if the market has already caught up, we get **no fill instead of paying the new price**. Immediate-or-cancel means nothing is left on the book to be picked off later.
- No fill → back to IDLE (log it). Partial fill → continue with whatever filled.

**Step 2 — WAITING (let the market catch up).** Do nothing until:
- the reprice is detected: best bid on our side ≥ `entry_avg + min_move_cents`, **or**
- `wait_max_ms` has passed (default: half of `deadline_ms`).
- *Why:* the profit only exists after the book moves. Selling earlier means selling at the stale price.

**Step 3 — EXITING_PASSIVE (sell without paying the spread).** Place a resting sell at `max(best_ask − 1¢, best_bid + 1¢)`. Re-quote if the book moves (throttled). *Why:* a buyer at the new price pays us, so we don't give up the spread or pay taker fees.

**Step 4 — EXITING_FORCED (sell no matter what).** When `deadline_ms` (measured from fill) hits: cancel the passive order, then sell the remainder at best bid, immediately. Retry until flat; alert the phone if not flat within 3 s. *Why:* **never hold through a swing we haven't seen.** We bet on something we already watched happen; the next play is a coin flip.

**Overrides (any time during steps 2–4):**
- **Same-direction tap** → "it got even better, keep holding": cancel the passive order, reset the deadline, go back to WAITING. (v1: don't add size.)
- **Opposite-direction tap or FLATTEN** → go straight to EXITING_FORCED.
- **Auto-stop (optional, configurable):** if our side's best bid < `entry_avg − stop_cents`, go to EXITING_FORCED. Catches reversals Kevin doesn't react to in time.

### Edge cases the system must handle
| Situation | Required behavior |
|---|---|
| Market already caught up when tapped | No fill → IDLE. Log as "missed". |
| Partial fill | Manage only the filled amount. |
| Play reversed (flag/review/foul) | Kevin hits FLATTEN; the optional auto-stop is the backup. |
| Market never moves | Wait timeout → passive → forced sell at bid, small loss. |
| Market overshoots then reverts | Passive exit usually sells near the top. |
| Next swing while holding | Same tap = extend; opposite/FLATTEN = exit now. |
| Phone loses signal | Server finishes the exit by itself. Phone gets the current state when it reconnects. |
| Server loses the Kalshi WebSocket | Reconnect; if holding a position, cancel open orders and force-exit; alert. |
| Order rejected / market paused | Keep retrying the exit with backoff; alert the phone. |
| Double tap | Ignore triggers while in ENTERING; use `client_order_id` so a retry can't create a duplicate order. |
| Empty/very thin book at exit | Keep retrying the sell at best bid; alert. Size limits keep this small. |
| Game ends while holding | Settles at $0/$1. Log it. |
| Server restart | On startup, reconcile positions and open orders with Kalshi before accepting any taps. |

---

## 7. Configuration

Global and per-sport presets, editable from the phone (settings screen) and persisted on the server.

| Param | Meaning | Default |
|---|---|---|
| `size` | Contracts per tap | small (e.g. 10) |
| `chase_cents` | Max cents above the pre-play ask we'll pay | 5 |
| `lookback_ms` | How far before the tap to take the pre-play price | 1500 |
| `min_move_cents` | Bid gain that counts as "market caught up" | 3 |
| `wait_max_ms` | Max time in WAITING | deadline/2 |
| `deadline_ms` | Must be flat by this long after the fill | football 20000, baseball 10000, basketball 6000 |
| `stop_cents` | Auto-stop distance (0 = off) | 0 |
| `max_position` | Hard cap on contracts held | 3× size |
| `max_price` | Never buy above this | 97¢ |
| `daily_loss_limit` | Stop accepting taps once hit | e.g. $20 |

Sport presets only change `deadline_ms` (and maybe `wait_max_ms`). **The sport is never otherwise special-cased.**

---

## 8. Phone UI requirements

- Huge **▲ (Team A good)** and **▼ (Team A bad)** buttons labeled with team names/colors. A **FLATTEN** button that's large but hard to hit by accident (e.g., long-press or separated placement).
- A haptic buzz on tap and on fill.
- Always visible: market name, current bid/ask, position and average price, live P&L, current state (IDLE/WAITING/...), time until deadline, and latency of the last tap (phone send → server receive → order acknowledged).
- Game picker: search → choose market → choose which team is "A".
- A **MARK button (paper mode)**: logs a tap timestamp and direction *without trading*. Use it to collect evidence before risking money (see §9).
- Screen stays awake while the app is open (Wake Lock API). Readable in sunlight (high contrast).
- Connection indicator; automatically reconnects.

---

## 9. Logging and measurement (required, not optional)

Every tap and every order produces structured logs (JSONL or SQLite): phone tap time (client clock + server receive time), anchor price, limit, Kalshi acknowledgement time, fills (price/size/time), exit path taken (passive/forced/flatten/stop), fees, and P&L per trade.

Also store **book snapshots from −30 s to +60 s around every tap/mark**, plus a recorder mode that logs the whole game. This allows:
- **Paper mode:** simulate "would I have filled at the anchor + chase price, and what would I have sold for?" from the recorded book.
- **Repricing speed:** how long market makers take to update after real events, per sport and per game.
- Tuning `lookback_ms`, `chase_cents`, `min_move_cents`, `deadline_ms`.

Provide a small analysis script/notebook: fill rate, edge per contract after fees, P&L distribution, time to reprice, and the exit-path breakdown.

### Stop criteria (Kevin decides; these are the proposed defaults)
After ≥30 live fills: stop or rethink if **median net edge per contract after fees ≤ 0**, if **fill rate < ~15–20%** with no improvement from tuning, or if logs show Kalshi applying a systematic delay to our orders.

---

## 10. Milestones (in order; don't skip ahead)

0. **Verify the ground.** Demo API keys. Confirm order schema, immediate-or-cancel semantics, fees, rate limits, cancel-on-disconnect, sell/netting behavior, and **whether any sports order-delay rule exists today**. Write the findings into §11.
1. **Market data recorder.** Search markets, subscribe to the book, keep the rolling buffer, record to disk. Test on a live game (no trading).
2. **Execution engine + state machine** against **demo**, with unit tests for every transition and every edge case in §6 (use a fake exchange/book simulator).
3. **Phone PWA** + server WebSocket + auth. Paper mode (MARK) end to end.
4. **Analysis tooling** on recorded games and paper marks.
5. **Live with minimal size**, daily loss limit on, at one game. Review the logs against the stop criteria before increasing anything.

Non-goals for v1: multiple simultaneous markets, scaling into positions, markets other than game winner (spreads/props/totals), any automated detection of plays (Kevin's eyes are the signal), and market making.

---

## 11. Verified facts log

Fill this in during Milestone 0 and keep it current. Include the date checked and the doc link.

- _(empty)_

---

## 12. Working conventions

- Python, typed, `asyncio` throughout; no blocking calls on the hot path. Measure and log hot-path latency.
- Secrets in environment variables / `.env` (gitignored). Never log keys or signatures.
- Fee calculation, price conversion (cents ↔ dollar strings), and side/action mapping each live in **one** place, with tests.
- Prefer small, readable modules: `kalshi_client`, `book`, `engine` (state machine), `api` (phone-facing), `store` (logging), `web/` (PWA), `analysis/`.
- Kevin prefers concise, direct communication. Flag assumptions explicitly; when unsure about exchange behavior, verify against docs or demo instead of guessing.
