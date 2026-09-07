from __future__ import annotations
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from entropy_arb.book import OrderBook
from entropy_arb.config import Config, VenueConf
from entropy_arb.engine import Engine
from entropy_arb.venue_hl import HLVenue


class FakeVenue:
    def __init__(self, key):
        self.key, self.name = key, key
        self.book = OrderBook()
        self.position = 0.0
        self.size_decimals = 3
        self.min_base = 0.001
        self.min_quote = 10
        self.calls = []
        self.responses = []
        self.actual = 0.0
        self.position_override = None
        self.set_book(99.9, 100.1)

    def set_book(self, bid, ask):
        self.book.apply_hl([
            [{"px": str(round(bid - i * .1, 8)), "sz": "10"} for i in range(6)],
            [{"px": str(round(ask + i * .1, 8)), "sz": "10"} for i in range(6)]])

    def px_round(self, price, round_up):
        import math
        return (math.ceil(price * 100) if round_up else math.floor(price * 100)) / 100

    async def fetch_position(self):
        return self.actual if self.position_override is None else self.position_override

    market_limit = HLVenue.market_limit

    async def send_market(self, **order):
        order["limit_px"] = self.market_limit(order["is_buy"], order["reference_px"], order["slippage_bps"])
        assert self.key == "entropy", "RH must never receive a real order"
        self.calls.append(order)
        result = self.responses.pop(0) if self.responses else {
            "filled_base": order["qty"], "avg_px": 100, "status": "filled", "unresolved": False}
        if isinstance(result, Exception):
            raise result
        filled = result.get("filled_base", 0)
        self.actual += filled if order["is_buy"] else -filled
        return result


@pytest.fixture
def eng(tmp_path):
    cfg = Config("TEST", VenueConf("entropy", "hl", "ENTROPY", "TEST"),
                 VenueConf("hedge", "lighter", "RH", "TEST"), quantity=1,
                 cycles=1, cooldown_sec=0, max_spread_bps=25, settle_timeout_sec=0.005,
                 retry_delay_sec=0.001, state_file=str(tmp_path / "state.json"),
                 trades_csv=str(tmp_path / "legs.csv"))
    e = Engine(cfg)
    e.entropy, e.hedge = FakeVenue("entropy"), FakeVenue("hedge")
    e.venues = {"entropy": e.entropy}
    e._step = 0.001
    e._last_reconcile = __import__("time").monotonic()
    return e


async def until(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


def test_partial_entry_and_close_only_remaining(eng):
    async def scenario():
        eng.direction = "long"
        eng.entropy.responses = [
            dict(status="filled", filled_base=.6),
            dict(status="filled", filled_base=.2),
            dict(status="filled", filled_base=.4)]
        assert await eng._open_position()
        assert eng.remaining == .6
        await eng._close_position()
        assert [o["qty"] for o in eng.entropy.calls] == [1, .6, .4]
        assert all(o["reduce_only"] for o in eng.entropy.calls[1:])
        assert eng.remaining == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [dict(status="timeout", unresolved=True), TimeoutError("lost response")])
def test_unknown_outcome_halts_without_retry_or_close(eng, response):
    async def scenario():
        eng.direction = "long"
        eng.entropy.responses = [response]
        with pytest.raises(RuntimeError, match="unknown|unresolved"):
            await eng._open_position()
        assert len(eng.entropy.calls) == 1 and eng.halted
        with pytest.raises(RuntimeError, match="Unfinished"):
            eng._journal.check_clean()
    asyncio.run(scenario())


def test_unknown_close_does_not_resend(eng):
    async def scenario():
        eng.direction = "long"
        await eng._open_position()
        eng.entropy.responses = [dict(status="timeout", unresolved=True)]
        with pytest.raises(RuntimeError):
            await eng._close_position()
        assert len(eng.entropy.calls) == 2 and eng.remaining == 1
    asyncio.run(scenario())


def test_zero_fill_retries_are_bounded(eng):
    async def scenario():
        eng.direction = "short"
        eng.entropy.responses = [dict(status="canceled", filled_base=0)] * 3
        with pytest.raises(RuntimeError, match="LEG2 exhausted"):
            await eng._open_position()
        assert len(eng.entropy.calls) == 3
        assert eng.remaining == 0
    asyncio.run(scenario())


def test_residual_blocks_next_cycle(eng):
    async def scenario():
        eng.direction = "short"
        await eng._open_position()
        eng.entropy.responses = [dict(status="canceled", filled_base=0)] * 3
        with pytest.raises(RuntimeError, match="residual"):
            await eng._close_position()
        assert eng.halted and eng.remaining == 1
        assert all(o["reduce_only"] and o["is_buy"] for o in eng.entropy.calls[1:])
    asyncio.run(scenario())


def test_position_mismatch_halts_before_entry(eng):
    async def scenario():
        eng.entropy.actual = .2
        with pytest.raises(RuntimeError, match="position mismatch"):
            await eng._strategy_loop()
        assert eng.entropy.calls == []
    asyncio.run(scenario())


def test_budget_and_rate_limit_pause_prevent_immediate_send(eng):
    async def scenario():
        import time
        eng.direction = "long"
        eng.cfg.entropy.orders_per_min = 1
        eng._sends.append(time.monotonic())
        task = asyncio.create_task(eng._open_position())
        await asyncio.sleep(.02)
        assert not eng.entropy.calls
        eng.request_stop()
        assert not await task
    asyncio.run(scenario())


def test_size_uses_only_entropy_grid_and_caps(eng):
    eng.cfg.quantity = 0
    qty = eng._entry_quantity(True)
    assert qty == .498
    eng.cfg.quantity = 6
    with pytest.raises(RuntimeError, match="cap"):
        eng._entry_quantity(True)


def test_journal_process_lock_and_recovery(eng):
    from entropy_arb.journal import CycleJournal
    eng._journal.acquire()
    try:
        second = CycleJournal(eng.cfg.state_file)
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
        eng.state = "LEG2_PENDING"
        eng._save()
        with pytest.raises(RuntimeError, match="Unfinished"):
            eng._journal.check_clean()
        eng.state = "IDLE"
        eng._save()
        eng._journal.check_clean()
    finally:
        eng._journal.close()
    second.acquire()
    second.close()


@pytest.mark.parametrize("filled", [float("nan"), 1.001, .0005])
def test_invalid_fill_quantity_halts(eng, filled):
    async def scenario():
        eng.direction = "long"
        eng.entropy.responses = [dict(status="filled", filled_base=filled)]
        with pytest.raises(RuntimeError, match="invalid filled quantity"):
            await eng._open_position()
        assert len(eng.entropy.calls) == 1
        assert eng.halted
    asyncio.run(scenario())


@pytest.mark.parametrize("direction", ["long", "short"])
def test_both_market_legs_use_separate_slippage_and_fixed_reference_on_retry(eng, direction):
    async def scenario():
        eng.direction = direction
        eng.cfg.leg2_slippage_bps, eng.cfg.leg4_slippage_bps = 10, 30
        original_send = eng.entropy.send_market
        async def send(**order):
            result = await original_send(**order)
            # Simulate the quote moving between attempts.
            bid = eng.entropy.book.best_bid() + 1
            eng.entropy.set_book(bid, bid + .2)
            return result
        eng.entropy.send_market = send
        eng.entropy.responses = [dict(status="canceled", filled_base=0),
                                 dict(status="filled", filled_base=1),
                                 dict(status="filled", filled_base=.4),
                                 dict(status="filled", filled_base=.6)]
        assert await eng._open_position()
        await eng._close_position()
        calls = eng.entropy.calls
        assert len(calls) == 4
        assert [c["slippage_bps"] for c in calls] == [10, 10, 30, 30]
        assert [c["reduce_only"] for c in calls] == [False, False, True, True]
        for first, retry in ((calls[0], calls[1]), (calls[2], calls[3])):
            assert first["reference_px"] == retry["reference_px"]
            assert first["limit_px"] == retry["limit_px"]
        assert calls[0]["reference_px"] != calls[2]["reference_px"]
        assert eng.remaining == 0
    asyncio.run(scenario())

def test_cleanup_after_recording_error_preserves_exit_slippage_anchor(eng):
    async def scenario():
        eng.direction = "long"
        await eng._open_position()
        eng.entropy.responses = [dict(status="filled", filled_base=.4),
                                 dict(status="filled", filled_base=.6)]
        original_record = eng._record
        def broken_record(*args, **kwargs):
            raise OSError("test log write failed")
        eng._record = broken_record
        with pytest.raises(OSError):
            await eng._close_position()
        eng._record = original_record
        eng.entropy.set_book(98, 98.2)
        await eng._close_position("shutdown")
        assert eng.entropy.calls[1]["reference_px"] == eng.entropy.calls[2]["reference_px"]
        assert eng.entropy.calls[1]["limit_px"] == eng.entropy.calls[2]["limit_px"]
        assert eng.remaining == 0
    asyncio.run(scenario())

@pytest.mark.parametrize("direction", ["long", "short"])
def test_turnover_sums_actual_partial_entry_and_exit_fills(eng, direction):
    from decimal import Decimal
    async def scenario():
        eng.direction = direction
        eng.entropy.responses = [
            dict(status="filled", filled_base=.6, avg_px=100.2),
            dict(status="filled", filled_base=.2, avg_px=101.1),
            dict(status="filled", filled_base=.4, avg_px=101.2)]
        await eng._open_position()
        await eng._close_position()
        assert eng.turnover == {"LEG2": Decimal("60.12"), "LEG4": Decimal("60.70")}
        assert eng.total_turnover == Decimal("120.82")
        assert not any(eng.unpriced_filled.values())
        assert [row["filled_notional"] for row in eng.recent_trades] == [
            Decimal("60.12"), Decimal("20.22"), Decimal("40.48")]
        import json
        state = json.loads(eng._journal.path.read_text(encoding="utf-8"))
        assert Decimal(state["session_turnover"]["LEG4"]) == Decimal("60.70")
    asyncio.run(scenario())


def test_virtual_legs_and_zero_fill_retries_do_not_add_turnover(eng):
    from decimal import Decimal
    async def scenario():
        eng.direction = "long"
        eng._record("LEG1", "sell", True, 500, 500, 999, 999, "VIRTUAL_FILLED")
        eng._record("LEG3", "buy", True, 500, 500, 888, 888, "VIRTUAL_FILLED")
        eng.entropy.responses = [dict(status="canceled", filled_base=0),
                                 dict(status="filled", filled_base=.5, avg_px=100)]
        await eng._open_position()
        assert eng.total_turnover == Decimal("50.0")
        assert eng.turnover["LEG4"] == 0
        assert not any(eng.unpriced_filled.values())
    asyncio.run(scenario())


def test_missing_execution_price_does_not_use_limit_as_turnover(eng):
    async def scenario():
        eng.direction = "long"
        eng.entropy.responses = [dict(status="filled", filled_base=1, avg_px=None)]
        await eng._open_position()
        assert eng.total_turnover == 0
        assert eng.unpriced_filled["LEG2"] == 1
        assert eng.recent_trades[-1]["filled_notional"] is None
        assert eng.remaining == 1 and not eng.halted
    asyncio.run(scenario())


def test_confirmed_turnover_survives_exit_position_check_failure(eng):
    async def scenario():
        eng.direction = "long"
        await eng._open_position()
        original_send = eng.entropy.send_market
        async def send(**order):
            result = await original_send(**order)
            eng.entropy.position_override = .7
            return result
        eng.entropy.send_market = send
        with pytest.raises(RuntimeError, match="position mismatch"):
            await eng._close_position()
        assert eng.turnover["LEG2"] == 100 and eng.total_turnover == 200
    asyncio.run(scenario())


def test_csv_failure_does_not_lose_or_duplicate_turnover(eng):
    async def scenario():
        eng.direction = "long"
        original_record = eng._record
        def failed_record(*args, **kwargs):
            raise OSError("test disk error")
        eng._record = failed_record
        with pytest.raises(OSError):
            await eng._open_position()
        assert eng.total_turnover == 100
        eng._record = original_record
        await eng._close_position("shutdown")
        assert eng.turnover["LEG2"] == 100 and eng.turnover["LEG4"] == 100
        assert eng.total_turnover == 200
    asyncio.run(scenario())
