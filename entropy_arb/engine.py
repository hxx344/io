"""Sequential four-leg scalping: virtual RH -> real Entropy -> virtual RH -> close."""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import random
import time
from collections import deque
from pathlib import Path

import aiohttp

from .book import floor_step
from .config import Config
from .journal import CycleJournal
from .recorder import MinuteRecorder
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue

log = logging.getLogger("engine")
CSV_HEADER = ["ts", "cycle", "direction", "leg", "venue", "side", "virtual",
              "requested_qty", "filled_qty", "limit_px", "avg_px", "status", "reason"]


class Engine:
    def __init__(self, cfg: Config, record_only=False):
        self.cfg, self.record_only = cfg, record_only
        self.entropy = self.hedge = self.session = None
        self.venues = {}
        self.stop = asyncio.Event()
        self._feed_stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self.markets_ready = False
        self.halted = False
        self.halt_reason = ""
        self.state = "IDLE"
        self.direction = ""
        self.cycle = self.completed_cycles = 0
        self.remaining = 0.0
        self.virtual_order = None
        self.recent_trades = deque(maxlen=50)
        self._sends = deque()
        self._limited_until = 0.0
        self._rng = random.SystemRandom()
        self._journal = CycleJournal(cfg.state_file)
        self._step = 0.0001
        self._last_reconcile = 0.0
        self._exposure_since = 0.0
        self._market_references = {}
        self.recorder = None

    def request_stop(self):
        self.stop.set()
        self._update_evt.set()

    def _save(self):
        self._journal.save({"state": self.state, "cycle": self.cycle,
                            "symbol": self.cfg.symbol, "direction": self.direction,
                            "remaining": self.remaining, "reason": self.halt_reason,
                            "updated_at": time.time()})

    def _halt(self, reason):
        self.halted, self.halt_reason, self.state = True, reason, "HALTED"
        self._save()
        log.error("HALTED: %s; confirmed remaining=%g", reason, self.remaining)
        raise RuntimeError(reason)

    async def run(self):
        tasks = []
        self.session = aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        try:
            if not self.record_only:
                if not self.cfg.creds_complete:
                    raise RuntimeError("Entropy HL_PRIVATE_KEY required; RH needs no credentials")
                self._journal.acquire()
                self._journal.check_clean()
            self.entropy = HLVenue(self.cfg.entropy, self.cfg.hl_api_url,
                                   self.cfg.hl_ws_url, self.session, self.cfg.settle_timeout_sec)
            self.hedge = LighterVenue(self.cfg.hedge, self.session)
            self.venues = {"entropy": self.entropy, "hedge": self.hedge}
            await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())
            self._step = 10 ** -self.entropy.size_decimals
            self.markets_ready = True
            if not self.record_only:
                self.entropy.init_signer()
                await self._confirm_position(0.0)
                if await self.entropy.fetch_open_orders():
                    self._halt("Entropy has pre-existing open orders for this symbol")
                self._save()
            for venue in self.venues.values():
                tasks += venue.start_tasks(self._feed_stop, self._update_evt.set, live=False)
            if self.cfg.recorder_enabled or self.record_only:
                self.recorder = MinuteRecorder(self.cfg.recorder_csv, self.entropy.book,
                                               self.hedge.book, self.cfg.staleness_sec)
                tasks.append(asyncio.create_task(self.recorder.run(self._feed_stop)))
            tasks.append(asyncio.create_task(self._status_loop()))
            if self.record_only:
                log.info("RECORD-ONLY: public feeds, no strategy/orders")
                await self.stop.wait()
            else:
                tasks.append(asyncio.create_task(self._keepalive_loop()))
                # Single coroutine owns all sends and position reads; no concurrent hedge/reconcile.
                await self._strategy_loop()
        finally:
            self._feed_stop.set()
            self.request_stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for venue in self.venues.values():
                await venue.close()
            await self.session.close()
            self._journal.close()

    async def _pulse(self, delay=0.1):
        try:
            await asyncio.wait_for(self._update_evt.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self._update_evt.clear()

    def _fresh(self, venue):
        book = venue.book
        if not book.is_fresh(self.cfg.staleness_sec):
            return False
        bid, ask = book.best_bid(), book.best_ask()
        return all(math.isfinite(p) and p > 0 for p in (bid, ask)) and bid < ask

    async def _confirm_position(self, expected):
        deadline = time.monotonic() + self.cfg.settle_timeout_sec
        last = "unavailable"
        while True:
            try:
                position = await self.entropy.fetch_position()
                last = str(position)
                if math.isfinite(position) and abs(position - expected) < self._step / 2:
                    self.entropy.position = position
                    self._last_reconcile = time.monotonic()
                    return
            except Exception as exc:
                last = str(exc)
            if time.monotonic() >= deadline:
                self._halt(f"Entropy position mismatch: expected {expected:g}, observed {last}")
            await asyncio.sleep(min(0.25, self.cfg.settle_timeout_sec))

    def _signed_remaining(self):
        return self.remaining if self.direction == "long" else -self.remaining

    async def _check_position_due(self):
        if time.monotonic() - self._last_reconcile >= self.cfg.reconcile_sec:
            await self._confirm_position(self._signed_remaining())

    def _record(self, leg, side, virtual, qty, filled, limit, avg, status, reason=""):
        row = dict(zip(CSV_HEADER, [time.time(), self.cycle, self.direction, leg,
                       "lighter-rh" if virtual else "entropy", side, virtual,
                       qty, filled, limit, avg, status, reason]))
        self.recent_trades.append(row)
        path = Path(self.cfg.trades_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
            if header:
                writer.writeheader()
            writer.writerow(row)
        log.info("cycle=%d %s %s %s filled=%g/%g limit=%g status=%s %s",
                 self.cycle, leg, "VIRTUAL" if virtual else "ENTROPY", side,
                 filled, qty, limit, status, reason)

    async def _wait_virtual(self, leg, is_buy, qty):
        self.state = leg
        self._save()
        self.virtual_order = None
        while not self.stop.is_set():
            await self._check_position_due()
            if leg == "LEG3" and self.cfg.max_hold_sec > 0:
                if time.monotonic() - self._exposure_since >= self.cfg.max_hold_sec:
                    self.virtual_order = None
                    return False
            if not self._fresh(self.hedge) or not self._fresh(self.entropy):
                self.virtual_order = None
                await self._pulse()
                continue
            book = self.hedge.book
            now = time.monotonic()
            if self.virtual_order is not None:
                order = self.virtual_order
                if book.generation != order["generation"]:
                    self.virtual_order = None
                    continue
                # Only a later RH book update can fill a newly armed virtual order.
                crossed = (book.best_ask() <= order["price"] if is_buy
                           else book.best_bid() >= order["price"])
                if now < order["expires"] and book.revision > order["revision"] and crossed:
                    avg = book.best_ask() if is_buy else book.best_bid()
                    self._record(leg, "buy" if is_buy else "sell", True, order["qty"], order["qty"],
                                 order["price"], avg, "VIRTUAL_FILLED")
                    self.virtual_order = None
                    return True
                if now < order["expires"]:
                    await self._pulse()
                    continue
            # Use the actual Nth price level, not a BBO percentage offset.
            levels = book.sorted_bids() if is_buy else book.sorted_asks()
            levels = [(px, size) for px, size in levels
                      if math.isfinite(px) and px > 0 and math.isfinite(size) and size > 0]
            if len(levels) < self.cfg.virtual_depth:
                self.virtual_order = None
                await self._pulse()
                continue
            price = levels[self.cfg.virtual_depth - 1][0]
            self.virtual_order = {"leg": leg, "side": "buy" if is_buy else "sell",
                                  "price": price, "revision": book.revision,
                                  "generation": book.generation,
                                  "depth": self.cfg.virtual_depth,
                                  "qty": qty or self._entry_quantity(self.direction == "long"),
                                  "expires": now + self.cfg.virtual_requote_sec}
            log.info("cycle=%d %s RH virtual %s depth=%d qty=%g limit=%g", self.cycle, leg,
                     self.virtual_order["side"], self.cfg.virtual_depth,
                     self.virtual_order["qty"], price)
            await self._pulse()
        self.virtual_order = None
        return False

    def _slippage(self, leg):
        override = self.cfg.leg2_slippage_bps if leg == "LEG2" else self.cfg.leg4_slippage_bps
        return self.cfg.leg_slippage_bps if override is None else override

    def _market_reference(self, is_buy):
        return self.entropy.book.best_ask() if is_buy else self.entropy.book.best_bid()

    def _limit(self, is_buy, reference_px=None, leg="LEG2"):
        reference_px = self._market_reference(is_buy) if reference_px is None else reference_px
        return self.entropy.market_limit(is_buy, reference_px, self._slippage(leg))

    def _entry_quantity(self, is_buy, reference_px=None):
        price = self._limit(is_buy, reference_px)
        if price <= 0:
            self._halt("Entropy order limit is not positive")
        risk_price = max(price, self.entropy.book.best_ask())
        requested = self.cfg.quantity or self.cfg.order_notional / risk_price
        qty = floor_step(requested, self._step)
        notional = qty * price
        if qty < max(self._step, self.entropy.min_base):
            self._halt("Configured entry quantity rounds below Entropy minimum")
        if notional < max(self.cfg.min_order_notional, self.entropy.min_quote):
            self._halt("Configured entry notional is below Entropy minimum")
        if qty * risk_price > min(self.cfg.max_order_notional, self.cfg.entropy.cap_usd) + 1e-8:
            self._halt("Configured entry exceeds Entropy order/position cap")
        return qty

    async def _wait_send_ready(self, closing):
        # Exit may proceed without RH, including after a stop request.
        deadline = time.monotonic() + max(self.cfg.staleness_sec, 65.0)
        while True:
            if not closing and self.stop.is_set():
                return False
            now = time.monotonic()
            while self._sends and now - self._sends[0] >= 60:
                self._sends.popleft()
            if (self._fresh(self.entropy) and (closing or self._fresh(self.hedge))
                    and len(self._sends) < self.cfg.entropy.orders_per_min
                    and now >= self._limited_until):
                return True
            if now >= deadline:
                self._halt("Timed out waiting for fresh Entropy book/order budget")
            await self._pulse()

    async def _send(self, leg, is_buy, qty, closing, reference_px, reason=""):
        # Checkpoint precedes submission: a crash cannot restart an uncertain order.
        self.state = leg + "_PENDING"
        self._save()
        self._sends.append(time.monotonic())
        limit = self._limit(is_buy, reference_px, leg)
        try:
            result = await self.entropy.send_market(is_buy=is_buy, qty=qty,
                reference_px=reference_px, slippage_bps=self._slippage(leg), reduce_only=closing)
        except Exception as exc:
            self._halt(f"{leg} submission outcome unknown: {exc}")
        if result.get("unresolved"):
            self._halt(f"{leg} order outcome unresolved: {result.get('status')} {result.get('err')}")
        filled = float(result.get("filled_base", 0))
        epsilon = self._step * 1e-6
        if (not math.isfinite(filled) or filled < 0 or filled > qty + epsilon
                or abs(filled - floor_step(filled, self._step)) > epsilon):
            self._halt(f"{leg} invalid filled quantity: {filled}")
        self.remaining = round(self.remaining - filled if closing else filled, 12)
        self.state = leg
        self._save()
        await self._confirm_position(self._signed_remaining())
        self._record(leg, "buy" if is_buy else "sell", False, qty, filled,
                     limit, result.get("avg_px"), result.get("status", "unknown"), reason)
        if "RATE_LIMITED" in str(result.get("err", "")):
            self._limited_until = time.monotonic() + self.cfg.rate_limit_pause_sec
        if result.get("err"):
            log.warning("%s: %s", leg, result["err"])
        return filled

    async def _open_position(self):
        is_buy = self.direction == "long"
        reference_px = self._market_references.get("LEG2")
        for attempt in range(self.cfg.max_order_attempts):
            if not await self._wait_send_ready(closing=False):
                return False
            await self._confirm_position(0.0)
            if not await self._wait_send_ready(closing=False):
                return False
            # Size is recomputed at the executable Entropy price after the virtual trigger.
            if reference_px is None:
                reference_px = self._market_reference(is_buy)
                self._market_references["LEG2"] = reference_px
            qty = self._entry_quantity(is_buy, reference_px)
            filled = await self._send("LEG2", is_buy, qty, closing=False, reference_px=reference_px)
            if filled > 0:
                self._exposure_since = time.monotonic()
                return True  # partial entry accepted, never top up blindly
            if attempt + 1 < self.cfg.max_order_attempts:
                await self._delay(self.cfg.retry_delay_sec)
        self._halt("LEG2 exhausted attempts without a fill")

    async def _close_position(self, reason="trigger"):
        reference_px = self._market_references.get("LEG4")
        self.virtual_order = None
        self.state = "LEG4"
        self._save()
        for attempt in range(self.cfg.max_order_attempts):
            if self.remaining < self._step / 2:
                await self._confirm_position(0.0)
                return
            await self._wait_send_ready(closing=True)
            await self._confirm_position(self._signed_remaining())
            await self._wait_send_ready(closing=True)
            qty = floor_step(self.remaining, self._step)
            if qty < self._step:
                self._halt("Uncloseable Entropy residual")
            if reference_px is None:
                reference_px = self._market_reference(self.direction == "short")
                self._market_references["LEG4"] = reference_px
            await self._send("LEG4", self.direction == "short", qty, closing=True,
                             reference_px=reference_px, reason=reason)
            if self.remaining < self._step / 2:
                return
            if attempt + 1 < self.cfg.max_order_attempts:
                # Shutdown still waits between bounded close attempts.
                await asyncio.sleep(self.cfg.retry_delay_sec)
        self._halt("LEG4 exhausted attempts; residual position remains")

    async def _delay(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _strategy_loop(self):
        try:
            while not self.stop.is_set():
                if self.cfg.cycles and self.completed_cycles >= self.cfg.cycles:
                    break
                await self._confirm_position(0.0)
                self.cycle += 1
                self._market_references.clear()
                self.direction = (self._rng.choice(("long", "short"))
                                  if self.cfg.direction == "random" else self.cfg.direction)
                self.state = "LEG1"
                self._save()
                # Long = virtual sell above market -> Entropy buy; short mirrors it.
                if not await self._wait_virtual("LEG1", self.direction == "short", self.cfg.quantity):
                    break
                if not await self._open_position():
                    break
                triggered = await self._wait_virtual("LEG3", self.direction == "long", self.remaining)
                reason = "trigger" if triggered else "shutdown" if self.stop.is_set() else "max_hold"
                await self._close_position(reason)
                self.completed_cycles += 1
                self.state = "IDLE"
                self._save()
                await self._delay(self.cfg.cooldown_sec)
        finally:
            # Unknown submissions / mismatched positions cannot be closed by guessing.
            if self.remaining > 0 and not self.halted and not self.state.endswith("_PENDING"):
                await self._close_position("shutdown")
            if not self.halted and self.remaining == 0 and not self.state.endswith("_PENDING"):
                self.state = "IDLE"
                self._save()
            self.request_stop()

    async def _keepalive_loop(self):
        while not self._feed_stop.is_set():
            await self.entropy.warm_http()
            await asyncio.sleep(self.cfg.http_keepalive_sec)

    async def _status_loop(self):
        while not self._feed_stop.is_set():
            log.info("4LEG state=%s cycle=%d completed=%d direction=%s Entropy remaining=%g",
                     self.state, self.cycle, self.completed_cycles, self.direction, self.remaining)
            await asyncio.sleep(self.cfg.status_interval_sec)
