"""
Cấu hình nhiều tài khoản MT5 từ ``accounts.json`` (mảng object).

Đường dẫn mặc định: biến môi trường ``MT5_ACCOUNTS_JSON`` hoặc tham số CLI ``--mt5-accounts-json``.

**Lot:** bỏ key ``lot`` hoặc ``"lot": null`` → dùng khối lượng đã parse từ ``trade_line`` (cùng
hành vi ``mode: from_trade``). Có ``lot`` thì ``fixed`` / ``max_notional_usd`` như cũ.

**Entry TP:** bỏ key ``entry_take_profit`` → giữ hành vi cũ là đặt TP2 nếu trade có TP2;
đặt ``"entry_take_profit": "tp1"`` để account đó chốt TP ở TP1 ngay khi mở lệnh.

**update-scalp:** optional ``\"update-scalp\": true`` trên từng object — :func:`sync_accounts_scalp_json`
lọc ra ``accounts-scalp.json`` cho luồng ``coinmap-automation update-scalp``.
``daemon-plan`` khi vào lệnh zone ``source=update-scalp`` dùng :func:`load_mt5_accounts_for_zone_entry`
để chỉ đọc ``accounts-scalp.json`` (không fallback full list).

**Bảo mật:** không commit file chứa mật khẩu; hạn chế quyền đọc (ví dụ ``chmod 600``).
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Union

_log = logging.getLogger(__name__)

SOURCE_UPDATE_SCALP = "update-scalp"

from automation_tool.mt5_openai_parse import ParsedTrade

LotMode = Literal["fixed", "max_notional_usd", "max_loss_usd", "from_trade"]
EntryTakeProfitTarget = Literal["tp1", "tp2"]
EntrySlot = Literal["sang", "chieu", "toi"]


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
    #: Bắt buộc: đường dẫn metatrader.exe/metatrader64.exe cho account này.
    terminal_path: str
    login: int
    password: str
    server: str
    primary: bool
    lot: LotRule
    #: TP đặt trên lệnh khi mở qua MT5. MT5 chỉ có 1 TP; mặc định giữ hành vi cũ là TP2.
    entry_take_profit: EntryTakeProfitTarget = "tp2"
    #: Map symbol logic (XAUUSD, EURUSD, …) → tên đúng trên broker của acc đó (vd. XAUUSD vs XAUUSDm).
    symbol_map: dict[str, str] = field(default_factory=dict)
    #: Chỉ cho account này vào lệnh ở các shard ``*_sang/_chieu/_toi`` được liệt kê.
    #: ``None`` = không giới hạn, giữ hành vi cũ là vào cả ba khung.
    entry_slots: Optional[tuple[EntrySlot, ...]] = None


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


def _parse_entry_take_profit(obj: Any, index: int) -> EntryTakeProfitTarget:
    raw = str(obj or "tp2").strip().lower()
    if raw in ("tp1", "tp2"):
        return raw  # type: ignore[return-value]
    raise ValueError(f"accounts[{index}].entry_take_profit phải là 'tp1' hoặc 'tp2'")


def _parse_entry_slots(obj: Any, index: int) -> Optional[tuple[EntrySlot, ...]]:
    if obj is None:
        return None
    if not isinstance(obj, list):
        raise ValueError(f"accounts[{index}].entry_slots phải là mảng ['sang','chieu','toi'] hoặc bỏ qua")
    out: list[EntrySlot] = []
    seen: set[str] = set()
    for raw in obj:
        slot = str(raw or "").strip().lower()
        if slot not in ("sang", "chieu", "toi"):
            raise ValueError(
                f"accounts[{index}].entry_slots chỉ hỗ trợ 'sang', 'chieu', 'toi'; gặp {raw!r}"
            )
        if slot in seen:
            continue
        seen.add(slot)
        out.append(slot)  # type: ignore[arg-type]
    if not out:
        raise ValueError(f"accounts[{index}].entry_slots không được rỗng")
    return tuple(out)


def _parse_one(obj: Any, index: int) -> MT5AccountEntry:
    if not isinstance(obj, dict):
        raise ValueError(f"accounts[{index}] phải là object")
    acc_id = str(obj.get("id") or "").strip()
    if not acc_id:
        raise ValueError(f"accounts[{index}].id bắt buộc (chuỗi không rỗng)")
    terminal_path = obj.get("terminal_path")
    if terminal_path is None:
        raise ValueError(f"accounts[{index}].terminal_path bắt buộc")
    if not isinstance(terminal_path, str):
        raise ValueError(f"accounts[{index}].terminal_path phải là chuỗi")
    terminal_path_s = terminal_path.strip()
    if not terminal_path_s:
        raise ValueError(f"accounts[{index}].terminal_path không được rỗng")
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
    entry_tp = _parse_entry_take_profit(obj.get("entry_take_profit"), index)
    entry_slots = _parse_entry_slots(obj.get("entry_slots"), index)
    sym_map = _parse_symbol_map(obj.get("symbol_map"), index)
    return MT5AccountEntry(
        id=acc_id,
        terminal_path=terminal_path_s,
        login=int(login),
        password=str(pw),
        server=server,
        primary=primary,
        lot=lot,
        entry_take_profit=entry_tp,
        entry_slots=entry_slots,
        symbol_map=sym_map,
    )


def mt5_account_allows_entry_slot(acc: MT5AccountEntry, slot: Optional[str]) -> bool:
    """Return whether ``acc`` may open a new trade in ``slot``; missing config means all slots."""
    allowed = acc.entry_slots
    if allowed is None:
        return True
    s = str(slot or "").strip().lower()
    if s not in ("sang", "chieu", "toi"):
        return True
    return s in allowed


def filter_mt5_accounts_for_entry_slot(
    accounts: list[MT5AccountEntry],
    slot: Optional[str],
) -> list[MT5AccountEntry]:
    """Filter multi-account entries by ``entry_slots`` for new order placement."""
    return [a for a in accounts if mt5_account_allows_entry_slot(a, slot)]


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


def sync_accounts_scalp_json(
    source_accounts_json: Path,
    *,
    dest_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    Tạo (hoặc xoá) ``accounts-scalp.json`` cạnh ``accounts.json``: chỉ giữ object có
    ``\"update-scalp\": true`` (đúng literal ``True`` trong JSON).     Key ``update-scalp`` không
    ghi vào file đích. Nếu sau lọc không có dòng nào ``primary: true``, gán
    ``primary: true`` cho phần tử đầu (các dòng còn lại ``false``) để subset vẫn hợp lệ
    khi tài khoản primary toàn cục không tham gia scalp. Validate giống
    :func:`load_mt5_accounts_from_path`.

    Trả về đường dẫn tuyệt đối file đích nếu đã ghi và hợp lệ; ``None`` nếu không có
    tài khoản nào được đánh dấu (file đích bị xoá nếu tồn tại).
    """
    src = source_accounts_json.expanduser()
    if not src.is_file():
        return None
    raw = src.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("accounts.json phải là mảng")
    out_rows: list[dict[str, Any]] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"accounts[{i}] phải là object")
        if row.get("update-scalp") is not True:
            continue
        cleaned = {k: v for k, v in row.items() if k != "update-scalp"}
        out_rows.append(cleaned)
    dest = (dest_path or (src.parent / "accounts-scalp.json")).expanduser()
    if not out_rows:
        try:
            if dest.is_file():
                dest.unlink()
        except OSError:
            pass
        return None
    n_primary = sum(1 for r in out_rows if r.get("primary") is True)
    if n_primary == 0:
        for i, r in enumerate(out_rows):
            r["primary"] = i == 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(out_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    load_mt5_accounts_from_path(dest)
    return dest.resolve()


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


def _is_update_scalp_zone_source(zone_source: str) -> bool:
    return (zone_source or "").strip().lower() == SOURCE_UPDATE_SCALP


def load_mt5_accounts_for_zone_entry(
    *,
    zone_source: str,
    cli_path: Optional[Path],
) -> Optional[list[MT5AccountEntry]]:
    """
    Đọc danh sách account để **đặt lệnh** theo ``Zone.source``.

    Với ``source`` = ``update-scalp``: chỉ dùng ``accounts-scalp.json`` cùng thư mục với file
    accounts đã resolve (giống :func:`sync_accounts_scalp_json`). Nếu đường dẫn hiện tại đã là
    ``accounts-scalp.json`` thì giữ nguyên.

    Giá trị trả về:

    - ``None`` — không có file accounts (chế độ một terminal / ``execute_trade`` đơn).
    - ``[]`` — zone ``update-scalp`` nhưng không có hoặc không đọc được ``accounts-scalp.json``:
      **không** được fallback sang full ``accounts.json``.
    - Danh sách khác rỗng — multi-account như cũ.
    """
    if not _is_update_scalp_zone_source(zone_source):
        return load_mt5_accounts_for_cli(cli_path)

    base = resolve_mt5_accounts_path(cli_path)
    if base is None or not base.is_file():
        return load_mt5_accounts_for_cli(cli_path)

    path_to_load = (
        base if base.name.lower() == "accounts-scalp.json" else base.parent / "accounts-scalp.json"
    )
    if not path_to_load.is_file():
        return []

    try:
        return load_mt5_accounts_from_path(path_to_load)
    except ValueError as e:
        _log.warning(
            "load_mt5_accounts_for_zone_entry: invalid %s: %s",
            path_to_load,
            e,
        )
        return []


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
    terminal_path: str,
    login: int,
    password: str,
    server: str,
    symbol_override: Optional[str],
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[float, Optional[str]]:
    """
    Đảm bảo phiên MT5 đúng terminal/account rồi tính volume, không đóng phiên sau khi đọc.
    Dùng cho multi-account khi ``mode=max_notional_usd`` và không dry-run.
    """
    from automation_tool.mt5_execute import (  # noqa: WPS433 — tránh vòng import tĩnh
        ensure_mt5_session,
        resolve_trade_symbol_on_broker,
    )

    term_path = (terminal_path or "").strip()
    if not term_path:
        return float(trade.lot), "terminal_path rỗng — không thể initialize MT5 terminal"
    session = ensure_mt5_session(
        terminal_path=term_path,
        login=login if login else None,
        password=password,
        server=server,
    )
    if not session.ok:
        return float(trade.lot), session.message
    mt5 = session.mt5
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


def compute_volume_for_max_loss_live(
    trade: ParsedTrade,
    rule: LotRuleMaxLossUsd,
    *,
    terminal_path: str,
    login: int,
    password: str,
    server: str,
    symbol_override: Optional[str],
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[float, Optional[str]]:
    """
    Đảm bảo phiên MT5 đúng terminal/account rồi tính volume theo max loss, không đóng phiên sau khi đọc.
    Dùng cho multi-account khi ``mode=max_loss_usd`` và không dry-run.
    """
    from automation_tool.mt5_execute import (  # noqa: WPS433 — tránh vòng import tĩnh
        ensure_mt5_session,
        resolve_trade_symbol_on_broker,
    )

    term_path = (terminal_path or "").strip()
    if not term_path:
        return float(trade.lot), "terminal_path rỗng — không thể initialize MT5 terminal"
    session = ensure_mt5_session(
        terminal_path=term_path,
        login=login if login else None,
        password=password,
        server=server,
    )
    if not session.ok:
        return float(trade.lot), session.message
    mt5 = session.mt5
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
