"""Compact four-leg execution dashboard, English or Chinese."""
from __future__ import annotations
import asyncio
import logging
from collections import deque
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class BufferLogHandler(logging.Handler):
    def __init__(self, capacity=100):
        super().__init__()
        self.lines = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.lines.append(self.format(record))


class Dashboard:
    def __init__(self, engine, log_buffer, log_file, force_terminal=False, lang="en"):
        self.engine, self.log_buffer, self.log_file = engine, log_buffer, log_file
        self.console = Console(force_terminal=force_terminal or None)
        self.zh = lang == "zh"

    def _t(self, en, zh):
        return zh if self.zh else en

    def _safe_render(self):
        e = self.engine
        mode = self._t("RECORD-ONLY", "仅采集") if e.record_only else self._t("LIVE", "实盘")
        title = f"4LEG | Lighter RH → Entropy | {e.cfg.symbol} | {mode}"
        if not e.markets_ready:
            return Panel(self._t("starting — resolving markets", "启动中：正在解析市场"), title=title)
        state = Text(f"{e.state} | " + self._t("cycle", "轮次") + f" {e.cycle} | "
                     + self._t("completed", "已完成") + f" {e.completed_cycles} | {e.direction}")
        if e.halted:
            state.append("\n" + e.halt_reason, style="red")
        books = Table(expand=True)
        for label in (self._t("Venue", "交易所"), self._t("Bid / Ask", "买一 / 卖一"),
                      self._t("Position", "持仓"), self._t("Feed", "行情")):
            books.add_column(label)
        for venue in e.venues.values():
            bid, ask = venue.book.best_bid(), venue.book.best_ask()
            bbo = f"{bid:g} / {ask:g}" if bid and ask else "—"
            pos = (f"{venue.position:+g}" if venue.key == "entropy"
                   else self._t("virtual only", "仅虚拟腿"))
            books.add_row(venue.name, bbo, pos, "OK" if e._fresh(venue) else self._t("STALE", "超时"))
        order = e.virtual_order
        virtual = (f"{order['leg']} {order['side']} "
                   + self._t("depth", "档位") + f" {order.get('depth', e.cfg.virtual_depth)} "
                   + f"@ {order['price']:g}" if order
                   else self._t("No virtual order", "暂无虚拟挂单"))
        status = Text(virtual + " | " + self._t("Entropy remaining", "Entropy 待平数量") + f" {e.remaining:g}")
        turnover = Text(self._t("Session turnover", "本次成交额")
                        + f" | LEG2 ${e.turnover['LEG2']:,.2f} | LEG4 ${e.turnover['LEG4']:,.2f} | "
                        + self._t("Total", "合计") + f" ${e.total_turnover:,.2f}")
        if any(e.unpriced_filled.values()):
            turnover.append("\n" + self._t("Incomplete: unpriced filled quantity", "合计不完整：缺少均价的已成交数量")
                            + f" LEG2={e.unpriced_filled['LEG2']} LEG4={e.unpriced_filled['LEG4']}",
                            style="yellow")
        legs = Table(expand=True)
        for name in ("Cycle", "Leg", "Venue", "Side", "Filled", self._t("Notional", "成交额"), "Status"):
            legs.add_column(name)
        for row in list(e.recent_trades)[-8:]:
            legs.add_row(str(row["cycle"]), row["leg"], row["venue"], row["side"],
                         f"{row['filled_qty']:g}",
                         f"${row['filled_notional']:,.2f}" if row.get("filled_notional") is not None else "—",
                         row["status"])
        events = Text("\n".join(list(self.log_buffer.lines)[-6:]) if self.log_buffer else "")
        return Panel(Group(state, books, status, turnover, legs, events), title=title)

    async def run(self):
        with Live(self._safe_render(), console=self.console, refresh_per_second=4) as live:
            while not self.engine.stop.is_set():
                live.update(self._safe_render())
                await asyncio.sleep(0.25)
            live.update(self._safe_render())
