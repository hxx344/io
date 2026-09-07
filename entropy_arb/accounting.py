"""Read-only session accounting from confirmed order IDs and exchange ledger data."""
from __future__ import annotations

import time
from decimal import Decimal


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite account value")
    return result


class SessionAccount:
    def __init__(self):
        self.started_ms = int(time.time() * 1000)
        self.pending = {}
        self.missing_order_id = False
        self.revision = 0
        self.snapshot_revision = -1
        self.snapshot = None
        self.updated_at = 0.0
        self.error = ""
        self.realized = self.fees = self.funding = Decimal(0)
        self.funding_cursor = self.started_ms
        self.funding_seen = set()

    def record_fill(self, oid, qty, since_ms):
        if qty <= 0:
            return
        self.revision += 1
        if oid is None:
            self.missing_order_id = True
        else:
            self.pending[str(oid)] = (number(qty), max(0, since_ms - 60_000))

    @property
    def total_pnl(self):
        if (self.snapshot is None or self.error or self.pending or self.missing_order_id
                or self.snapshot_revision != self.revision):
            return None
        return self.realized - self.fees + self.funding + self.snapshot["unrealized"]

    async def refresh(self, venue, expected_position):
        revision = self.revision
        pending = dict(self.pending)
        if pending:
            fills = await venue.fetch_history("userFillsByTime", min(v[1] for v in pending.values()))
            grouped, seen = {}, set()
            for fill in fills:
                oid = str(fill.get("oid"))
                if fill.get("coin") != venue.coin or oid not in pending:
                    continue
                key = (oid, fill["tid"])
                if key in seen:
                    continue
                seen.add(key)
                if fill["feeToken"] != "USDC":
                    raise ValueError("Cannot value non-USDC trading fee")
                qty, pnl, fee = grouped.get(oid, (Decimal(0),) * 3)
                size = number(fill["sz"])
                if size <= 0:
                    raise ValueError("Invalid accounting fill size")
                grouped[oid] = (qty + size, pnl + number(fill["closedPnl"]), fee + number(fill["fee"]))
            for oid, (qty, pnl, fee) in grouped.items():
                if abs(qty - pending[oid][0]) <= Decimal(10) ** -venue.size_decimals * Decimal("0.000001"):
                    self.realized += pnl
                    self.fees += fee  # fee already includes builderFee
                    del self.pending[oid]
                elif qty > pending[oid][0]:
                    raise ValueError("Accounting fill quantity exceeds confirmed order")
        end_ms = int(time.time() * 1000)
        funding = await venue.fetch_history("userFunding", self.funding_cursor, end_ms)
        for row in funding:
            delta = row["delta"]
            if delta.get("coin") != venue.coin or row["time"] < self.started_ms:
                continue
            key = (row["time"], row["hash"], delta["coin"])
            if key not in self.funding_seen:
                self.funding += number(delta["usdc"])
                self.funding_seen.add(key)
        # Overlap catches delayed ledger publication without double counting.
        self.funding_cursor = max(self.started_ms, end_ms - 60_000)
        snapshot = await venue.fetch_account_snapshot()
        if snapshot is None:
            raise ValueError("No account address configured")
        self.snapshot = snapshot
        self.updated_at = time.monotonic()
        if self.revision != revision:
            raise ValueError("Orders changed during account refresh; waiting for next snapshot")
        if abs(snapshot["position"] - number(expected_position)) >= Decimal(10) ** -venue.size_decimals / 2:
            raise ValueError("Account position not yet synchronized")
        self.snapshot_revision = revision
        self.error = ""
