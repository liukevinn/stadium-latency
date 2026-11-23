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

### Kalshi integration (details and sources in §11)
- Hosts are in `src/courtside/config.py`. **All development and testing happens on demo first.**
- Auth: signed headers, built by `kalshi/auth.py`. Sign at send time.
- Orders: **V2 only**, `POST /portfolio/events/orders`. Direction is quoted on the YES leg (`side: bid|ask`). **"Buy NO at p" = `ask` at YES price `1−p`.** `kalshi/sides.py` is the only code that does this mapping.
- Prices and counts on the wire are strings (`"0.5600"`, `"10.00"`). `kalshi/units.py` is the only code that converts them.
- Fees: `fees.py` is the only fee function. Its coefficients are unverified; Kalshi's per-fill `fee_cost`/`average_fee_paid` is the source of truth.
- Market discovery: **there is no text search endpoint.** The UI's "Duke"/"Clemson" search has to filter a cached list from `GET /events?series_ticker=<game series>&status=open&with_nested_markets=true` on our side. The team for each market is in `yes_sub_title`.

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
Tags: **[docs]** = read in the docs; **[probe]** = needs confirming with `scripts/probe.py` (results go in `data/probe/`); **[unverified]** = we couldn't get a source.

### Environments (checked 2026-11-23) [docs] — [api_environments](https://docs.kalshi.com/getting_started/api_environments)
- REST prod `https://external-api.kalshi.com/trade-api/v2` (recommended; `api.elections.kalshi.com` still works). Demo `https://external-api.demo.kalshi.co/trade-api/v2` (`demo-api.kalshi.co` still works).
- WS prod `wss://external-api-ws.kalshi.com/trade-api/ws/v2`; demo `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`.
- **Separate lower-latency WS host for `fill` and `user_orders` only**: `wss://user-ws.kalshi.com/...` (demo `user-ws.demo.kalshi.co`). Other channels on it return error 8.
- Demo and prod API keys are separate.

### Auth (2026-11-23) [docs] — [api_keys](https://docs.kalshi.com/getting_started/api_keys)
- Message = `timestamp_ms + METHOD + path`. The path starts at `/trade-api/...` and has **no query string**. WS handshake signs `GET /trade-api/ws/v2`.
- RSA-PSS/SHA-256/MGF1-SHA-256/salt = digest length, **or Ed25519 (recommended)**. Both are supported in `auth.py`.
- Timestamp must be within **30 s** of Kalshi's clock, or you get 401 `header_timestamp_expired`. Keep NTP on.

### Orders (2026-11-23) [docs] — [create-order-v2](https://docs.kalshi.com/api-reference/orders/create-order-v2), [order_direction](https://docs.kalshi.com/getting_started/order_direction), [order_errors](https://docs.kalshi.com/getting_started/order_errors)
- **The legacy `POST/DELETE /portfolio/orders` writes return 410 `deprecated_v1_order_endpoint`.** Use the V2 endpoints:
  - create `POST /portfolio/events/orders`
  - cancel `DELETE /portfolio/events/orders/{id}?market_ticker=`
  - cancel all `DELETE /portfolio/events/orders` (204)
  - batch `.../batched`
  - amend `.../{id}/amend`
  - decrease `.../{id}/decrease`
  - Reads are unchanged: `GET /portfolio/orders[/{id}]`.
- Create body:
  - Required: `ticker`, `side` (`bid`|`ask`, YES leg), `price` (dollar string, e.g. `"0.5600"`), `count` (fixed-point string), `time_in_force` (`immediate_or_cancel`|`fill_or_kill`|`good_till_canceled`), `self_trade_prevention_type` (`taker_at_cross`|`maker`).
  - Optional: `client_order_id` (≤64 chars), `expiration_time` (unix **seconds**; not with IOC), `post_only` (GTC only), `reduce_only` (**IOC only**; capped to your position), `cancel_order_on_pause`, `order_group_id`, `subaccount`.
- Direction: `bid` = buy YES ≡ sell NO; `ask` = sell YES ≡ buy NO.
- Response (201 means accepted, not filled): `order_id`, `fill_count`, `remaining_count`, `average_fill_price`, `average_fee_paid`, `ts_ms`. **An IOC fill is known from the ack itself; no second round trip is needed.**
- Errors to handle:
  - `order_already_exists` (409, duplicate `client_order_id`; this is how retries stay safe)
  - `fill_or_kill_insufficient_resting_volume` (409)
  - `trading_is_paused` (409; cancels still work)
  - `exchange_is_paused` (409; cancels also fail)
  - `market_inactive`
  - `invalid_price` (off the market's price grid)
  - `insufficient_balance`
- **[probe, demo 2026-11-23, `KXNBAGAME-26NOV23OKCIND-IND`]**
  - IOC that misses → 201 with `fill_count "0.00"`, `remaining_count "0.00"`. **"No fill" = `fill_count == 0`.**
  - IOC that crosses fills at the **resting price**, not our limit. A reduce-only sell IOC with a 1¢ limit filled at the 36¢ bid. So a forced exit can use an aggressive limit safely.
  - Reusing a `client_order_id`, even after the first order was IOC-cancelled → 409 `order_already_exists`. Retrying with the same coid is safe.
  - FOK larger than the book → 409 `fill_or_kill_insufficient_resting_volume`. `reduce_only` + GTC → 400 `invalid_order`.
  - `ask` from flat **opens NO** (position_fp −1). `reduce_only` bid for 3 against −1 filled 1 → flat. `reduce_only` when flat fills 0, with no error.
  - **`GET /portfolio/orders/{id}` returns 404 for a cancelled order with no fills** (both IOC misses and cancelled GTC orders). Don't use it as a status check; use the ack and the `user_orders`/`fill` WS.
  - **REST positions can lag fills.** One read right after a fill still showed the old position. The engine tracks position from acks and fills, and reconciles with REST only when IDLE or at startup.
  - Latency from a home Mac to demo: REST round trip median 88 ms (min 75). Order round trip 80–90 ms warm, 150–340 ms on a cold connection, so keep the connection warm. Cancel takes about 80 ms. Ack `ts_ms` minus send time ≈ the round trip, so clock skew is negligible.
  - Order groups work on demo (`POST /portfolio/order_groups/create` → `order_group_id`).

### Positions and netting (2026-11-23) [docs] — [get-positions](https://docs.kalshi.com/api-reference/portfolio/get-positions), [help: buying yes vs selling no](https://help.kalshi.com/en/articles/13823806-buying-yes-vs-selling-no)
- `market_positions[].position_fp` is **signed**: + = long YES, − = long NO. Kalshi: "our backend treats [buying YES and selling NO] as essentially the same thing", so a position is one signed number per market. Exiting a ▼ (NO) trade = `bid` on YES.
- Other fields: `market_exposure_dollars` (cost basis, no fees), `realized_pnl_dollars`, `fees_paid_dollars`, `netting_enabled`.
- Subaccount "collateral return"/netting setting is **locked per event when the first order is placed**. With the default, buying and selling both sides on demo worked with no issues [probe].

### Market data (2026-11-23) [docs] — [orderbook-updates](https://docs.kalshi.com/websockets/orderbook-updates), [get-market-orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- The book is **bids only**: `yes_dollars_fp` and `no_dollars_fp` as `[price, size]` strings, sorted ascending (best bid last). YES ask = 1 − best NO bid.
- WS `orderbook_snapshot`, then `orderbook_delta` (`price_dollars`, `delta_fp`, `side`, `ts_ms`). A delta caused by our own order includes `client_order_id`.
- `seq` is per subscription and must be contiguous. On a gap, send `update_subscription` with action `get_snapshot`. A repeated subscribe alone does **not** resync.
- `use_yes_price: true` puts NO-side levels in YES pricing. The default is false and is planned to flip; set it explicitly.
- Server sends a ping after 10 s with no data. The server drops slow readers after 15 s of backlog (close 1008).
- Market fields: `yes_bid_dollars`, `yes_ask_dollars`, sizes, `yes_sub_title` (team), `status`, `price_ranges` (`{start, end, step}`). `units.parse_price` raises on sub-cent prices. [probe] NFL/NCAAF/NBA/NHL/NCAAMB game markets on demo are `price_level_structure: linear_cent`, step 0.01.
- **Book sizes are fractional** (e.g. `"1853.84"`). Store sizes as Decimal.
- Demo has live-looking game-winner books (e.g. NBA 36/37¢ with about 450 contracts each side). The `orderbook_snapshot` WS message arrived about 100 ms after subscribing.
- Game-winner series tickers follow `KX<LEAGUE>GAME`: `KXNFLGAME`, `KXNCAAFGAME`, `KXNBAGAME`, `KXNHLGAME`, `KXNCAAMBGAME`, … Each event (game) has 2 markets, one per team; `yes_sub_title` is the team name.
- **No text search.** Discovery goes through `GET /series?category=Sports`, `GET /search/filters_by_sport`, `GET /events?series_ticker=…&with_nested_markets=true`.

### Rate limits (2026-11-23) [docs] — [rate_limits](https://docs.kalshi.com/getting_started/rate_limits)
- Token buckets, separate for read and write. Default cost is 10 tokens per request; per-endpoint costs are at `GET /account/endpoint_costs`.
- Basic tier: 200 read / 100 write tokens per second, i.e. about **10 order creates per second**. Cancel costs 2. [probe] Our demo account is `basic` (read capacity 600 / refill 200; write 100/100). The balance endpoint has its own 10-token bucket (1 call per second).
- Advanced tier (300/300) needs one call to `POST /account/api_usage_level/upgrade` after ≥1 API order.
- A 429 has no Retry-After header; back off. `X-RateLimit-*` headers are on every response.
- Not a constraint at our volume, but a FLATTEN retry loop must not spin.

### Cancel-on-disconnect (2026-11-23) [docs] — [order_groups](https://docs.kalshi.com/getting_started/order_groups)
- **No cancel-on-disconnect exists.** The server must handle disconnects itself (CLAUDE.md §6 already requires this).
- **Order groups** offer a related protection. A group has a contract limit over a rolling 15 s window; when it trips, all of the group's resting orders are cancelled and new orders are rejected until it's reset. This is a cheap exchange-side guard against a runaway loop: put every order in a group with limit ≈ 2×`max_position`.
- `cancel_order_on_pause` cancels resting orders when trading pauses.

### Fees (2026-11-23)
- [docs] Per series: `fee_type` ∈ `quadratic` | `quadratic_with_maker_fees` | `quadratic_with_combo_maker_fees` | `flat`, plus `fee_multiplier`. Events can override both. ([get-series](https://docs.kalshi.com/api-reference/market/get-series))
- [docs] Combo maker fees use a 0.5 maker multiplier instead of 0.25.
- [docs] Fees are computed per fill and rounded **up to $0.000001**, not to the cent per order. ([fee_rounding](https://docs.kalshi.com/getting_started/fee_rounding))
- [docs] Cancels are free. Settlement fee is 0 for yes/no markets.
- **[probe, demo] Taker = `0.07 × C × P(1−P)` confirmed.** The amount charged = the model fee **ceiled to $0.0001** per fill (the account balance precision; docs say non-direct members are aligned to $0.01).
  - 1 contract at 37¢: model $0.016317 → charged $0.0164.
  - 3 contracts at 37¢: model $0.048951 → $0.0490; `average_fee_paid` $0.0163 is **per contract**.
  - The 6 probe fills sum to $0.1626, which equals `fees_paid_dollars` exactly.
- [probe] Sports series fee settings: 4,238 of 4,243 are `quadratic ×1` (**no maker fees** → the passive exit costs nothing in fees). 5 are `quadratic ×0.5`. All `KX*GAME` series checked are `quadratic ×1`.
- These are demo values. Before going live, re-run `probe.py --readonly` against prod, and check fee fields on the first live fills.
- [unverified] Maker fee multiplier (0.25×) is not exercised, since game series have no maker fees.
- Fee drag at 50¢ is about 1.75¢ per contract per side, **about 3.5¢ per round trip** when both legs are taker. So `min_move_cents` should be ≥ 4–5 to cover fees, or the exit should be passive.

### Sports order delay (2026-11-23) — **[unverified], highest-priority open question**
- Dec 2025 press: Kalshi filed with the CFTC to allow order-execution delays during live events, with no stated scope. A Kalshi spokesperson said it "is only a filing." Crypto.com has applied about a 3 s delay to non-market-maker sports orders. ([gambling911](https://gambling911.com/prediction-markets/kalshi-delay-implementation-on-order-executions-120925), [yogonet](https://www.yogonet.com/international/news/2025/12/11/116729-cryptocom-implements-a-threesecond-delay-for-retail-users-in-its-sports-eventbetting-market))
- Rule 13.1 amendment (CFTC filing of Nov 26 2025, effective Dec 11 2025): any delay on orders during live events **must be announced on the website/platform**. ([filing](https://www.cftc.gov/filings/orgrules/rules11262533628.pdf); full text not extracted)
- No source found confirming that a delay is live in 2026. No announcements endpoint exists in the API.
- **How to settle it:**
  1. Check Kalshi's site, rulebook and market rules for a live-event delay notice.
  2. On prod, at a live game, send a 1-contract IOC and compare ack `ts_ms` minus send time, and the time to our own `orderbook_delta`, against a non-sports market. A systematic extra delay of hundreds of ms or more = stop.

### Dev environment note
- The project lives on `~/Desktop`, and macOS keeps setting the `hidden` flag on files inside the project venv. Python 3.11 skips hidden `.pth` files, which breaks the editable install. Fix: the venv lives at `~/.venvs/courtside`, and `.venv` is a symlink to it.

### Old engine (github.com/liukevinn/k), reuse notes
- Reused: the RSA-PSS signing logic, now `kalshi/auth.py` with Ed25519 added.
- Not reused: its `Contexts/kalshi_api_reference.md` is stale (Bearer-token login, cents ints, the old `trading-api.kalshi.com` host). Its REST client passed the key *path* where PEM content was expected, and used the removed v1 order endpoints.

---

## 12. Working conventions

- Python, typed, `asyncio` throughout; no blocking calls on the hot path. Measure and log hot-path latency.
- Secrets in environment variables / `.env` (gitignored). Never log keys or signatures.
- Fee calculation, price conversion (cents ↔ dollar strings), and side/action mapping each live in **one** place, with tests.
- Prefer small, readable modules: `kalshi_client`, `book`, `engine` (state machine), `api` (phone-facing), `store` (logging), `web/` (PWA), `analysis/`.
- Kevin prefers concise, direct communication. Flag assumptions explicitly; when unsure about exchange behavior, verify against docs or demo instead of guessing.
