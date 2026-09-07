import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from entropy_arb.engine import Engine
from test_engine import eng, until


def test_random_long_short_cycles_only_have_leg2_leg4_and_wait_50ms(eng):
    async def scenario():
        eng.cfg.cycles = 2
        eng.cfg.max_spread_bps = 2
        eng.entropy.set_book(99.99, 100.01)  # exactly 2 bps, inclusive threshold
        eng.hedge.book.clear()
        choices = iter(("long", "short"))
        eng._rng = SimpleNamespace(choice=lambda options: next(choices))
        sends = []
        original = eng.entropy.send_market
        async def send(**order):
            sends.append(time.monotonic())
            return await original(**order)
        eng.entropy.send_market = send
        await eng._strategy_loop()
        assert [o["is_buy"] for o in eng.entropy.calls] == [True, False, False, True]
        assert [o["reduce_only"] for o in eng.entropy.calls] == [False, True, False, True]
        assert [r["leg"] for r in eng.recent_trades] == ["LEG2", "LEG4"] * 2
        assert all(not r["virtual"] for r in eng.recent_trades)
        assert all(sends[i + 1] - sends[i] >= .049 for i in (0, 2))
        assert not eng.hedge.calls
        assert eng.completed_cycles == 2 and eng.remaining == 0
        assert eng.total_turnover == 400
    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [.001, .08])
def test_timer_starts_at_submission_and_no_position_query_between_legs(eng, delay):
    async def scenario():
        events, waits = [], []
        original_send = eng.entropy.send_market
        original_position = eng.entropy.fetch_position
        original_delay = eng._delay
        async def send(**order):
            events.append("exit" if order["reduce_only"] else "entry")
            if not order["reduce_only"]:
                await asyncio.sleep(delay)
            return await original_send(**order)
        async def position():
            events.append("position")
            return await original_position()
        async def wait(seconds):
            waits.append(seconds)
            await original_delay(seconds)
        eng.entropy.send_market = send
        eng.entropy.fetch_position = position
        eng._delay = wait
        await eng._strategy_loop()
        assert events[events.index("entry") + 1] == "exit"
        assert events[events.index("exit") + 1] == "position"
        if delay > .05:
            assert waits[0] == 0  # no additional 50ms after a slow fill acknowledgement
        else:
            assert 0 <= waits[0] < .05
    asyncio.run(scenario())


@pytest.mark.parametrize("bid,ask,stale", [(99, 101, False), (101, 100, False),
                                         (100, 100, False), (99.99, 100.01, True)])
def test_wide_crossed_locked_or_stale_entropy_never_opens(eng, bid, ask, stale):
    async def scenario():
        eng.cfg.max_spread_bps = 2
        eng.entropy.set_book(bid, ask)
        if stale:
            eng.entropy.book.alive_ts = 0
        task = asyncio.create_task(eng._strategy_loop())
        await until(lambda: eng.state == "WAIT_SPREAD")
        await asyncio.sleep(.01)
        assert not eng.entropy.calls
        eng.request_stop()
        await task
        assert not eng.entropy.calls and eng.state == "IDLE"
    asyncio.run(scenario())


def test_spread_is_rechecked_after_pre_entry_position_query(eng):
    async def scenario():
        eng.cfg.max_spread_bps = 2
        eng.entropy.set_book(99.99, 100.01)
        reads = 0
        original = eng.entropy.fetch_position
        async def position():
            nonlocal reads
            reads += 1
            if reads == 2:  # strategy flat check, then immediate pre-entry check
                eng.entropy.set_book(99, 101)
            return await original()
        eng.entropy.fetch_position = position
        task = asyncio.create_task(eng._strategy_loop())
        await until(lambda: reads == 2)
        await asyncio.sleep(.01)
        assert not eng.entropy.calls
        eng.entropy.set_book(99.995, 100.005)
        eng._update_evt.set()
        await task
        assert len(eng.entropy.calls) == 2
    asyncio.run(scenario())


def test_exit_ignores_wide_spread_and_closes_actual_partial_quantity(eng):
    async def scenario():
        eng.cfg.max_spread_bps = 2
        eng.entropy.set_book(99.99, 100.01)
        eng.entropy.responses = [dict(filled_base=.6, avg_px=100, status="filled"),
                                 dict(filled_base=.2, avg_px=101, status="filled"),
                                 dict(filled_base=.4, avg_px=101, status="filled")]
        original = eng.entropy.send_market
        async def send(**order):
            result = await original(**order)
            if not order["reduce_only"]:
                eng.entropy.set_book(99, 101)
            return result
        eng.entropy.send_market = send
        await eng._strategy_loop()
        assert [o["qty"] for o in eng.entropy.calls] == [1, .6, .4]
        assert all(o["reduce_only"] for o in eng.entropy.calls[1:])
        assert eng.remaining == 0
    asyncio.run(scenario())


def test_stop_interrupts_delay_and_closes_confirmed_position(eng):
    async def scenario():
        eng.cfg.close_delay_ms = 5000
        task = asyncio.create_task(eng._strategy_loop())
        await until(lambda: eng.state == "WAIT_CLOSE")
        eng.request_stop()
        await asyncio.wait_for(task, 1)
        assert eng.remaining == 0 and len(eng.entropy.calls) == 2
        assert eng.recent_trades[-1]["reason"] == "shutdown"
    asyncio.run(scenario())


def test_entry_reserves_an_exit_order_slot(eng):
    async def scenario():
        eng.cfg.entropy.orders_per_min = 2
        eng._sends.append(time.monotonic())
        task = asyncio.create_task(eng._strategy_loop())
        await until(lambda: eng.state == "WAIT_SPREAD")
        assert not eng.entropy.calls
        eng._sends.clear()
        eng._update_evt.set()
        await task
        assert len(eng.entropy.calls) == 2 and eng.remaining == 0
    asyncio.run(scenario())


def test_record_only_boots_with_only_entropy_market_and_no_rh(eng):
    async def scenario():
        e = Engine(eng.cfg, record_only=True)
        e.cfg.recorder_enabled = False
        venue = eng.entropy
        venue.load_market = AsyncMock()
        venue.close = AsyncMock()
        venue._query_address = lambda: None
        def start_tasks(stop, notify, live):
            e.request_stop()
            return []
        venue.start_tasks = start_tasks
        with patch("entropy_arb.engine.HLVenue", return_value=venue):
            await e.run()
        assert list(e.venues) == ["entropy"]
        assert e.recorder.hedge_book is None
        assert e.finished.is_set() and not venue.calls
        venue.load_market.assert_awaited_once()
    asyncio.run(scenario())
