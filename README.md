# Lighter RH – Entropy fixed four-leg scalping

[中文说明与完整配置](README.zh-CN.md)

Forked from [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb), with sequential virtual-maker triggering based on [hxx344/perp](https://github.com/hxx344/perp/blob/76d8e8e620ca65a0af9c90163a93b0d53f319468/strategies/aster_lighter_cycle.py).

Every cycle independently chooses long or short with equal probability. LEG1 and LEG3 are local virtual limit orders driven exclusively by Lighter Robinhood public books. Only LEG2 and LEG4 send real orders on Entropy (`io` on Hyperliquid).

| Leg | Long cycle | Short cycle |
| --- | --- | --- |
| LEG1: RH virtual entry | Virtual sell above ask; wait for bid >= limit | Virtual buy below bid; wait for ask <= limit |
| LEG2: Entropy entry | IOC buy to open long | IOC sell to open short |
| LEG3: RH virtual exit | After confirmed entry, virtual buy below current bid | After confirmed entry, virtual sell above current ask |
| LEG4: Entropy exit | IOC reduce-only sell | IOC reduce-only buy |

Virtual limits use `virtual_offset_bps` beyond RH BBO, rounded outward to RH price precision. Each price stays fixed until touched or its re-quote timeout expires. A new RH book update must cross the limit; reconnects and sequence gaps invalidate the old virtual order. Virtual fills model BBO touches, not queue priority or traded volume. Entropy IOC limits use Entropy's own BBO and the configured slippage cap. No premium, fee, or profit threshold gates the exit.

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

`--symbol` must exist on both exchanges; IDs and precision are resolved dynamically. `--hedge lighter-rh` is optional and fixed. Old `thresholds`, `inventory`, and `hedge` configuration blocks are rejected; use the new example.

Configure `cycle.direction` (`random`, `long`, `short`), `cycle.cycles` (0 = continuous), `cycle.virtual_offset_bps` (default 2), `cycle.virtual_requote_sec` (default 3), and `sizing.quantity` (>0 = fixed base size; 0 = default $50 notional). Sizes round down using Entropy precision only. `cycle.max_hold_sec` defaults to 0 (wait for LEG3); a positive value closes via LEG4 at the holding deadline.

## Settlement and restart behavior

Start flat with no open orders on the selected Entropy market. Use one process per account/market and do not concurrently trade that position. After every real order, the engine verifies the account position before advancing. A partial LEG2 fill becomes the cycle quantity; LEG4 retries only its remaining quantity with `reduce_only`. Closing residuals below the normal minimum notional is attempted; exchange rejection after bounded retries halts the cycle.

Ambiguous responses are polled by unique `cloid`; unresolved outcomes halt without resubmission. `logs/cycle.json` is written before each send and prevents restarting an unfinished cycle. Reconcile terminal orders and residual positions, then archive the checkpoint before restarting. Its adjacent process lock prevents two processes using the same state file.

Ctrl+C / SIGTERM stops waiting LEG1 without opening a position. With a known position, it lets an in-flight submission settle and attempts bounded LEG4 closing while feeds remain running. Unknown orders, mismatched positions or an unclosed residual cause a nonzero exit and retain the checkpoint. Forced termination cannot flatten.

`logs/legs.csv` records virtual and real leg events. `logs/minutes.csv` retains the original public-price schema; `hedge_*` means RH reference prices. `tools/analyze.py` provides descriptive statistics only. Virtual legs do not hedge the real Entropy position; cycle PnL depends on its actual fills, fees and funding.

## Tests

```bash
python -X utf8 -m pip install pytest
python -X utf8 -m pytest -q
```

Tests simulate all submissions, including official SDK signing, IOC/reduce-only wire fields, full long/short cycles, partial fills, uncertain settlement, re-quotes, feed gaps, stopping, and durable state/locking.

API references: [Hyperliquid exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint), [Lighter RH WebSocket](https://apidocs.rh.lighter.xyz/docs/websocket). Upstream MIT license retained.
