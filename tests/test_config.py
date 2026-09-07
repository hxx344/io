from pathlib import Path
import pytest
from entropy_arb.config import ConfigError, load_config


@pytest.fixture
def load(tmp_path, monkeypatch):
    monkeypatch.delenv("HL_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    def read(text="{}", **kwargs):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(str(path), str(tmp_path / "missing.env"), symbol="TEST", **kwargs)
    return read


def test_default_random_fixed_rh(load):
    cfg = load()
    assert cfg.direction == "random"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.hl_dex == "io"
    assert not cfg.creds_complete


def test_only_entropy_credentials_required(load, monkeypatch):
    monkeypatch.setenv("HL_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("LIGHTER_ACCOUNT_INDEX", "not-an-integer")
    assert load().creds_complete  # RH credentials are never parsed/required


def test_example():
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(str(path), "missing.env", symbol="SNDK")
    assert cfg.direction == "random" and cfg.virtual_depth == 4


@pytest.mark.parametrize("text", [
    "thresholds: {midline_bps: 0}", "hedge: {taker_fee_bps: 0}",
    "entropy: {dex: xyz}", "cycle: {direction: buy}", "cycle: {cycles: -1}",
    "cycle: {cycles: true}", "cycle: {virtual_depth: 0}",
    "cycle: {virtual_depth: -1}", "cycle: {virtual_depth: 1.5}", "cycle: {virtual_depth: true}", "cycle: {virtual_requote_sec: 0}",
    "cycle: {max_hold_sec: -1}", "sizing: {quantity: .nan}", "sizing: {quantity: .inf}",
    "sizing: {quantity: -1}", "sizing: {max_order_notional_usd: 1}",
    "execution: {max_order_attempts: 0}", "execution: {leg_slippage_bps: 10000}",
    "execution: {staleness_sec: 0}", "entropy: {max_orders_per_min: 0}",
    "logging: {level: unknown}", "logging: {file: ''}", "[]", "cycle: []", "false",
])
def test_invalid_config(load, text):
    with pytest.raises(ConfigError):
        load(text)


@pytest.mark.parametrize("venue", ["lighter", "tradexyz", "binance"])
def test_other_venues_rejected(load, venue):
    with pytest.raises(ConfigError, match="fixed"):
        load(hedge_venue=venue)

@pytest.mark.parametrize("key", ["leg2_slippage_bps", "leg4_slippage_bps"])
@pytest.mark.parametrize("value", ["-1", "10000", ".nan", ".inf"])
def test_invalid_per_leg_slippage(load, key, value):
    with pytest.raises(ConfigError):
        load(f"execution: {{{key}: {value}}}")


def test_depth_and_independent_slippage_config(load):
    cfg = load("cycle: {virtual_depth: 8}\nexecution: {leg2_slippage_bps: 12, leg4_slippage_bps: 25}")
    assert cfg.virtual_depth == 8
    assert cfg.leg2_slippage_bps == 12 and cfg.leg4_slippage_bps == 25


def test_old_virtual_offset_requires_migration(load):
    with pytest.raises(ConfigError, match="virtual_offset_bps"):
        load("cycle: {virtual_offset_bps: 2}")
