"""Entropy IOC wire format and ambiguous-response regression tests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from eth_account import Account
from hyperliquid.utils import signing
from entropy_arb.config import VenueConf
from entropy_arb.venue_hl import HLVenue, HLAccount, NonceAllocator


@pytest.mark.parametrize("body", [None, [], {}, {"status": "ok"},
    {"status": "ok", "response": {"data": {"statuses": ["waitingForFill"]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "NaN", "avgPx": "10"}}]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": "10"}}]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 12}}]}}},
])
def test_malformed_or_open_response_is_unresolved(body):
    assert HLVenue._parse(body)["unresolved"] is True


def test_explicit_no_match_is_resolved_zero():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order could not immediately match against any resting orders."}]}}}
    result = HLVenue._parse(body)
    assert result["filled_base"] == 0 and not result["unresolved"]


def test_full_and_partial_fill_response():
    result = HLVenue._parse({"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.3", "avgPx": "103.1", "oid": 11}}]}}})
    assert result["filled_base"] == .3 and result["avg_px"] == 103.1
    assert result["oid"] == 11
    assert not result["unresolved"]


def venue():
    v = HLVenue(VenueConf("entropy", "hl", "ENTROPY", "TEST"),
                "https://api.hyperliquid.xyz", "wss://api.hyperliquid.xyz/ws", None, .005)
    # Ephemeral test wallet, never persisted or funded; HTTP posting is mocked.
    wallet = Account.create()
    v.account = SimpleNamespace(wallet=wallet, query_address=wallet.address,
                                nonces=NonceAllocator(), is_mainnet=True)
    v._signing = signing
    v.coin, v.asset_id, v.size_decimals = "io:TEST", 110002, 3
    return v


def test_real_sdk_signs_ioc_reduce_only_and_unique_cloids():
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock(return_value=(
            {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.2", "avgPx": "100"}}]}}}, None, False))
        for is_buy in (False, True):
            result = await v.send_market(is_buy=is_buy, qty=.2, reference_px=100, slippage_bps=20, reduce_only=True)
            assert result["filled_base"] == .2
        payloads = [call.args[0] for call in v._post_exchange.call_args_list]
        for payload in payloads:
            order = payload["action"]["orders"][0]
            assert order["t"] == {"limit": {"tif": "Ioc"}}
            assert order["a"] == 110002 and order["r"] is True and order["s"] == "0.2"
            assert set(payload["signature"]) == {"r", "s", "v"}
        assert payloads[0]["action"]["orders"][0]["c"] != payloads[1]["action"]["orders"][0]["c"]
    asyncio.run(scenario())


def test_ambiguous_post_resolves_terminal_partial_fill_by_cloid():
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock(return_value=(None, None, True))
        v._info = AsyncMock(return_value={"status": "order", "order": {
            "status": "canceled", "order": {"origSz": "1", "sz": "0.7"}}})
        result = await v.send_market(is_buy=True, qty=1, reference_px=100, slippage_bps=20)
        assert result["filled_base"] == pytest.approx(.3) and not result["unresolved"]
        payload = v._post_exchange.call_args.args[0]
        assert v._info.call_args.args[0]["oid"] == payload["action"]["orders"][0]["c"]
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["open", "triggered", "unexpected"])
def test_nonterminal_order_status_cannot_confirm_a_fill(status):
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock(return_value=(None, None, True))
        v._info = AsyncMock(return_value={"status": "order", "order": {
            "status": status, "order": {"origSz": "1", "sz": "0"}}})
        result = await v.send_market(is_buy=True, qty=1, reference_px=100, slippage_bps=20)
        assert result["unresolved"] and result["err"].startswith("cloid=")
        assert v._post_exchange.await_count == 1
    asyncio.run(scenario())

def test_invalid_account_response_never_means_flat():
    async def scenario():
        v = venue()
        for response in ({}, [], {"assetPositions": None}):
            v._info = AsyncMock(return_value=response)
            with pytest.raises(RuntimeError, match="Malformed"):
                await v.fetch_position()
        v._info = AsyncMock(return_value={"assetPositions": []})
        assert await v.fetch_position() == 0
        v._info = AsyncMock(return_value={"assetPositions": [
            {"position": {"coin": "io:TEST", "szi": "-0.2"}}]})
        assert await v.fetch_position() == -.2
    asyncio.run(scenario())


def test_open_orders_filter_selected_market():
    async def scenario():
        v = venue()
        v._info = AsyncMock(return_value=[{"coin": "io:TEST", "oid": 1}, {"coin": "io:OTHER", "oid": 2}])
        assert await v.fetch_open_orders() == [{"coin": "io:TEST", "oid": 1}]
        assert v._info.call_args.args[0]["dex"] == "io"
    asyncio.run(scenario())

@pytest.mark.parametrize("is_buy,reduce_only", [(True, False), (False, False), (True, True), (False, True)])
def test_market_wire_enforces_slippage_for_open_and_close(is_buy, reduce_only):
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock(return_value=(
            {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.2", "avgPx": "100"}}]}}}, None, False))
        reference, slip = 100.123, 7.0
        await v.send_market(is_buy=is_buy, qty=.2, reference_px=reference,
                            slippage_bps=slip, reduce_only=reduce_only)
        order = v._post_exchange.call_args.args[0]["action"]["orders"][0]
        limit = float(order["p"])
        assert order["b"] == is_buy and order["r"] == reduce_only
        assert order["t"] == {"limit": {"tif": "Ioc"}}
        if is_buy:
            assert reference <= limit <= reference * (1 + slip / 10000)
        else:
            assert reference >= limit >= reference * (1 - slip / 10000)
    asyncio.run(scenario())


@pytest.mark.parametrize("reference,slip", [(0, 20), (float("nan"), 20), (100, -1), (100, 10000), (100, float("inf"))])
def test_market_invalid_slippage_never_submits(reference, slip):
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock()
        with pytest.raises(ValueError):
            await v.send_market(is_buy=True, qty=1, reference_px=reference, slippage_bps=slip)
        assert v._post_exchange.await_count == 0
    asyncio.run(scenario())


def test_order_status_recovery_uses_actual_fills_for_vwap():
    async def scenario():
        v = venue()
        v._post_exchange = AsyncMock(return_value=(None, None, True))
        v._info = AsyncMock(side_effect=[
            {"status": "order", "order": {"status": "canceled", "order": {
                "oid": 12, "origSz": "1", "sz": ".5"}}},
            [{"oid": 12, "coin": "io:TEST", "tid": 1, "sz": ".2", "px": "100"},
             {"oid": 12, "coin": "io:TEST", "tid": 2, "sz": ".3", "px": "102"},
             {"oid": 12, "coin": "io:TEST", "tid": 2, "sz": ".3", "px": "102"},
             {"oid": 13, "coin": "io:TEST", "tid": 3, "sz": "1", "px": "999"},
             {"oid": 12, "coin": "io:OTHER", "tid": 4, "sz": "1", "px": "999"}]])
        result = await v.send_market(is_buy=True, qty=1, reference_px=100, slippage_bps=20)
        assert result["filled_base"] == .5
        assert result["avg_px"] == pytest.approx(101.2)
        assert not result["unresolved"]
        assert v._info.call_args.args[0]["type"] == "userFills"
    asyncio.run(scenario())


@pytest.mark.parametrize("fills", [[], {},
    [{"oid": 12, "coin": "io:TEST", "tid": 1, "sz": ".1", "px": "100"}],
    [{"oid": 12, "coin": "io:TEST", "tid": 1, "sz": ".5", "px": "NaN"}],
    TimeoutError("price lookup timeout")])
def test_incomplete_fill_history_never_extrapolates_a_price(fills):
    async def scenario():
        v = venue()
        v._info = AsyncMock(side_effect=[fills])
        assert await v._fill_average(12, .5) is None
    asyncio.run(scenario())
