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
                 cycles=1, cooldown_sec=0, settle_timeout_sec=0.005,
                 retry_delay_sec=0.001, state_file=str(tmp_path / "state.json"),
                 trades_csv=str(tmp_path / "legs.csv"))
    e = Engine(cfg)
    e.entropy, e.hedge = FakeVenue("entropy"), FakeVenue("hedge")
    e.venues = {"entropy": e.entropy, "hedge": e.hedge}
    e._step = 0.001
    e._last_reconcile = __import__("time").monotonic()
    return e


async def until(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


async def touch(e):
    await until(lambda: e.virtual_order is not None)
    order = dict(e.virtual_order)
    price = order["price"]
    if order["side"] == "sell":
        e.hedge.set_book(price, price + 0.02)
    else:
        e.hedge.set_book(price - 0.02, price)
    e._update_evt.set()
    return order


def test_full_random_long_and_short_cycles(eng):
    async def scenario():
        eng.cfg.cycles = 2
        choices = iter(("long", "short"))
        chosen = []
        def choose(options):
            assert options == ("long", "short")
            result = next(choices)
            chosen.append(result)
            return result
        eng._rng = SimpleNamespace(choice=choose)
        task = asyncio.create_task(eng._strategy_loop())
        for cycle, direction in enumerate(("long", "short"), 1):
            await until(lambda: eng.cycle == cycle and eng.virtual_order is not None)
            order = await touch(eng)
            assert order["side"] == ("sell" if direction == "long" else "buy")
            await until(lambda: eng.state == "LEG3" and eng.virtual_order is not None)
            assert len(eng.entropy.calls) == 2 * cycle - 1
            await touch(eng)
            await until(lambda: eng.completed_cycles == cycle)
        await task
        assert chosen == ["long", "short"]
        assert [o["is_buy"] for o in eng.entropy.calls] == [True, False, False, True]
        assert [o["reduce_only"] for o in eng.entropy.calls] == [False, True, False, True]
        assert [r["leg"] for r in eng.recent_trades] == ["LEG1", "LEG2", "LEG3", "LEG4"] * 2
        assert eng.hedge.calls == []
        assert eng.remaining == 0 and eng.state == "IDLE"
    asyncio.run(scenario())


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


def test_shutdown_during_leg3_closes_even_without_rh_feed(eng):
    async def scenario():
        eng.cfg.direction = "long"
        task = asyncio.create_task(eng._strategy_loop())
        await touch(eng)
        await until(lambda: eng.state == "LEG3")
        eng.hedge.book.clear()
        eng.request_stop()
        await task
        assert len(eng.entropy.calls) == 2
        assert eng.entropy.calls[-1]["reduce_only"]
        assert eng.remaining == 0
        assert eng.recent_trades[-1]["reason"] == "shutdown"
    asyncio.run(scenario())


def test_stop_at_leg1_sends_nothing(eng):
    async def scenario():
        task = asyncio.create_task(eng._strategy_loop())
        await until(lambda: eng.virtual_order is not None)
        eng.request_stop()
        await task
        assert eng.entropy.calls == [] and eng.state == "IDLE"
    asyncio.run(scenario())


def test_virtual_price_fixed_then_requotes_without_fill(eng):
    async def scenario():
        eng.cfg.virtual_requote_sec = .02
        eng.direction = "long"
        task = asyncio.create_task(eng._wait_virtual("LEG1", False, 1))
        await until(lambda: eng.virtual_order is not None)
        first = dict(eng.virtual_order)
        eng.hedge.set_book(99.8, 100)
        eng._update_evt.set()
        await asyncio.sleep(.005)
        assert eng.virtual_order["price"] == first["price"]
        await until(lambda: eng.virtual_order["price"] != first["price"])
        assert not task.done() and eng.entropy.calls == []
        eng.request_stop()
        assert not await task
    asyncio.run(scenario())


def test_reconnected_book_rearms_instead_of_false_fill(eng):
    async def scenario():
        eng.direction = "long"
        task = asyncio.create_task(eng._wait_virtual("LEG1", False, 1))
        await until(lambda: eng.virtual_order is not None)
        old = eng.virtual_order["price"]
        eng.hedge.book.clear()
        eng.hedge.set_book(old + 1, old + 2)
        eng._update_evt.set()
        await until(lambda: eng.virtual_order and eng.virtual_order["price"] > old + 2)
        assert not task.done()
        eng.request_stop()
        await task
    asyncio.run(scenario())


def test_stale_or_crossed_books_do_not_arm(eng):
    async def scenario():
        eng.direction = "long"
        eng.hedge.book.alive_ts = 0
        task = asyncio.create_task(eng._wait_virtual("LEG1", False, 1))
        await asyncio.sleep(.02)
        assert eng.virtual_order is None
        eng.hedge.set_book(101, 100)
        eng._update_evt.set()
        await asyncio.sleep(.02)
        assert eng.virtual_order is None
        eng.request_stop()
        await task
    asyncio.run(scenario())


def test_max_hold_closes_without_leg3_touch(eng):
    async def scenario():
        eng.cfg.direction = "long"
        eng.cfg.max_hold_sec = .01
        task = asyncio.create_task(eng._strategy_loop())
        await touch(eng)
        await task
        assert [r["leg"] for r in eng.recent_trades] == ["LEG1", "LEG2", "LEG4"]
        assert eng.recent_trades[-1]["reason"] == "max_hold"
        assert eng.remaining == 0
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

def test_expired_virtual_order_cannot_fill_on_a_late_update(eng):
    async def scenario():
        import time
        eng.direction = "long"
        task = asyncio.create_task(eng._wait_virtual("LEG1", False, 1))
        await until(lambda: eng.virtual_order is not None)
        price = eng.virtual_order["price"]
        eng.virtual_order["expires"] = time.monotonic() - 1
        eng.hedge.set_book(price, price + .02)
        eng._update_evt.set()
        await until(lambda: eng.virtual_order and eng.virtual_order["price"] > price)
        assert not task.done() and not eng.recent_trades
        eng.request_stop()
        await task
    asyncio.run(scenario())


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

@pytest.mark.parametrize("is_buy,depth,expected", [(True, 4, 99.6), (False, 4, 100.4), (True, 1, 99.9), (False, 6, 100.6)])
def test_virtual_uses_exact_selected_side_depth(eng, is_buy, depth, expected):
    async def scenario():
        eng.cfg.virtual_depth = depth
        eng.direction = "short" if is_buy else "long"
        task = asyncio.create_task(eng._wait_virtual("LEG1", is_buy, 1))
        await until(lambda: eng.virtual_order is not None)
        assert eng.virtual_order["price"] == expected
        assert eng.virtual_order["depth"] == depth
        assert not task.done()  # own-side depth is an actual resting virtual price
        await touch(eng)
        assert await task
        assert eng.recent_trades[-1]["limit_px"] == expected
        assert eng.entropy.calls == []
    asyncio.run(scenario())


def test_missing_depth_waits_instead_of_using_bbo_or_last_level(eng):
    async def scenario():
        eng.direction = "long"
        eng.hedge.book.apply_hl([[{"px": "99.9", "sz": "1"}], [{"px": "100.1", "sz": "1"}]])
        task = asyncio.create_task(eng._wait_virtual("LEG1", False, 1))
        await asyncio.sleep(.02)
        assert eng.virtual_order is None and not task.done()
        eng.hedge.set_book(99.9, 100.1)
        eng._update_evt.set()
        await until(lambda: eng.virtual_order is not None)
        assert eng.virtual_order["price"] == 100.4
        eng.request_stop()
        await task
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
