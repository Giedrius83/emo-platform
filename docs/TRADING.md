# Trading pipeline: audit, design, operation

## 1. Audit (state before this change)

| Area | Finding |
|---|---|
| Trading pipeline | **Not in the repository.** No code placed, sized, or closed orders. The only eToro calls were five read-only GETs for the dashboard. Trading was done by the Grok Bot agents from natural-language instructions, outside version control. |
| Trading execution | Grok's Trader asked a human to approve each order in chat. There was no single controlled execution path, no confirmation of fills, and no handling of uncertain submits. |
| Risk controls | None enforced in code. The live account shows holds of 3–6 days (ETH 10→16 Sep, XRP 23→28 Sep, DOT) against a 15-minute intent, placeholder SL/TP (SL 0.0001, TP 0), and stops 8–10% below entry with take-profits of 0.5–2%. |
| Instrument discovery | None in code. The Grok bots picked coins by prompt. |
| Duplicate protection | None. The live account held two NEAR positions at once (16–17 Sep). |
| Dashboard | Read-only already. It had no trading controls and no order routes. The "Balance history" chart has been replaced by TRADING ASSETS. |
| Secrets | Clean: no keys in git, the frontend, or the built bundle. Keys lived only in `/opt/emo/.env` (mode 600). |
| Testing | 56 tests covered the dashboard; none covered trading. |
| Critical problems | Non-deterministic trading with no enforced limits; no idempotency; no restart recovery; positions held for days; stacked positions. |

**Found while checking the eToro API. This explains the wide stops:** eToro's minimum stop-loss for crypto is **10% of margin**. At 1× leverage that is 10% below entry (`minStopLossPercentage: 10.0` from `/trading/info/*/eligibility` for BTC, ETH, 1INCH). A 0.3–0.5% stop **cannot be placed at the broker**. See §3.4 for how the pipeline handles it.

## 2. Architecture

```
            ┌───────────── emo-trader (own process, own key file) ─────────────┐
 eToro  ◄──►│ broker.py  (the ONLY module with order routes; env fixed at start)│
 Public API │    ▲                                                               │
            │ MANAGER ── SCOUT → QUANT → GUARD → TRADER   (direct calls, no waits)│
            │    │            strict JSON Message per handoff                     │
            │    └─► SQLite (WAL): signals, pipeline_events, orders, positions,   │
            │        risk_events, bot_health, execution_metrics, system_state     │
            └──────────────────────────────┬────────────────────────────────────┘
                                           │ opened read-only (mode=ro)
            emo-dashboard (FastAPI) ───────┘──► browser (no trading controls)
```

* **One execution path:** `Trader.execute()` → `Broker.open_by_amount()`, and `Trader.close()` → `Broker.close_position()`. A test fails if any other module references an order route.
* **The dashboard cannot trade:** it opens the database with `mode=ro`, holds no trading keys (those live in `/opt/emo/trading.env`, loaded only by `emo-trader`), and exposes no write route except bot reports (`/api/ingest`).
* **Nothing outside the five stages is on the path.** ORKA, Planner, Coder, Overwatch, the dashboard and reporting never delay or veto a trade.

### Stages

| Stage | Does | Output |
|---|---|---|
| SCOUT | Ranks the whole eligible crypto universe from one rates call, then scores the top `CANDIDATES_PER_SCAN` on 1m candles. | Ranked `SIGNAL`s, or `NO_SIGNAL` |
| QUANT | Checks a fresh realtime quote, spread ≤ `MAX_SPREAD_PERCENT`, fresh 1m and 5m candles, and that 1m and 5m agree in direction. | `APPROVED` / `REJECTED` / `INVALID` |
| GUARD | Applies halts, eligibility, duplicates, open-position count, daily loss, loss streak, trades per day and exposure, using eToro's current cash and equity. Sizes the trade and sets TP/SL deterministically. | `APPROVED` + size and levels / `REJECTED` / `RISK_BLOCK` / `INVALID` |
| TRADER | Execution only (see §3). | `EXECUTED` / `FAILED` / `REJECTED` |
| MANAGER | Runs the loops: verifies the environment, discovers the universe, reconciles with eToro, applies halts, publishes state. | — |

`REJECTED` is a bad candidate: the Manager hands straight on to the next ranked signal (up to 3 per scan). `INVALID` is a data or protocol failure: three in a row trigger an emergency halt.

## 3. Execution, duplicates, lifecycle

### 3.1 Entry, in order
1. The approval must come from GUARD. The database must have the signal as `APPROVED`, within `SIGNAL_TTL_SECONDS`, with no emergency halt.
2. A fresh quote: realtime, not stale, spread within the limit, and entry slippage within `MAX_ENTRY_SLIPPAGE_PERCENT` of Guard's reference price.
3. Position and order rows are inserted in one transaction. The database **refuses** a second order for the signal and a second live position on the instrument.
4. The rows are marked `SUBMITTED`, then **one** POST is sent with the stored `x-request-id`.
5. After a timeout, 5xx or 429 the POST is **never resent**. The trader finds out whether eToro has the order. It looks for a position the trader does not know, on the same instrument and side, with an amount within fees of the request, opened after the submit. It then confirms that position's order id. If found, the trade proceeds. If not, the trade is marked failed and an **emergency halt** follows (a late appearance is caught by reconciliation as well).
6. The fill is confirmed through `orders:lookup?orderId=`. eToro's copy of the position must then carry TP and SL, or the position is closed at once as `SYSTEM_FAILURE`.

### 3.2 Identifiers
`signal_id` → `execution_id` (with a unique `request_id` = `x-request-id`) → eToro `order_id` → eToro `position_id`. All four are unique in the database. Every journal line carries the `signal_id`.

### 3.3 Lifecycle
`CREATED → SUBMITTED → OPEN → MONITORING → CLOSED`. Any other move raises `LifecycleError`. Close reasons: `TP, SL, TIMEOUT, EMERGENCY, MANUAL, BROKER_REJECT, SYSTEM_FAILURE`.

### 3.4 Exits
| Exit | Where it is enforced |
|---|---|
| Take-profit 0.4–0.8% | **At eToro** (no minimum) and by the monitor |
| Stop-loss 0.3–0.5% | **By the monitor** (a market close), because eToro rejects stops under 10% |
| Backstop stop | **At eToro**, at eToro's minimum distance + `BROKER_SL_BUFFER_PERCENT` (≈11%). This is crash protection for when the trader is down. |
| `MAX_HOLD_SECONDS` (900) | By the monitor, whether or not a price is available |

If the trader is offline, an open position is protected only by eToro's TP and the ~11% backstop until the trader restarts. On restart it closes anything past its deadline immediately.

### 3.5 Restart and reconciliation
On start, and on every account refresh:
* each unresolved order is looked up by its reference and resolved (filled → monitored, rejected or not found → closed);
* each position that vanished from eToro is finalised from trade history (TP, SL or MANUAL, with eToro's net profit);
* **any eToro position the trader did not open halts entries** (`POSITION_MISMATCH`).

### 3.6 Verified on the eToro DEMO account (2026-09-28)
One $10 BTC round trip was placed with the trader's exact request bodies, with the owner's approval:

* Open `by-amount` with TP 83335.71 and backstop SL 73873.29 was **accepted as sent**. Filled at 82900.5 (order 384481669, position 3605462366).
* The portfolio row carried both protections (`isNoStopLoss: false`, `isNoTakeProfit: false`).
* The close was **accepted**. `close-orders/{id}` reported rate 82923.5, and the trade appeared in history. The cost was about $0.10 of virtual money in fees.

It exposed two differences from the published spec, both handled and covered by tests using the captured payloads:

* `GET /trading/info/demo/orders/{id}` returns a flat shape (`statusID`, `positions[]`), not the documented one. Fills are therefore confirmed through `orders:lookup?orderId=`, which matches the spec, and `normalize_order` accepts both.
* `orders:lookup?referenceId=<x-request-id>` returns 404 for these orders (their `referenceID` is all zeros). Uncertain submits are therefore resolved from the portfolio (§3.1, step 5).

Also observed: history's `minDate` excludes the given day, so the trader queries two days back.

## 4. Risk states

| State | Cause | Effect | Clears |
|---|---|---|---|
| `ENTRY_HALTED` | `MAX_DAILY_LOSS_USD`, `MAX_CONSECUTIVE_LOSSES`, `MAX_TRADE_COUNT_PER_DAY` (UTC day) | No new entries; open trades are still managed | Next UTC day |
| `EMERGENCY_HALT` | Auth failure, repeated API failures (per endpoint), stale market feed, clock skew, uncertain submit, unconfirmed fill, position mismatch, repeated invalid data, DB failure, environment mismatch | No new entries; open trades are still managed (closed at once if `EMERGENCY_CLOSE_POSITIONS=true`) | Only `emo-trading recover "what you checked"`; it refuses while orders are unresolved |
| `SYSTEM_OFFLINE` | The trader has not written a heartbeat for 90 s | Shown by the dashboard | Restart the trader |

## 5. Configuration

The trader reads its settings from the environment (`/opt/emo/trading.env`). Invalid values stop the trader.

| Key | Default | |
|---|---|---|
| `TRADING_ENV` | `demo` | `real` also needs `TRADING_REAL_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` |
| `TRADING_PIPELINE_ENABLED` | `false` | Explicit opt-in |
| `ETORO_TRADING_API_KEY`, `ETORO_TRADING_USER_KEY` | — | Separate from the dashboard's read-only keys |
| `MAX_SPREAD_PERCENT` | 0.20 | |
| `MAX_MARKET_DATA_AGE_SECONDS` | 15 | Maximum quote age |
| `MAX_CANDLE_AGE_SECONDS` | 150 | Latest 1m candle; 5m allows +300 s |
| `MAX_POSITION_PERCENT` | 25 | Of eToro equity |
| `MAX_POSITION_USD` | 0 (off) | Hard cap in dollars |
| `TP_PERCENT_MIN` / `MAX` | 0.4 / 0.8 | |
| `SL_PERCENT_MIN` / `MAX` | 0.3 / 0.5 | |
| `MAX_HOLD_SECONDS` | 900 | |
| `MAX_ENTRY_SLIPPAGE_PERCENT` | 0.15 | |
| `BROKER_SL_BUFFER_PERCENT` | 1.0 | Added to eToro's minimum stop distance |
| `LEVERAGE` | 1 | Must be allowed by eligibility |
| `ALLOW_SHORT` | false | |
| `MAX_DAILY_LOSS_USD` | 25 | |
| `MAX_CONSECUTIVE_LOSSES` | 3 | |
| `MAX_OPEN_POSITIONS` | 2 | |
| `MAX_TOTAL_EXPOSURE_PERCENT` | 50 | |
| `MAX_TRADE_COUNT_PER_DAY` | 20 | |
| `API_FAILURE_HALT_COUNT` | 5 | Consecutive failures of one endpoint |
| `ORDER_CONFIRM_TIMEOUT_SECONDS` | 30 | |
| `SIGNAL_TTL_SECONDS` | 20 | |
| `EMERGENCY_CLOSE_POSITIONS` | false | |
| `SCAN_INTERVAL_SECONDS` | 30 | |
| `MONITOR_INTERVAL_SECONDS` | 2 | |
| `UNIVERSE_REFRESH_SECONDS` | 3600 | |
| `CANDIDATES_PER_SCAN` | 6 | |
| `MIN_MOMENTUM_5M_PERCENT` | 0.05 | |
| `RANK_WEIGHT_MOMENTUM_5M` / `_1M` / `_SPREAD` / `_VOLATILITY` | 1.0 / 0.5 / 2.0 / 0.25 | Ranking factors |

**Universe:** `GET /api/v1/market-data/search?internalAssetClassId=10` (crypto; verified to return 664 instruments). It keeps instruments that are tradable, buy-enabled, not delisted, not hidden and active, and whose eligibility allows open and close at the configured leverage. It is refreshed hourly. There is no symbol whitelist.

**Data eToro does not provide:** crypto candle `volume` is `null`. The dashboard shows `DATA_UNAVAILABLE` and ranking does not use volume.

## 6. Operating on DEMO

On the server (after the usual deploy):

```bash
emo-trading setup      # paste a DEMO key with WRITE permission; starts the trader on DEMO
emo-trading status     # halt state, live positions, unresolved orders
emo-trading logs       # recent log lines (IDs, never keys)
emo-trading stop       # stop; open positions keep eToro TP + backstop SL
emo-trading recover "checked positions in eToro"   # clear an emergency halt
```

On start the trader prints `TRADING ENVIRONMENT: DEMO`. It then checks the key's scopes through `/api/v1/me` (it must have DEMO write) and reads the DEMO portfolio. If either check fails, it halts before any order.

## 7. Before REAL money (not enabled)

1. **Stop the Grok bots from trading the real account.** Remove their eToro write access, or delete the key they use. If they keep trading, the trader sees their positions as `POSITION_MISMATCH` and halts. That is by design: two traders on one account would duplicate and conflict.
2. Create a REAL key with WRITE permission. Put it in `trading.env` with `TRADING_ENV=real` and `TRADING_REAL_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`.
3. Set `MAX_POSITION_USD` and `MAX_DAILY_LOSS_USD` to amounts you accept losing. The real account holds about $41, and eToro's minimum is $10, so `MAX_POSITION_PERCENT` 25 allows one ~$10 position.
4. Understand the stop-loss gap (§3.4): the 0.3–0.5% stop is a software stop. If the server or eToro API is down, only the ~11% backstop and TP protect the position.
5. Run DEMO for a meaningful period first and review `emo-trading status` and the dashboard journal.
