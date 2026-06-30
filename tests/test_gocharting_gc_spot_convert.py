"""Tests for GC → spot footprint conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.gocharting_gc_spot_convert import (
    GcToSpotConversionError,
    GOCHARTING_GC_EXPORT_LABEL,
    build_basis_index,
    convert_footprint_combined_to_spot,
    enrich_prepared_footprint_from_gc_csv,
    finalize_prepared_spot_footprint,
    is_finalized_spot_footprint,
    is_gocharting_main_pair_path,
    is_prepared_footprint_path,
    parse_prepared_footprint_path,
    prepared_footprint_json_path,
    validate_match_ratio,
    _parse_gc_csv_bar_flow_rows,
    _shift_bar_flow_prices,
)
from automation_tool.images import (
    existing_prepared_footprint_json_paths,
    ordered_chart_openai_payloads,
    persist_prepared_footprint_json_files,
    GOCHARTING_GOLD_EXPORT_LABEL,
)


def _mt5_payload(time_key: str, close: float) -> dict:
    return {
        "symbol": "XAUUSD",
        "broker_symbol": "XAUUSDm",
        "interval": "5m",
        "bars": [
            {
                "t": "2026-06-25T05:00:00+07:00",
                "open": close - 1,
                "high": close + 2,
                "low": close - 3,
                "close": close,
                "tick_volume": 100,
            }
        ],
    }


def test_prepared_footprint_path_helpers() -> None:
    p = prepared_footprint_json_path(Path("/tmp/charts"), "XAUUSD", "5m")
    assert p.name == "footprint_XAUUSD_5m.json"
    assert is_prepared_footprint_path(p)
    assert parse_prepared_footprint_path(p) == ("XAUUSD", "5m")
    assert not is_prepared_footprint_path(Path("footprint_combined_5m.json"))
    assert not is_prepared_footprint_path(Path("footprint_combined_15m.json"))
    assert parse_prepared_footprint_path(Path("footprint_combined_5m.json")) is None


def test_is_gocharting_main_pair_path() -> None:
    assert is_gocharting_main_pair_path(Path(f"20260101_gocharting_{GOCHARTING_GC_EXPORT_LABEL}_5m.csv"))
    assert is_gocharting_main_pair_path(Path("footprint_XAUUSD_15m.json"))
    assert not is_gocharting_main_pair_path(Path("20260101_gocharting_DXY_15m.csv"))


def test_build_basis_index() -> None:
    time_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    candles = [{"time_gmt7": time_key, "ohlc": {"close": 4019.0}}]
    mt5 = _mt5_payload(time_key, 4023.5)
    basis_index, spot_index = build_basis_index(candles, mt5)
    assert basis_index[time_key] == pytest.approx(4.5)
    assert spot_index[time_key]["close"] == 4023.5


def test_convert_footprint_combined_to_spot_shifts_prices() -> None:
    time_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    doc = {
        "symbol": "COMEX:GC1!",
        "candles": [
            {
                "time_gmt7": time_key,
                "ohlc": {"open": 4019.0, "high": 4020.0, "low": 4018.0, "close": 4019.0, "volume": 10},
                "footprint": [
                    {"price": 4019.0, "buy": 5, "sell": 3},
                    {"price": 4018.9, "buy": 1, "sell": 2},
                ],
            }
        ],
    }
    mt5 = _mt5_payload(time_key, 4023.5)
    cfg = {"footprint_ws": {"gc_to_spot": {"enabled": True, "min_matched_ratio": 0.8}}}
    out = convert_footprint_combined_to_spot(
        doc,
        mt5_payload=mt5,
        cfg=cfg,
        logic_symbol="XAUUSD",
        interval="5m",
    )
    c0 = out["candles"][0]
    assert out["symbol"] == "XAUUSD"
    assert c0["ohlc"]["close"] == 4023.5
    prices = [lvl["price"] for lvl in c0["footprint"]]
    assert 4023.4 in prices or 4023.5 in prices


def test_validate_match_ratio_raises() -> None:
    cfg = {"footprint_ws": {"gc_to_spot": {"min_matched_ratio": 0.8}}}
    with pytest.raises(GcToSpotConversionError):
        validate_match_ratio(1, 10, cfg, label="test")


def test_parse_gc_csv_and_enrich_bar_flow(tmp_path: Path) -> None:
    time_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    csv_text = (
        "Date,Open,High,Low,Close,Volume,Delta,MaxDelta,MinDelta,CumDelta,"
        "BuyVolume,SellVolume,Vwap,BuyVwap,SellVwap\n"
        f'"{time_key}","4019.0","4020.0","4018.0","4019.0","100","-5","1","-6","-10",'
        f'"40","45","4019.5","4020.0","4018.0"\n'
    )
    csv_path = tmp_path / "20260101_gocharting_GC_5m.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    rows = _parse_gc_csv_bar_flow_rows(csv_text)
    assert rows[time_key]["delta"] == -5

    doc = {
        "candles": [
            {
                "time_gmt7": time_key,
                "ohlc": {"close": 4023.5},
                "footprint": [],
            }
        ]
    }
    cfg = {"footprint_ws": {"gc_to_spot": {"min_matched_ratio": 0.8}}}
    basis_index = {time_key: 4.5}
    out = enrich_prepared_footprint_from_gc_csv(
        doc, csv_path, cfg=cfg, basis_index=basis_index
    )
    bar_flow = out["candles"][0]["bar_flow"]
    assert bar_flow["delta"] == -5
    assert bar_flow["close"] == pytest.approx(4023.5)
    assert bar_flow["buyvwap"] == pytest.approx(4024.5)
    assert bar_flow["sellvwap"] == pytest.approx(4022.5)
    assert "source" not in out


def test_shift_bar_flow_prices_includes_buyvwap_sellvwap() -> None:
    basis = -13.7261
    bar = {
        "vwap": 4017.04,
        "buyvwap": 4031.0,
        "sellvwap": 4030.0,
        "buy_vwap": 4031.5,
        "sell_vwap": 4029.5,
    }
    out = _shift_bar_flow_prices(bar, basis, spot_tick=0.01)
    assert out["vwap"] == pytest.approx(4003.31)
    assert out["buyvwap"] == pytest.approx(4017.27)
    assert out["sellvwap"] == pytest.approx(4016.27)
    assert out["buy_vwap"] == pytest.approx(4017.77)
    assert out["sell_vwap"] == pytest.approx(4015.77)


def test_convert_footprint_drops_unmatched_candles() -> None:
    matched_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    unmatched_key = "Thu Jun 25 2026 05:05:00 GMT+0700"
    doc = {
        "symbol": "COMEX:GC1!",
        "candles": [
            {
                "time_gmt7": matched_key,
                "ohlc": {"close": 4019.0},
                "footprint": [{"price": 4019.0, "buy": 1, "sell": 1}],
            },
            {
                "time_gmt7": unmatched_key,
                "ohlc": {"close": 4020.0},
                "footprint": [{"price": 4020.0, "buy": 2, "sell": 2}],
                "mt5_spot_ohlc": {"close": 4006.0},
            },
        ],
    }
    mt5 = _mt5_payload(matched_key, 4023.5)
    cfg = {"footprint_ws": {"gc_to_spot": {"enabled": True, "min_matched_ratio": 0.5}}}
    out = convert_footprint_combined_to_spot(
        doc,
        mt5_payload=mt5,
        cfg=cfg,
        logic_symbol="XAUUSD",
        interval="5m",
    )
    assert len(out["candles"]) == 1
    assert out["candles"][0]["time_gmt7"] == matched_key
    assert "mt5_spot_ohlc" not in out["candles"][0]
    assert "gc_to_spot" not in out


def test_finalize_prepared_spot_footprint_strips_gc_metadata() -> None:
    raw = {
        "symbol": "XAUUSD",
        "interval": "5m",
        "ohlc_matched": 48,
        "ohlc_available": 686,
        "gc_to_spot": {"matched": 48, "total": 49, "avg_basis": -13.7},
        "source": {"csv": "gc.csv", "footprint_ws": "footprint_combined_5m.json"},
        "request": {
            "exchange": "COMEX",
            "segment": "FUTURE",
            "symbol": "GC1!",
            "interval": "5m",
            "session": "ETH",
            "date": "2026-06-30",
        },
        "candles": [],
    }
    out = finalize_prepared_spot_footprint(raw, logic_symbol="XAUUSD", interval="5m")
    assert out["symbol"] == "XAUUSD"
    assert out["request"] == {
        "symbol": "XAUUSD",
        "interval": "5m",
        "date": "2026-06-30",
    }
    assert is_finalized_spot_footprint(out, logic_symbol="XAUUSD")
    for key in ("source", "gc_to_spot", "ohlc_matched", "ohlc_available"):
        assert key not in out


def test_ordered_payloads_skip_gc_csv_when_gc_to_spot(tmp_path: Path) -> None:
    stamp = "20260101_120000"
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    m15_csv = tmp_path / f"{stamp}_gocharting_{sym}_15m.csv"
    m5_csv = tmp_path / f"{stamp}_gocharting_{sym}_5m.csv"
    m15_csv.write_text("Date,Open\nx,1\n", encoding="utf-8")
    m5_csv.write_text("Date,Open\nx,1\n", encoding="utf-8")
    (tmp_path / f"{stamp}_gocharting_{sym}_15m.png").write_bytes(b"x")
    (tmp_path / f"{stamp}_gocharting_{sym}_5m.png").write_bytes(b"x")
    (tmp_path / "footprint_XAUUSD_15m.json").write_text("{}", encoding="utf-8")
    (tmp_path / "footprint_XAUUSD_5m.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")

    gc_cfg = {
        "footprint_ws": {
            "enabled": True,
            "gc_to_spot": {"enabled": True, "skip_main_csv": True, "skip_main_png": True},
        },
        "capture_plan": [
            {"symbol": "DXY", "intervals": ["15m"]},
            {"symbol": "XAUUSD", "intervals": ["15m", "5m"]},
        ],
    }
    payloads = ordered_chart_openai_payloads(tmp_path, stamp=stamp, gocharting_cfg=gc_cfg)
    from automation_tool.images import extend_openai_payloads_with_footprint_json

    payloads = extend_openai_payloads_with_footprint_json(
        payloads, tmp_path, gocharting_cfg=gc_cfg
    )
    names = [p.name for k, p in payloads if isinstance(p, Path)]
    assert not any(f"_gocharting_{sym}_" in n for n in names)
    assert "footprint_XAUUSD_15m.json" in names
    assert "footprint_XAUUSD_5m.json" in names


def test_persist_prepared_footprint_mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fp_dir = tmp_path / "footprint_images"
    fp_dir.mkdir()
    time_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    raw = {
        "symbol": "COMEX:GC1!",
        "candles": [
            {
                "time_gmt7": time_key,
                "ohlc": {"open": 4019.0, "high": 4020.0, "low": 4018.0, "close": 4019.0, "volume": 10},
                "footprint": [{"price": 4019.0, "buy": 2, "sell": 1}],
            }
        ],
    }
    import json

    (fp_dir / "footprint_combined_5m.json").write_text(json.dumps(raw), encoding="utf-8")
    stamp = "20260101_120000"
    csv_text = (
        "Date,Open,High,Low,Close,Volume,Delta,MaxDelta,MinDelta,CumDelta,BuyVolume,SellVolume,Vwap\n"
        f'"{time_key}","4019.0","4020.0","4018.0","4019.0","100","-5","1","-6","-10","40","45","4019.5"\n'
    )
    (tmp_path / f"{stamp}_gocharting_GC_5m.csv").write_text(csv_text, encoding="utf-8")
    (tmp_path / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")

    mt5 = _mt5_payload(time_key, 4023.5)

    def fake_resolve(**_kwargs):
        return mt5

    monkeypatch.setattr(
        "automation_tool.gocharting_gc_spot_convert.resolve_mt5_spot_payload",
        fake_resolve,
    )
    monkeypatch.setattr(
        "automation_tool.images.existing_footprint_combined_json_paths",
        lambda _charts_dir, **_: [fp_dir / "footprint_combined_5m.json"],
    )

    cfg = {
        "footprint_ws": {
            "enabled": True,
            "gc_to_spot": {"enabled": True, "min_matched_ratio": 0.8},
            "max_candles": 50,
            "block_multiplier": 1,
            "tick_size": 0.1,
        }
    }
    written = persist_prepared_footprint_json_files(
        tmp_path, chart_stamp=stamp, gocharting_cfg=cfg
    )
    assert len(written) == 1
    assert written[0].name == "footprint_XAUUSD_5m.json"
    paths = existing_prepared_footprint_json_paths(tmp_path)
    assert paths[0].name == "footprint_XAUUSD_5m.json"
