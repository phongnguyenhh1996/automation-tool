"""
Cấu hình nhiều tài khoản MT5 từ ``accounts.json`` (mảng object).

Đường dẫn mặc định: biến môi trường ``MT5_ACCOUNTS_JSON`` hoặc tham số CLI ``--mt5-accounts-json``.

**Lot:** bỏ key ``lot`` hoặc ``"lot": null`` → dùng khối lượng đã parse từ ``trade_line`` (cùng
hành vi ``mode: from_trade``). Có ``lot`` thì ``fixed`` / ``max_notional_usd`` như cũ.

**Bảo mật:** không commit file chứa mật khẩu; hạn chế quyền đọc (ví dụ ``chmod 600``).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Union

from automation_tool.mt5_openai_parse import ParsedTrade

LotMode = Literal["fixed", "max_notional_usd", "max_loss_usd", "from_trade"]


@dataclass(frozen=True)
class LotRuleFixed:
    mode: Literal["fixed"] = "fixed"
    volume: float = 0.01


@dataclass(frozen=True)
class LotRuleMaxNotionalUsd:
    mode: Literal["max_notional_usd"] = "max_notional_usd"
    max_usd: float = 100.0


@dataclass(frozen=True)
class LotRuleMaxLossUsd:
    """
    Tính volume sao cho thua lỗ tối đa khi chạm SL xấp xỉ ``max_usd``.

    Dựa vào ``mt5.order_calc_profit`` cho 1.0 lot (entry → SL), sau đó scale volume.
    """

    mode: Literal["max_loss_usd"] = "max_loss_usd"
    max_usd: float = 100.0


@dataclass(frozen=True)
class LotRuleFromTrade:
    """Dùng ``ParsedTrade.lot`` từ trade_line; không ghi đè trong ``execute_trade``."""

    mode: Literal["from_trade"] = "from_trade"


LotRule = Union[LotRuleFixed, LotRuleMaxNotionalUsd, LotRuleMaxLossUsd, LotRuleFromTrade]


@dataclass(frozen=True)
class MT5AccountEntry:
    """Một dòng trong ``accounts.json``."""

    id: str
    login: int
    password: str
    server: str
    primary: bool
    lot: LotRule
    #: Map symbol logic (XAUUSD, EURUSD, …) → tên đúng trên broker của acc đó (vd. XAUUSD vs XAUUSDm).
    symbol_map: dict[str, str] = field(default_factory=dict)


def _parse_symbol_map(obj: Any, index: int) -> dict[str, str]:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError(f"accounts[{index}].symbol_map phải là object hoặc bỏ qua")
    out: dict[str, str] = {}
    for k, v in obj.items():
        ks = str(k).strip().upper()
        vs = str(v).strip()
        if not ks or not vs:
            raise ValueError(
                f"accounts[{index}].symbol_map: mỗi key/value phải là chuỗi không rỗng"
            )
        out[ks] = vs
    return out


def _parse_lot(d: Any) -> LotRule:
    if not isinstance(d, dict):
        raise ValueError("lot phải là object")
    mode = str(d.get("mode") or "fixed").strip()
    if mode == "fixed":
        v = d.get("volume")
        if v is None:
            raise ValueError("lot.mode=fixed cần volume")
        return LotRuleFixed(volume=float(v))
    if mode == "max_notional_usd":
        m = d.get("max_usd")
        if m is None:
            raise ValueError("lot.mode=max_notional_usd cần max_usd")
        return LotRuleMaxNotionalUsd(max_usd=float(m))
    if mode == "max_loss_usd":
        m = d.get("max_usd")
        if m is None:
            raise ValueError("lot.mode=max_loss_usd cần max_usd")
        return LotRuleMaxLossUsd(max_usd=float(m))
    if mode == "from_trade":
        return LotRuleFromTrade()
    raise ValueError(f"lot.mode không hỗ trợ: {mode!r}")


def _parse_one(obj: Any, index: int) -> MT5AccountEntry:
    if not isinstance(obj, dict):
        raise ValueError(f"accounts[{index}] phải là object")
    acc_id = str(obj.get("id") or "").strip()
    if not acc_id:
        raise ValueError(f"accounts[{index}].id bắt buộc (chuỗi không rỗng)")
    login = obj.get("login")
    if login is None:
        raise ValueError(f"accounts[{index}].login bắt buộc")
    pw = obj.get("password")
    if pw is None or str(pw) == "":
        raise ValueError(f"accounts[{index}].password bắt buộc")
    server = str(obj.get("server") or "").strip()
    if not server:
        raise ValueError(f"accounts[{index}].server bắt buộc")
    primary = bool(obj.get("primary", False))
    lot_raw = obj.get("lot")
    if lot_raw is None:
        lot: LotRule = LotRuleFromTrade()
    else:
        lot = _parse_lot(lot_raw)
    sym_map = _parse_symbol_map(obj.get("symbol_map"), index)
    return MT5AccountEntry(
        id=acc_id,
        login=int(login),
        password=str(pw),
        server=server,
        primary=primary,
        lot=lot,
        symbol_map=sym_map,
    )


def load_mt5_accounts_from_path(path: Path) -> list[MT5AccountEntry]:
    """Đọc và validate mảng account; đúng một ``primary: true``."""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("accounts.json phải là mảng không rỗng")
    accounts = [_parse_one(x, i) for i, x in enumerate(data)]
    primaries = [a for a in accounts if a.primary]
    if len(primaries) != 1:
        raise ValueError(
            f"Cần đúng 1 tài khoản primary=true, hiện có {len(primaries)}"
        )
    ids = [a.id for a in accounts]
    if len(set(ids)) != len(ids):
        raise ValueError("id các tài khoản phải khác nhau")
    return accounts


def default_mt5_accounts_json_path() -> Optional[Path]:
    raw = (os.getenv("MT5_ACCOUNTS_JSON") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def load_mt5_accounts_optional(path: Optional[Path] = None) -> Optional[list[MT5AccountEntry]]:
    """Trả về ``None`` nếu không có file / không set env (single-account)."""
    p = path or default_mt5_accounts_json_path()
    if p is None or not p.is_file():
        return None
    return load_mt5_accounts_from_path(p)


def resolve_mt5_accounts_path(cli_path: Optional[Path]) -> Optional[Path]:
    """Ưu tiên đường dẫn CLI; không thì ``MT5_ACCOUNTS_JSON``."""
    return cli_path if cli_path is not None else default_mt5_accounts_json_path()


def load_mt5_accounts_for_cli(cli_path: Optional[Path]) -> Optional[list[MT5AccountEntry]]:
    """Tiện ích cho CLI / params: một đường dẫn optional + env."""
    return load_mt5_accounts_optional(resolve_mt5_accounts_path(cli_path))


def primary_account(accounts: list[MT5AccountEntry]) -> MT5AccountEntry:
    for a in accounts:
        if a.primary:
            return a
    raise RuntimeError("internal: no primary")


def primary_account_id(accounts: list[MT5AccountEntry]) -> str:
    return primary_account(accounts).id


def reference_price_for_lot(
    mt5: Any,
    sym: str,
    trade: ParsedTrade,
) -> tuple[float, Optional[str]]:
    """
    Giá dùng tính notional: pending dùng ``trade.price``; market dùng bid/ask theo side.
    """
    if trade.kind != "MARKET" and trade.price is not None:
        return float(trade.price), None
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return 0.0, f"symbol_info_tick({sym!r}) None"
    if trade.side == "BUY":
        return float(tick.ask), None
    return float(tick.bid), None


def _round_volume_to_step(vol: float, step: float, vol_min: float, vol_max: float) -> float:
    if step <= 0:
        step = 0.01
    # floor to step
    n = math.floor(vol / step + 1e-12)
    out = n * step
    if out < vol_min - 1e-12:
        out = vol_min
    if out > vol_max + 1e-12:
        out = vol_max
    # Lot chỉ làm tròn tới 2 chữ số thập phân (0.01 lot precision).
    return round(out, 2)


def compute_lot_override(
    trade: ParsedTrade,
    rule: LotRule,
    *,
    mt5: Any,
    resolved_symbol: str,
    dry_run: bool,
) -> tuple[float, Optional[str]]:
    """
    Trả về (volume, warning_or_none).

    ``max_notional_usd``: ``volume ≈ max_usd / (contract_size * price)`` (ký quỹ kiểu USD
    cho nhiều CFD/metal; broker khác nhau có thể cần chỉnh sau).

    ``max_loss_usd``: ``volume ≈ max_usd / loss_per_1lot`` trong đó ``loss_per_1lot`` lấy từ
    ``mt5.order_calc_profit`` với volume=1.0 (entry → SL). Thường yêu cầu ``trade.sl``.
    """
    if isinstance(rule, LotRuleFixed):
        return float(rule.volume), None

    if isinstance(rule, LotRuleFromTrade):
        return float(trade.lot), None

    if isinstance(rule, LotRuleMaxNotionalUsd):
        if dry_run:
            # Không có terminal: dùng lot từ trade_line làm mô phỏng
            return float(trade.lot), "[dry-run] max_notional_usd → dùng lot từ trade_line"

        info = mt5.symbol_info(resolved_symbol)
        if info is None:
            return float(trade.lot), f"symbol_info({resolved_symbol!r}) None — dùng lot từ trade_line"

        cs = float(getattr(info, "trade_contract_size", 0) or 0)
        if cs <= 0:
            return float(trade.lot), "trade_contract_size=0 — dùng lot từ trade_line"

        px, err = reference_price_for_lot(mt5, resolved_symbol, trade)
        if err or px <= 0:
            return float(trade.lot), (err or "price=0") + " — dùng lot từ trade_line"

        # Notional (quote USD cho đa số symbol USD-denominated) ≈ volume * contract_size * price
        denom = cs * px
        if denom <= 0:
            return float(trade.lot), "denom<=0 — dùng lot từ trade_line"

        raw_vol = float(rule.max_usd) / denom
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
        vmax = float(getattr(info, "volume_max", 100.0) or 100.0)
        vol = _round_volume_to_step(raw_vol, step, vmin, vmax)
        hint = f"max_notional_usd={rule.max_usd} contract={cs} price={px:.5f} → vol={vol}"
        return vol, hint

    if isinstance(rule, LotRuleMaxLossUsd):
        if dry_run:
            return float(trade.lot), "[dry-run] max_loss_usd → dùng lot từ trade_line"

        if trade.sl is None:
            return float(trade.lot), "trade.sl=None — dùng lot từ trade_line"

        info = mt5.symbol_info(resolved_symbol)
        if info is None:
            return float(trade.lot), f"symbol_info({resolved_symbol!r}) None — dùng lot từ trade_line"

        entry_px, err = reference_price_for_lot(mt5, resolved_symbol, trade)
        if err or entry_px <= 0:
            return float(trade.lot), (err or "price=0") + " — dùng lot từ trade_line"

        try:
            order_type = mt5.ORDER_TYPE_BUY if trade.side == "BUY" else mt5.ORDER_TYPE_SELL
        except Exception:
            order_type = 0

        # Lấy lỗ/lãi cho 1.0 lot từ entry → SL.
        try:
            pl_1lot = mt5.order_calc_profit(order_type, resolved_symbol, 1.0, entry_px, float(trade.sl))
        except Exception as e:
            return float(trade.lot), f"order_calc_profit lỗi: {e!r} — dùng lot từ trade_line"

        if pl_1lot is None:
            return float(trade.lot), "order_calc_profit=None — dùng lot từ trade_line"

        loss_1lot = abs(float(pl_1lot))
        if loss_1lot <= 0:
            return float(trade.lot), f"loss_1lot<=0 (pl_1lot={pl_1lot}) — dùng lot từ trade_line"

        raw_vol = float(rule.max_usd) / loss_1lot
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
        vmax = float(getattr(info, "volume_max", 100.0) or 100.0)
        vol = _round_volume_to_step(raw_vol, step, vmin, vmax)
        hint = (
            f"max_loss_usd={rule.max_usd} entry={entry_px:.5f} sl={float(trade.sl):.5f} "
            f"loss_1lot≈{loss_1lot:.5f} → vol={vol}"
        )
        return vol, hint

    return float(trade.lot), f"lot rule không rõ: {rule!r}"



def compute_volume_for_max_notional_live(
    trade: ParsedTrade,
    rule: LotRuleMaxNotionalUsd,
    *,
    login: int,
    password: str,
    server: str,
    symbol_override: Optional[str],
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[float, Optional[str]]:
    """
    Một lần ``initialize`` + tính volume (rồi caller gọi ``execute_trade`` sẽ init lại).
    Dùng cho multi-account khi ``mode=max_notional_usd`` và không dry-run.
    """
    from automation_tool.mt5_execute import (  # noqa: WPS433 — tránh vòng import tĩnh
        _load_mt5,
        format_last_error,
        resolve_trade_symbol_on_broker,
    )

    mt5 = _load_mt5()
    kwargs: dict[str, Any] = {}
    if login and password and server:
        kwargs["login"] = login
        kwargs["password"] = password
        kwargs["server"] = server
    if not mt5.initialize(**kwargs):
        return float(trade.lot), f"mt5.initialize thất bại: {format_last_error(mt5)}"
    try:
        rt, err = resolve_trade_symbol_on_broker(
            mt5,
            trade,
            symbol_override,
            account_symbol_map=account_symbol_map,
        )
        if err or rt is None:
            return float(trade.lot), err
        return compute_lot_override(
            rt,
            rule,
            mt5=mt5,
            resolved_symbol=rt.symbol,
            dry_run=False,
        )
    finally:
        mt5.shutdown()


def compute_volume_for_max_loss_live(
    trade: ParsedTrade,
    rule: LotRuleMaxLossUsd,
    *,
    login: int,
    password: str,
    server: str,
    symbol_override: Optional[str],
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[float, Optional[str]]:
    """
    Một lần ``initialize`` + tính volume theo max loss (rồi caller gọi ``execute_trade`` sẽ init lại).
    Dùng cho multi-account khi ``mode=max_loss_usd`` và không dry-run.
    """
    from automation_tool.mt5_execute import (  # noqa: WPS433 — tránh vòng import tĩnh
        _load_mt5,
        format_last_error,
        resolve_trade_symbol_on_broker,
    )

    mt5 = _load_mt5()
    kwargs: dict[str, Any] = {}
    if login and password and server:
        kwargs["login"] = login
        kwargs["password"] = password
        kwargs["server"] = server
    if not mt5.initialize(**kwargs):
        return float(trade.lot), f"mt5.initialize thất bại: {format_last_error(mt5)}"
    try:
        rt, err = resolve_trade_symbol_on_broker(
            mt5,
            trade,
            symbol_override,
            account_symbol_map=account_symbol_map,
        )
        if err or rt is None:
            return float(trade.lot), err
        return compute_lot_override(
            rt,
            rule,
            mt5=mt5,
            resolved_symbol=rt.symbol,
            dry_run=False,
        )
    finally:
        mt5.shutdown()
