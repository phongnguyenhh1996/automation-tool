from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_derived import (
    DerivedConfig,
    attach_session_profile_to_combined_document,
    compute_absorption_for_candle,
    compute_candle_orderflow,
    compute_imbalance_levels,
    compute_level_rl,
    compute_stacked_in_candle,
    enrich_footprint_combined_document,
    footprint_derived_enabled,
    session_profile_from_combined_document,
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
        {"price": 4024.8, "bid": 14, "ask": 0, "rl": 14.0, "side": "BID", "total_vol": 14},
        {"price": 4024.7, "bid": 12, "ask": 1, "rl": 12.0, "side": "BID", "total_vol": 13},
        {"price": 4024.6, "bid": 20, "ask": 4, "rl": 5.0, "side": "BID", "total_vol": 24},
    ]
    out = compute_stacked_in_candle(levels, rl_min=4.0, stacked_min_levels=3)
    assert len(out) == 1
    assert out[0]["side"] == "BID"
    assert out[0]["level_count"] == 3
    assert out[0]["prices"] == [4024.8, 4024.7, 4024.6]


def test_compute_stacked_in_candle_not_enough_levels() -> None:
    levels = [
        {"price": 4024.8, "bid": 14, "ask": 0, "rl": 14.0, "side": "BID", "total_vol": 14},
        {"price": 4024.7, "bid": 12, "ask": 1, "rl": 12.0, "side": "BID", "total_vol": 13},
    ]
    out = compute_stacked_in_candle(levels, rl_min=4.0, stacked_min_levels=3)
    assert out == []


def test_session_profile_from_combined_document_max_volume_poc() -> None:
    doc = {
        "request": {"interval": "5m"},
        "candles": [
            {
                "footprint": [
                    {"price": 100.0, "buy": {"volume": 30}, "sell": {"volume": 10}},
                    {"price": 101.0, "buy": {"volume": 5}, "sell": {"volume": 5}},
                ]
            },
            {
                "footprint": [
                    {"price": 100.0, "buy": {"volume": 20}, "sell": {"volume": 0}},
                    {"price": 99.0, "buy": {"volume": 8}, "sell": {"volume": 2}},
                ]
            },
        ],
    }
    sp = session_profile_from_combined_document(doc)
    assert sp["poc"] == 100.0
    assert sp["interval"] == "5m"
    assert sp["candles_used"] == 2
    assert sp["total_volume"] == 80
    assert sp["value_area_fraction"] == 0.70

    attached = attach_session_profile_to_combined_document(doc)
    assert attached["session_profile"]["poc"] == 100.0
    assert "histogram" not in attached["session_profile"]


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
                    _level(4024.8, 14, 0),
                    _level(4024.7, 12, 1),
                    _level(4024.6, 20, 4),
                ],
            }
        ],
    }
    out = enrich_footprint_combined_document(doc, cfg={"footprint_ws": {"derived": {"enabled": True}}})
    candle = out["candles"][0]
    assert "orderflow" in candle
    assert candle["orderflow"]["stacked_in_candle"]
    assert "derived_metrics" not in out


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
                        "ohlc": {"low": 4024.6, "high": 4024.8, "close": 4024.7},
                        "footprint": [
                            _level(4024.8, 14, 0),
                            _level(4024.7, 12, 1),
                            _level(4024.6, 20, 4),
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
