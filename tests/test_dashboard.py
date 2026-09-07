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
