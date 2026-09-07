"""Read-only Lighter Robinhood public market data. No signer/order API."""
from __future__ import annotations
import asyncio
import logging
import math
import aiohttp
from .book import OrderBook
from .config import VenueConf
from .feeds import LighterBookFeed

log = logging.getLogger("lighter")


class LighterVenue:
    kind = "lighter"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float = 5.0) -> None:
        self.conf, self.session = conf, session
        self.key, self.name = conf.key, conf.label
        self.profile = conf.lighter_profile
        self.book = OrderBook()
        self.market_id = -1
        self.price_decimals = 2

    async def load_market(self) -> None:
        async with self.session.get(self.profile.api_url + "/api/v1/orderBooks",
                                    timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json()
        for market in data.get("order_books") or []:
            if market.get("symbol") != self.conf.symbol:
                continue
            if market.get("status") != "active":
                raise RuntimeError(f"[RH] {self.conf.symbol} is not active")
            self.market_id = int(market["market_id"])
            self.price_decimals = int(market["supported_price_decimals"])
            log.info("[RH] public reference %s market_id=%d", self.conf.symbol, self.market_id)
            return
        raise RuntimeError(f"[RH] {self.conf.symbol} not found")

    def px_round(self, px: float, round_up: bool) -> float:
        factor = 10 ** self.price_decimals
        rounded = math.ceil(px * factor) if round_up else math.floor(px * factor)
        return round(rounded / factor, self.price_decimals)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool = False) -> list:
        if live:
            raise RuntimeError("Lighter RH legs are virtual; live orders are disabled")
        return [asyncio.create_task(LighterBookFeed(
            self.name, self.profile.ws_url, self.market_id, self.book,
            notify).run(stop), name="book-rh")]

    async def close(self) -> None:
        pass
