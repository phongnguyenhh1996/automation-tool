from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_derived import (
    DerivedConfig,
    compute_absorption_for_candle,
    compute_candle_orderflow,
    compute_diagonal_level_rl_at_index,
    compute_imbalance_levels,
    compute_level_rl,
    compute_stacked_in_candle,
    enrich_footprint_combined_document,
    enrich_footprint_levels_in_candle,
    enrich_prepared_footprint_stacked,
    footprint_derived_enabled,
    _level_volumes,
)
from automation_tool.openai_prompt_flow import _json_file_header_and_body


def _level(price: float, bid: int, ask: int) -> dict:
    return {
        "price": price,
        "buy": {"volume": bid},
        "sell": {"volume": ask},
    }


def test_compute_level_rl_bid_dominant() -> None:
    rl, side = compute_level_rl(120, 25)
    assert rl == 4.8
    assert side == "BID"


def test_compute_level_rl_zero_volumes() -> None:
    rl, side = compute_level_rl(0, 0)
    assert rl is None
    assert side is None


def test_compute_diagonal_level_rl_bid_vs_ask_below() -> None:
    levels = [
        {"price": 4024.8, "bid": 40, "ask": 0},
        {"price": 4024.7, "bid": 5, "ask": 5},
    ]
    rl, side = compute_diagonal_level_rl_at_index(levels, 0)
    assert rl == 8.0
    assert side == "BID"


def test_compute_diagonal_level_rl_ask_vs_bid_above() -> None:
    levels = [
        {"price": 4024.8, "bid": 10, "ask": 0},
        {"price": 4024.7, "bid": 5, "ask": 40},
    ]
    rl, side = compute_diagonal_level_rl_at_index(levels, 1)
    assert rl == 4.0
    assert side == "ASK"


def test_compute_diagonal_level_rl_bottom_row_no_bid_signal() -> None:
    levels = [
        {"price": 4024.8, "bid": 40, "ask": 0},
        {"price": 4024.7, "bid": 5, "ask": 20},
    ]
    rl, side = compute_diagonal_level_rl_at_index(levels, 1)
    assert rl == 0.5
    assert side == "ASK"
    rl_top, side_top = compute_diagonal_level_rl_at_index(levels, 0)
    assert rl_top == 2.0
    assert side_top == "BID"


def test_compute_imbalance_levels_filters_below_rl_min() -> None:
    levels = [
        {"price": 100.0, "bid": 10, "ask": 2, "rl": 5.0, "side": "BID", "total_vol": 12},
        {"price": 99.6, "bid": 2, "ask": 2, "rl": 1.0, "side": None, "total_vol": 4},
    ]
    out = compute_imbalance_levels(levels, rl_min=4.0)
    assert len(out) == 1
    assert out[0]["price"] == 100.0
    assert out[0]["rl"] == 5.0


def test_compute_stacked_in_candle_three_levels() -> None:
    levels = [
        {"price": 4025.0, "bid": 50, "ask": 0, "rl": 10.0, "side": "BID", "total_vol": 50},
        {"price": 4024.8, "bid": 40, "ask": 5, "rl": 10.0, "side": "BID", "total_vol": 45},
        {"price": 4024.7, "bid": 36, "ask": 4, "rl": 9.0, "side": "BID", "total_vol": 40},
    ]
    out = compute_stacked_in_candle(levels, rl_min=4.0, stacked_min_levels=3)
    assert len(out) == 1
    assert out[0]["side"] == "BID"
    assert out[0]["level_count"] == 3
    assert out[0]["prices"] == [4025.0, 4024.8, 4024.7]


def test_compute_stacked_in_candle_not_enough_levels() -> None:
    levels = [
        {"price": 4024.8, "bid": 14, "ask": 0, "rl": 14.0, "side": "BID", "total_vol": 14},
        {"price": 4024.7, "bid": 12, "ask": 1, "rl": 12.0, "side": "BID", "total_vol": 13},
    ]
    out = compute_stacked_in_candle(levels, rl_min=4.0, stacked_min_levels=3)
    assert out == []


def test_compute_absorption_bid_at_low() -> None:
    candle = {"ohlc": {"low": 4707.0, "high": 4712.0, "close": 4709.2}}
    levels = [
        {"price": 4707.1, "bid": 120, "ask": 730, "rl": 6.08, "side": "ASK", "total_vol": 850},
        {"price": 4708.0, "bid": 50, "ask": 60, "rl": 1.2, "side": "ASK", "total_vol": 110},
    ]
    cfg = DerivedConfig(
        absorption_volume_pct=0.25,
        absorption_extreme_ticks=2,
        absorption_side_ratio=1.5,
        tick_size=0.1,
    )
    out = compute_absorption_for_candle(candle, levels, cfg=cfg)
    assert len(out) == 1
    assert out[0]["side"] == "BID"
    assert out[0]["price"] == 4707.1


def test_compute_absorption_ask_at_high() -> None:
    candle = {"ohlc": {"low": 4707.0, "high": 4712.0, "close": 4710.8}}
    levels = [
        {"price": 4711.9, "bid": 480, "ask": 140, "rl": 3.43, "side": "BID", "total_vol": 620},
    ]
    cfg = DerivedConfig(
        absorption_volume_pct=0.0,
        absorption_extreme_ticks=2,
        absorption_side_ratio=1.5,
        tick_size=0.1,
    )
    out = compute_absorption_for_candle(candle, levels, cfg=cfg)
    assert len(out) == 1
    assert out[0]["side"] == "ASK"


def test_enrich_footprint_combined_document_adds_orderflow() -> None:
    doc = {
        "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "ohlc": {"low": 4707.0, "high": 4712.0, "close": 4709.2},
                "footprint": [
                    _level(4025.0, 50, 0),
                    _level(4024.8, 40, 5),
                    _level(4024.7, 36, 4),
                    _level(4024.6, 1, 1),
                ],
            }
        ],
    }
    out = enrich_footprint_combined_document(doc, cfg={"footprint_ws": {"derived": {"enabled": True}}})
    candle = out["candles"][0]
    assert "orderflow" in candle
    assert candle["orderflow"]["stacked_in_candle"]
    level = candle["footprint"][0]
    assert level["rl"] == 10.0
    assert level["imbalance"] == "bid"
    assert candle["footprint"][1]["imbalance"] == "bid"
    assert candle["footprint"][2]["imbalance"] == "bid"
    assert candle["footprint"][3]["imbalance"] == ""
    assert "derived_metrics" not in out
    assert "imbalance_levels" not in candle["orderflow"]


def test_enrich_footprint_levels_omits_imbalance_when_disabled() -> None:
    candle = {
        "footprint": [
            _level(4024.8, 40, 0),
            _level(4024.7, 2, 10),
        ],
    }
    out = enrich_footprint_levels_in_candle(
        candle,
        cfg=DerivedConfig(imbalance_enabled=False),
    )
    assert out[0]["rl"] == 4.0
    assert "imbalance" not in out[0]
    assert out[1]["rl"] == 0.25
    assert "imbalance" not in out[1]


def test_compute_candle_orderflow_respects_feature_toggles() -> None:
    candle = {
        "ohlc": {"low": 4707.0, "high": 4712.0, "close": 4709.2},
        "footprint": [
            _level(4024.8, 14, 0),
            _level(4024.7, 12, 1),
            _level(4024.6, 20, 4),
        ],
    }
    cfg = DerivedConfig(
        imbalance_enabled=False,
        stacked_enabled=False,
        absorption_enabled=False,
    )
    out = compute_candle_orderflow(candle, cfg=cfg)
    assert out == {}
    assert "stacked_in_candle" not in out
    assert "absorption" not in out


def test_enrich_footprint_combined_document_omits_orderflow_when_all_disabled() -> None:
    doc = {
        "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "footprint": [_level(4024.8, 40, 0), _level(4024.7, 5, 5)],
            }
        ],
    }
    out = enrich_footprint_combined_document(
        doc,
        cfg={
            "footprint_ws": {
                "derived": {
                    "enabled": True,
                    "imbalance_enabled": False,
                    "stacked_enabled": False,
                    "absorption_enabled": False,
                }
            }
        },
    )
    candle = out["candles"][0]
    assert "orderflow" not in candle
    assert candle["footprint"][0]["rl"] == 8.0
    assert "imbalance" not in candle["footprint"][0]


def test_footprint_derived_enabled_env_override() -> None:
    cfg = {"footprint_ws": {"derived": {"enabled": True}}}
    assert footprint_derived_enabled(cfg) is True


def test_json_file_header_and_body_enriches_combined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fp_dir = tmp_path / "footprint_images"
    fp_dir.mkdir(parents=True)
    json_path = fp_dir / "footprint_combined_5m.json"
    json_path.write_text(
        json.dumps(
            {
                "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
                "candles": [
                    {
                        "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                        "ohlc": {"low": 4024.6, "high": 4025.0, "close": 4024.7},
                        "footprint": [
                            _level(4025.0, 50, 0),
                            _level(4024.8, 40, 5),
                            _level(4024.7, 36, 4),
                            _level(4024.6, 1, 1),
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOOTPRINT_WS_MAX_CANDLES", "50")

    header, body = _json_file_header_and_body(json_path, max_chars=100_000)
    payload = json.loads(body)
    assert "orderflow" in payload["candles"][0]
    assert "stacked_in_candle" in payload["candles"][0]["orderflow"]
    assert payload["candles"][0]["footprint"][0]["rl"] == 10.0
    assert payload["candles"][0]["footprint"][0]["imbalance"] == "bid"
    assert "orderflow" in header


def test_json_file_header_and_body_drops_forming_candle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_tool import gocharting_ws_decode as ws_decode

    fp_dir = tmp_path / "footprint_images"
    fp_dir.mkdir(parents=True)
    json_path = fp_dir / "footprint_combined_5m.json"
    closed_time = "Thu Jun 25 2026 11:25:00 GMT+0700"
    forming_time = "Thu Jun 25 2026 11:30:00 GMT+0700"
    json_path.write_text(
        json.dumps(
            {
                "is_complete": False,
                "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
                "candles": [
                    {
                        "time_gmt7": closed_time,
                        "ohlc": {"low": 4024.6, "high": 4024.8, "close": 4024.7},
                        "footprint": [_level(4024.8, 14, 0)],
                    },
                    {
                        "time_gmt7": forming_time,
                        "ohlc": {"low": 4025.0, "high": 4025.2, "close": 4025.1},
                        "footprint": [_level(4025.2, 10, 2)],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOOTPRINT_WS_MAX_CANDLES", "50")
    fixed_now = datetime(2026, 6, 25, 11, 32)
    original_drop = ws_decode.drop_forming_footprint_candle

    def drop_with_fixed_now(doc: dict, **kwargs):
        kwargs.setdefault("now", fixed_now)
        return original_drop(doc, **kwargs)

    monkeypatch.setattr(ws_decode, "drop_forming_footprint_candle", drop_with_fixed_now)

    _header, body = _json_file_header_and_body(json_path, max_chars=100_000)
    payload = json.loads(body)
    assert len(payload["candles"]) == 1
    assert payload["candles"][0]["time_gmt7"] == closed_time
    assert forming_time not in body


def test_level_volumes_accepts_slim_int_buy_sell() -> None:
    price, bid, ask = _level_volumes({"price": 4019.0, "buy": 12, "sell": 3})
    assert price == 4019.0
    assert bid == 12
    assert ask == 3


def test_enrich_prepared_footprint_stacked_on_slim_footprint() -> None:
    doc = {
        "symbol": "XAUUSD",
        "interval": "5m",
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "ohlc": {"low": 4024.6, "high": 4025.0, "close": 4024.8},
                "footprint": [
                    {"price": 4025.0, "buy": 50, "sell": 0},
                    {"price": 4024.8, "buy": 40, "sell": 5},
                    {"price": 4024.7, "buy": 36, "sell": 4},
                    {"price": 4024.6, "buy": 1, "sell": 1},
                ],
            }
        ],
    }
    out = enrich_prepared_footprint_stacked(
        doc,
        cfg={
            "footprint_ws": {
                "derived": {
                    "enabled": True,
                    "stacked_enabled": True,
                    "stacked_min_levels": 3,
                    "rl_min": 4.0,
                }
            }
        },
    )
    candle = out["candles"][0]
    assert "orderflow" in candle
    assert candle["orderflow"]["stacked_in_candle"]
    stacked = candle["orderflow"]["stacked_in_candle"][0]
    assert stacked["side"] == "BID"
    assert stacked["level_count"] >= 3
    assert stacked["prices"][0] == 4025.0


def test_enrich_prepared_footprint_stacked_respects_stacked_disabled() -> None:
    doc = {
        "symbol": "XAUUSD",
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "footprint": [
                    {"price": 4025.0, "buy": 50, "sell": 0},
                    {"price": 4024.8, "buy": 40, "sell": 5},
                    {"price": 4024.7, "buy": 36, "sell": 4},
                ],
            }
        ],
    }
    cfg = {
        "footprint_ws": {
            "derived": {
                "enabled": False,
                "stacked_enabled": False,
            }
        }
    }
    out = enrich_prepared_footprint_stacked(doc, cfg=cfg)
    assert "orderflow" not in out["candles"][0]
