import asyncio
from unittest.mock import AsyncMock
from entropy_arb.book import OrderBook
from entropy_arb.feeds import LighterBookFeed
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.config import VenueConf, LIGHTER_PROFILES


def test_rh_adapter_has_no_real_order_path():
    rh = LighterVenue(VenueConf("hedge", "lighter", "RH", "TEST",
                                lighter_profile=LIGHTER_PROFILES["lighter-rh"]), None)
    assert not hasattr(rh, "send_taker")
    assert not hasattr(rh, "send_market")
    assert not hasattr(rh, "init_signer")


def test_nonce_gap_clears_book_and_old_deltas_are_ignored():
    async def scenario():
        book, ws = OrderBook(), AsyncMock()
        feed = LighterBookFeed("RH", "wss://example.invalid", 1, book, lambda: None)
        def message(nonce, begin=None, bid="100"):
            return {"channel": "order_book:1", "order_book": {
                "nonce": nonce, "begin_nonce": begin,
                "bids": [{"price": bid, "size": "1"}],
                "asks": [{"price": "101", "size": "1"}]}}
        await feed._handle_book(ws, message(10), snapshot=True)
        revision = book.revision
        await feed._handle_book(ws, message(9, 9, "999"), snapshot=False)
        assert book.revision == revision and book.best_bid() == 100
        await feed._handle_book(ws, message(13, 12), snapshot=False)
        assert not book.ready and ws.send.await_count == 2
        await feed._handle_book(ws, message(14, 14), snapshot=False)
        assert not book.ready
        await feed._handle_book(ws, message(15), snapshot=True)
        assert book.ready
    asyncio.run(scenario())
