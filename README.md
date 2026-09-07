# Lighter RH – Entropy fixed four-leg scalping

[中文说明与完整配置](README.zh-CN.md)

Forked from [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb), with sequential virtual-maker triggering based on [hxx344/perp](https://github.com/hxx344/perp/blob/76d8e8e620ca65a0af9c90163a93b0d53f319468/strategies/aster_lighter_cycle.py).

Every cycle independently chooses long or short with equal probability. LEG1 and LEG3 are local virtual limit orders driven exclusively by Lighter Robinhood public books. Only LEG2 and LEG4 execute real slippage-protected market orders on Entropy (`io` on Hyperliquid).

| Leg | Long cycle | Short cycle |
| --- | --- | --- |
| LEG1: RH virtual entry | Virtual sell at RH ask level 4; wait for bid >= limit | Virtual buy at RH bid level 4; wait for ask <= limit |
| LEG2: Entropy entry | Market buy to open long | Market sell to open short |
| LEG3: RH virtual exit | After confirmed entry, virtual buy at current RH bid level 4 | After confirmed entry, virtual sell at current RH ask level 4 |
| LEG4: Entropy exit | Market reduce-only sell | Market reduce-only buy |

Virtual limits use the actual Nth RH bid (buy) or ask (sell), controlled by `cycle.virtual_depth` (default 4, one-based). No percentage offset is added. If that side has fewer than N valid price levels, the strategy waits instead of falling back to BBO. Each price stays fixed until touched or its re-quote timeout expires. A new RH book update must cross the limit; reconnects and sequence gaps invalidate the old virtual order. Virtual fills model BBO touches, not queue priority or traded volume.

Entropy market execution follows [Hyperliquid's official market_open/market_close wire format](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/exchange.py): aggressive IOC orders with price protection; any unfilled remainder is canceled and never rests on the book. `execution.leg2_slippage_bps` and `execution.leg4_slippage_bps` independently cap entry/exit slippage. Each leg captures Entropy's ask for buys or bid for sells immediately before its first submission. Buy protection is at most `ask * (1 + bps/10000)`; sell protection is at least `bid * (1 - bps/10000)`, rounded inside the cap. Retries retain the same reference and protection price; the next leg gets a new reference. A strict slippage cap can leave an order unfilled or partially filled, with the existing bounded retry/halt handling. No premium, fee, or profit threshold gates the exit.

## Run

Python 3.10+:

```bash
python -m venv .venv
source .venv/bin/activate  # PowerShell: .\.venv\Scripts\Activate.ps1
python -X utf8 -m pip install -r requirements-live.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Fill only `HL_PRIVATE_KEY` and `HL_ACCOUNT_ADDRESS`. With an API agent, use the agent private key and main account address. RH requires no credentials or signing SDK. HTTP and WebSocket connections support environment proxy settings.

```bash
# Public data only, no strategy or orders:
python main.py --symbol SNDK --record-only
# Live trading:
python main.py --symbol SNDK
# Chinese dashboard / plain logs:
python main.py --symbol SNDK --cn
python main.py --symbol SNDK --no-dashboard
```

`--symbol` must exist on both exchanges; IDs and precision are resolved dynamically. `--hedge lighter-rh` is optional and fixed. Old `thresholds`, `inventory`, and `hedge` configuration blocks are rejected; use the new example. Replace the previous `cycle.virtual_offset_bps` with `cycle.virtual_depth: 4`.

Configure `cycle.direction` (`random`, `long`, `short`), `cycle.cycles` (0 = continuous), `cycle.virtual_depth` (default 4), `cycle.virtual_requote_sec` (default 3), and `sizing.quantity` (>0 = fixed base size; 0 = default $50 notional). Sizes round down using Entropy precision only. `cycle.max_hold_sec` defaults to 0 (wait for LEG3); a positive value closes via LEG4 at the holding deadline.

For example, use bid/ask level 6 with 0.10% entry slippage and 0.15% exit slippage:

```yaml
cycle:
  virtual_depth: 6
execution:
  leg2_slippage_bps: 10
  leg4_slippage_bps: 15
```

Both leg-specific slippage settings default to 20 bps (0.20%). When an override is absent, the legacy `execution.leg_slippage_bps` supplies its fallback. Restart after editing configuration.

## Settlement and restart behavior

Start flat with no open orders on the selected Entropy market. Use one process per account/market and do not concurrently trade that position. After every real order, the engine verifies the account position before advancing. A partial LEG2 fill becomes the cycle quantity; LEG4 retries only its remaining quantity with `reduce_only`. Closing residuals below the normal minimum notional is attempted; exchange rejection after bounded retries halts the cycle.

Ambiguous responses are polled by unique `cloid`; unresolved outcomes halt without resubmission. `logs/cycle.json` is written before each send and prevents restarting an unfinished cycle. Reconcile terminal orders and residual positions, then archive the checkpoint before restarting. Its adjacent process lock prevents two processes using the same state file.

Ctrl+C / SIGTERM stops waiting LEG1 without opening a position. With a known position, it lets an in-flight submission settle and attempts bounded LEG4 closing while feeds remain running. Unknown orders, mismatched positions or an unclosed residual cause a nonzero exit and retain the checkpoint. Forced termination cannot flatten.

`logs/legs.csv` records virtual and real leg events. `logs/minutes.csv` retains the original public-price schema; `hedge_*` means RH reference prices. `tools/analyze.py` provides descriptive statistics only. Virtual legs do not hedge the real Entropy position; cycle PnL depends on its actual fills, fees and funding.

## LEG2 / LEG4 turnover

The dashboard, per-fill logs, periodic status logs and shutdown summary show LEG2 turnover, LEG4 turnover, and their sum. Each real fill contributes `actual filled quantity * actual average execution price`. Both entry and exit count positively; partial fills and closing retries contribute only their actual fills. Virtual legs and unfilled orders contribute nothing. This is gross trading turnover before fees/funding, not PnL: a $50 entry plus a $51 exit totals $101.

Counters accumulate across cycles **within the current process** and reset on restart. The cycle checkpoint retains a session snapshot, and `logs/legs.csv` retains fill quantities and prices. Orders recovered via order-status polling try to recover VWAP from their actual trade history. Missing prices produce an explicit incomplete-total indicator with the unpriced filled quantities; limit/protection prices are never substituted.

## Balance and session strategy PnL

The dashboard shows Entropy balance, equity, withdrawable funds, and session net PnL: `realized PnL - actual fees + signed funding + unrealized PnL`. PnL accumulates across cycles and resets on restart. Only this process's LEG2/LEG4 order IDs and the selected symbol contribute; virtual legs and deposits/withdrawals do not. Actual fill fees already include builder fees; configured fee estimates are not used.

Standard accounts show the `io USDC` margin balance. Unified/portfolio-margin accounts show `shared USDC`, excluding other collateral assets; spot holds are not presented as withdrawable margin. Read-only polling defaults to 10 seconds (`logging.account_refresh_sec`). Delayed fills, missing order IDs or unsynchronized positions show unavailable/synchronizing PnL. Failed refreshes retain the previous balance with a stale label. The dashboard remains open through shutdown closing and the final refresh. Record-only mode can show balance with `HL_ACCOUNT_ADDRESS`, but has no strategy PnL. Sources: [account/funding API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [unified balances](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot).

## Tests

```bash
python -X utf8 -m pip install pytest
python -X utf8 -m pytest -q
```

Tests simulate all submissions, including official SDK signing, IOC/reduce-only wire fields, bid/ask depth selection, insufficient depth, per-leg slippage caps across retries, full long/short cycles, partial fills, uncertain settlement, re-quotes, feed gaps, stopping, and durable state/locking.

API references: [Hyperliquid exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint), [Lighter RH WebSocket](https://apidocs.rh.lighter.xyz/docs/websocket). Upstream MIT license retained.
