# MQL5 Trade Copier – Manual Test Checklist

Files:
- `mql5/Experts/PrimaryPublisher.mq5` (run on Primary terminal)
- `mql5/Experts/SlaveFollower.mq5` (run on each Slave terminal)

## Setup
- Start **two MT5 terminals** on the same machine/VPS (so they share MT5 **Common Files**).
- Compile both EAs in MetaEditor and attach:
  - Primary: attach `PrimaryPublisher` to any chart.
  - Slave: attach `SlaveFollower` to any chart.
- Ensure both use the same `InpChannel` value (default `default`).

Optional symbol mapping on Slave:
- If broker symbols differ (suffix/prefix), set:
  - `InpSymbolPrefix` / `InpSymbolSuffix`, or
  - `InpSymbolMapFile` pointing to a Common file like `trade_copier_default_symbol_map.csv` with lines: `EURUSD=EURUSD.a`

## 1) Market order copy (OPEN)
- On primary: place a **BUY market** order.
- Expect on slave:
  - A position opens for the mapped symbol.
  - Position comment starts with `COPIER:<primary_login>:POS:<primary_position_id>`.

Repeat for **SELL market**.

## 2) Modify SL/TP (MODIFY / trailing)
- On primary: set SL/TP on the open position, then move SL a few times (simulate trailing).
- Expect on slave:
  - SL/TP updates to match primary.
  - No “duplicate positions” are created.

## 3) Partial close (PARTIAL)
- On primary: close **50% volume** of the position.
- Expect on slave:
  - Position volume decreases by the same closed lots (or best-effort if broker min lot differs).

## 4) Full close (CLOSE)
- On primary: close the remaining volume.
- Expect on slave:
  - The copier position closes.

## 5) Pending orders copy (OPEN pending)
- On primary: place:
  - Buy Limit / Sell Limit
  - Buy Stop / Sell Stop
- Expect on slave:
  - Corresponding pending order is created.
  - Order comment starts with `COPIER:<primary_login>:ORD:<primary_ticket>`.

## 6) Modify pending (MODIFY pending)
- On primary: modify pending order price + SL/TP.
- Expect on slave:
  - Pending order is modified accordingly.

## 7) Cancel pending (CANCEL)
- On primary: delete the pending order.
- Expect on slave:
  - Pending order is deleted.

## 8) Snapshot-based recovery (SNAP_* reconciliation)
Purpose: prove resilience if slave misses events.

- On slave: temporarily remove EA from chart (or disable AutoTrading) for ~15–30s.
- On primary: open a market order and/or create a pending order, then modify SL/TP.
- Re-enable slave EA.
- Expect on slave:
  - Within `InpSnapshotSeconds` (primary), slave reconstructs missing positions/orders from `SNAP_POS` / `SNAP_ORD`.
  - Any copier orders/positions that **no longer exist** on primary are cleaned up on `SNAP_END`.

## Notes / common issues
- Verified (manual): Python MetaTrader5 integration can run **two MT5 terminals in parallel** on the same Windows VPS **when each account uses its own `terminal_path`** (per-account EXE path). This avoids terminals “fighting” over a single session.
- If primary & slave are **not** on the same machine (no shared Common Files), file-based copier won’t work; switch to TCP/HTTP relay.
- If broker has different min lot / step, partial-close might need rounding (future enhancement).
- If symbol doesn’t exist on slave broker, it will log and skip that event.

