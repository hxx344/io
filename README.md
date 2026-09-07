# Entropy spread-gated entry and timed exit

[中文说明](README.zh-CN.md)

The strategy only connects to Entropy (`io` on Hyperliquid), with no RH connection or virtual LEG1/LEG3 orders. Each cycle independently chooses long/short with equal probability, submits LEG2 market entry when the spread qualifies, then closes through LEG4 reduce-only market execution.

`spread bps = (ask - bid) / ((ask + bid) / 2) * 10000`. The default gate is **<= 2 bps**. Stale, invalid or crossed books block entry; the gate is rechecked after awaited position checks and before retries. Exit ignores this gate.

**50ms starts when the LEG2 attempt that fills is submitted.** An early fill acknowledgement waits until the deadline; a late acknowledgement closes immediately without another 50ms delay. Quantity must be confirmed before closing. No extra position REST request sits between the legs; each exit reconciles remaining position, and the next entry verifies flatness. Network latency, stale books, limits, persistence and scheduling can delay sending; this is a target submission interval, not an exchange execution-time guarantee.

Both legs retain separate slippage caps and protected IOC market execution. Retries retain each leg's first price anchor. Partial entry accepts only filled quantity; exit retries only the remainder. Entry reserves one order slot for exit (`entropy.max_orders_per_min >= 2`).

## Configuration

Edit `config.yaml`, then restart:

```yaml
cycle:
  direction: random
  cycles: 0
  max_spread_bps: 2.0
  close_delay_ms: 50.0
sizing:
  quantity: 0
  order_notional_usd: 50
execution:
  leg2_slippage_bps: 20
  leg4_slippage_bps: 20
  cooldown_sec: 2
```


## Run

Python 3.10+. Install `requirements-live.txt`, copy `config.example.yaml` to `config.yaml` and `.env.example` to `.env`. Set `HL_PRIVATE_KEY` to the API agent key and `HL_ACCOUNT_ADDRESS` to the funded main account address. A symbol only needs to exist on Entropy.

```bash
python main.py --symbol SNDK --record-only
python main.py --symbol SNDK --cn
python main.py --symbol SNDK --no-dashboard
```

Direction is fixed to random. Legacy `virtual_depth`, `virtual_requote_sec` and `max_hold_sec` are accepted with an ignored-setting warning; remove them. Legacy `--hedge lighter-rh` does not create a connection. `execution.leg_slippage_bps` remains the fallback cap. HTTP and WebSocket connections support environment proxies.

## Settlement and stopping

Start flat with no open orders on the selected market, using one process per account/market without concurrent manual trading. Ambiguous submissions are queried by unique cloid and halt if unresolved. A durable checkpoint precedes every send; unfinished checkpoints block restart pending reconciliation. The state file has a process lock.

Ctrl+C / SIGTERM stops waiting entries; known open positions interrupt the timer and close with bounded retries. Unknown orders, mismatched positions and unclosed residuals halt with an unfinished checkpoint. Forced process termination cannot flatten.

`logs/legs.csv` appends only LEG2/LEG4 records; `limit_px` is the market protection price. `logs/minutes.csv` keeps its header with Entropy BBO and empty hedge/premium fields. `tools/analyze.py` summarizes minute-close Entropy spreads, not cycle PnL.

## LEG2 / LEG4 turnover

The dashboard, per-fill logs, periodic status logs and shutdown summary show LEG2 turnover, LEG4 turnover, and their sum. Each real fill contributes `actual filled quantity * actual average execution price`. Both entry and exit count positively; partial fills and closing retries contribute only their actual fills. Unfilled orders contribute nothing. This is gross trading turnover before fees/funding, not PnL: a $50 entry plus a $51 exit totals $101.

Counters accumulate across cycles **within the current process** and reset on restart. The cycle checkpoint retains a session snapshot, and `logs/legs.csv` retains fill quantities and prices. Orders recovered via order-status polling try to recover VWAP from their actual trade history. Missing prices produce an explicit incomplete-total indicator with the unpriced filled quantities; limit/protection prices are never substituted.

## Balance and session strategy PnL

The dashboard shows Entropy balance, equity, withdrawable funds, and session net PnL: `realized PnL - actual fees + signed funding + unrealized PnL`. PnL accumulates across cycles and resets on restart. Only this process's LEG2/LEG4 order IDs and the selected symbol contribute; deposits/withdrawals do not. Actual fill fees already include builder fees; configured fee estimates are not used.

Standard accounts show the `io USDC` margin balance. Unified/portfolio-margin accounts show `shared USDC`, excluding other collateral assets; spot holds are not presented as withdrawable margin. Read-only polling defaults to 10 seconds (`logging.account_refresh_sec`). Delayed fills, missing order IDs or unsynchronized positions show unavailable/synchronizing PnL. Failed refreshes retain the previous balance with a stale label. The dashboard remains open through shutdown closing and the final refresh. Record-only mode can show balance with `HL_ACCOUNT_ADDRESS`, but has no strategy PnL. Sources: [account/funding API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [unified balances](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot).



## Tests

`python -X utf8 -m pytest -q`

Tests cover spread boundaries, stale/invalid books, rechecks, random directions, timing and slow acknowledgements, partial fills, spread-independent exits, reserved order budget, shutdown, settlement, slippage, accounting and startup without RH. All order transport is simulated.

[Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint). Forked from [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb); upstream MIT license retained.
