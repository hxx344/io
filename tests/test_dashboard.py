from rich.console import Console
from entropy_arb.dashboard import BufferLogHandler, Dashboard
from test_engine import eng  # shared deterministic fixture


def render(engine, lang="en"):
    console = Console(record=True, width=120)
    dash = Dashboard(engine, BufferLogHandler(), "logs/engine.log", lang=lang)
    console.print(dash._safe_render())
    return console.export_text()


def test_startup(eng):
    assert "resolving markets" in render(eng)


def test_four_leg_display(eng):
    eng.markets_ready = True
    eng.state, eng.direction, eng.remaining = "LEG3", "long", 1
    eng.virtual_order = dict(leg="LEG3", side="buy", price=99.98)
    text = render(eng)
    for word in ("4LEG", "LIVE", "LEG3", "virtual only", "99.98", "Entropy remaining 1"):
        assert word in text
    text = render(eng, "zh")
    for word in ("实盘", "仅虚拟腿", "待平数量", "买一 / 卖一"):
        assert word in text


def test_record_only(eng):
    eng.markets_ready, eng.record_only = True, True
    assert "RECORD-ONLY" in render(eng)


def test_dashboard_displays_real_turnover_and_missing_price(eng):
    eng.markets_ready = True
    eng._account_turnover("LEG2", .5, 100)
    eng._account_turnover("LEG4", .5, 102)
    text = render(eng, "zh")
    for word in ("本次成交额", "LEG2 $50.00", "LEG4 $51.00", "合计 $101.00"):
        assert word in text
    assert "Total $101.00" in render(eng)
    eng._account_turnover("LEG4", .1, None)
    assert "合计不完整" in render(eng, "zh")
    assert "Incomplete" in render(eng)


def test_balance_and_net_pnl_bilingual_with_stale_state(eng):
    import time
    from decimal import Decimal as D
    from test_accounting import snapshot
    eng.markets_ready = True
    stats = eng.account_stats
    stats.snapshot = snapshot(unrealized="-2")
    stats.snapshot_revision = stats.revision
    stats.updated_at = time.monotonic()
    stats.realized, stats.fees, stats.funding = D("5"), D(".5"), D("-.1")
    for lang, words in (("zh", ("Entropy 余额", "1,000.00 USDC", "本次策略总净盈亏", "$+2.40", "手续费")),
                        ("en", ("Entropy balance", "Session strategy net PnL", "$+2.40", "Funding"))):
        text = render(eng, lang)
        for word in words:
            assert word in text
    stats.error = "offline"
    assert "数据过期" in render(eng, "zh")
    assert "暂无数据 / 同步中" in render(eng, "zh")
    assert "$+2.40" not in render(eng)


def test_dashboard_survives_stop_until_final_close(eng):
    import asyncio
    from unittest.mock import patch
    async def scenario():
        eng.request_stop()
        dash = Dashboard(eng, None, "unused")
        with patch("entropy_arb.dashboard.Live"):
            task = asyncio.create_task(dash.run())
            await asyncio.sleep(.01)
            assert not task.done()
            eng.finished.set()
            await asyncio.wait_for(task, 1)
    asyncio.run(scenario())
