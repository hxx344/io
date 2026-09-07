import asyncio
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from entropy_arb.accounting import SessionAccount
from test_engine import eng
from test_venue_hl import venue


def snapshot(position="0", unrealized="0"):
    return dict(balance=D("1000"), equity=D("1002"), free=D("900"), scope="io USDC",
                position=D(position), unrealized=D(unrealized))


def fill(oid, tid, sz, pnl="0", fee="0.1", coin="io:TEST"):
    return dict(oid=oid, tid=tid, sz=sz, closedPnl=pnl, fee=fee, coin=coin, feeToken="USDC")


def test_session_fills_partial_close_fees_funding_dedup_and_other_orders():
    async def scenario():
        stats = SessionAccount()
        stats.record_fill(10, 1, stats.started_ms)
        stats.record_fill(11, .4, stats.started_ms)
        fills = [fill(10, 1, ".3"), fill(10, 2, ".7", fee=".2"),
                 fill(11, 3, ".4", pnl="2", fee=".04"), fill(99, 9, "1", pnl="999"),
                 fill(10, 99, "1", pnl="999", coin="xyz:TEST")]
        funding = [dict(time=stats.started_ms, hash="x", delta=dict(coin="io:TEST", usdc="-.05")),
                   dict(time=stats.started_ms, hash="y", delta=dict(coin="xyz:TEST", usdc="99"))]
        async def history(kind, *args):
            return fills + [fills[0]] if kind == "userFillsByTime" else funding * 2
        v = SimpleNamespace(coin="io:TEST", size_decimals=3, fetch_history=history,
                            fetch_account_snapshot=AsyncMock(return_value=snapshot(".6", "3")))
        await stats.refresh(v, .6)
        assert stats.total_pnl == D("4.61")  # 2 + 3 - .34 - .05
        await stats.refresh(v, .6)
        assert stats.total_pnl == D("4.61")
        stats.record_fill(12, .6, stats.started_ms)
        assert stats.total_pnl is None
        fills.append(fill(12, 4, ".6", pnl="-1", fee=".06"))
        v.fetch_account_snapshot.return_value = snapshot()
        await stats.refresh(v, 0)
        assert stats.total_pnl == D(".55")
    asyncio.run(scenario())


def test_incomplete_fill_history_retries_without_double_counting():
    async def scenario():
        stats = SessionAccount()
        stats.record_fill(10, 1, stats.started_ms)
        v = SimpleNamespace(coin="io:TEST", size_decimals=3,
                            fetch_account_snapshot=AsyncMock(return_value=snapshot("1", "2")))
        rows = [fill(10, 1, ".4")]
        async def history(kind, *args):
            return rows if kind == "userFillsByTime" else []
        v.fetch_history = history
        await stats.refresh(v, 1)
        assert stats.total_pnl is None and stats.fees == 0
        rows.append(fill(10, 2, ".6", fee=".2"))
        await stats.refresh(v, 1)
        assert stats.total_pnl == D("1.7")
        stats.record_fill(None, .1, stats.started_ms)
        assert stats.total_pnl is None
    asyncio.run(scenario())


def test_account_error_retains_balance_and_recovery_does_not_reset_session(eng):
    async def scenario():
        eng.entropy.fetch_history = AsyncMock(return_value=[])
        eng.entropy.fetch_account_snapshot = AsyncMock(return_value=snapshot())
        await eng._refresh_account()
        assert eng.account_stats.total_pnl == 0
        eng.entropy.fetch_account_snapshot.side_effect = OSError("offline")
        await eng._refresh_account()
        assert eng.account_stats.snapshot["balance"] == 1000
        assert eng.account_stats.error == "offline" and eng.account_stats.total_pnl is None
        eng.entropy.fetch_account_snapshot.side_effect = None
        eng.entropy.fetch_account_snapshot.return_value = snapshot("1")
        await eng._refresh_account()
        assert "not yet synchronized" in eng.account_stats.error
        eng.entropy.fetch_account_snapshot.return_value = snapshot()
        await eng._refresh_account()
        assert eng.account_stats.total_pnl == 0
    asyncio.run(scenario())


def test_order_during_refresh_invalidates_pnl():
    async def scenario():
        stats = SessionAccount()
        async def get_snapshot():
            stats.record_fill(2, 1, stats.started_ms)
            return snapshot()
        v = SimpleNamespace(size_decimals=3, fetch_history=AsyncMock(return_value=[]),
                            fetch_account_snapshot=get_snapshot)
        with pytest.raises(ValueError, match="Orders changed"):
            await stats.refresh(v, 0)
        assert stats.total_pnl is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["disabled", "default", "unifiedAccount", "portfolioMargin", "dexAbstraction"])
def test_balance_scope_and_symbol_unrealized(mode):
    async def scenario():
        v = venue()
        state = dict(marginSummary=dict(totalRawUsd="100", accountValue="103"), withdrawable="80",
                     assetPositions=[dict(position=dict(coin="io:TEST", szi="-.5", unrealizedPnl="3")),
                                     dict(position=dict(coin="io:OTHER", szi="1", unrealizedPnl="999"))])
        async def info(payload):
            if payload["type"] == "userAbstraction":
                return mode
            if payload["type"] == "spotClearinghouseState":
                return dict(balances=[dict(token=0, total="234", hold="99")])
            assert payload["user"] == v.account.query_address
            return state
        v._info = info
        snap = await v.fetch_account_snapshot()
        assert snap["position"] == D("-.5") and snap["unrealized"] == 3
        if mode in ("unifiedAccount", "portfolioMargin"):
            assert snap["balance"] == 234 and snap["free"] is None and snap["equity"] is None
        else:
            assert snap["balance"] == 100 and snap["equity"] == 103 and snap["free"] == 80
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [{}, {"assetPositions": []},
    dict(assetPositions=[], marginSummary=dict(totalRawUsd="NaN", accountValue="0"), withdrawable="0")])
def test_malformed_account_is_not_zero_balance(bad):
    async def scenario():
        v = venue()
        async def info(payload):
            return "disabled" if payload["type"] == "userAbstraction" else bad
        v._info = info
        with pytest.raises((ValueError, KeyError)):
            await v.fetch_account_snapshot()
    asyncio.run(scenario())


def test_history_pagination_keeps_timestamp_boundaries():
    async def scenario():
        v = venue()
        v._info = AsyncMock(side_effect=[
            [dict(time=100, tid=1), dict(time=101, tid=2)],
            [dict(time=101, tid=2), dict(time=101, tid=3)], []])
        rows = await v.fetch_history("userFillsByTime", 99, 110)
        assert [r["tid"] for r in rows] == [1, 2, 3]
        assert [c.args[0]["startTime"] for c in v._info.call_args_list] == [99, 101, 102]
        assert all(c.args[0]["aggregateByTime"] is False for c in v._info.call_args_list)
    asyncio.run(scenario())


def test_saturated_history_is_explicit_error():
    async def scenario():
        v = venue()
        v._info = AsyncMock(return_value=[dict(time=100)] * 500)
        with pytest.raises(ValueError, match="saturated"):
            await v.fetch_history("userFunding", 100, 101)
    asyncio.run(scenario())


def test_real_engine_orders_feed_accounting_without_virtual_legs(eng):
    async def scenario():
        eng.direction = "short"
        eng.entropy.coin = "io:TEST"
        eng.entropy.responses = [dict(oid=20, filled_base=1, avg_px=100, status="filled"),
                                 dict(oid=21, filled_base=1, avg_px=98, status="filled")]
        await eng._open_position()
        assert eng.account_stats.pending.keys() == {"20"}
        await eng._close_position()
        assert eng.account_stats.pending.keys() == {"20", "21"}
        async def history(kind, *args):
            return [fill(20, 1, "1"), fill(21, 2, "1", pnl="2")] if kind == "userFillsByTime" else []
        eng.entropy.fetch_history = history
        eng.entropy.fetch_account_snapshot = AsyncMock(return_value=snapshot())
        await eng._refresh_account()
        assert eng.account_stats.total_pnl == D("1.8")
        assert not eng.hedge.calls
    asyncio.run(scenario())
