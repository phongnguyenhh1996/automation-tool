"""Persistence for Responses thread id and zone price JSON files under data/."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from automation_tool.config import default_data_dir


def default_last_response_id_path() -> Path:
    return default_data_dir() / "last_response_id.txt"


def default_morning_baseline_prices_path() -> Path:
    return default_data_dir() / "morning_baseline_prices.json"


def default_last_alert_prices_path() -> Path:
    return default_data_dir() / "last_alert_prices.json"


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: Any) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, raw)


def read_last_response_id(path: Optional[Path] = None) -> Optional[str]:
    p = path or default_last_response_id_path()
    if not p.is_file():
        return None
    line = p.read_text(encoding="utf-8").strip()
    return line or None


def write_last_response_id(response_id: str, path: Optional[Path] = None) -> None:
    p = path or default_last_response_id_path()
    _atomic_write_text(p, response_id.strip() + "\n")


@dataclass(frozen=True)
class MorningBaselinePrices:
    prices: tuple[float, float, float]
    labels: tuple[str, str, str] = ("plan_chinh", "plan_phu", "scalp")
    updated_at: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "prices": list(self.prices),
            "labels": list(self.labels),
            "updated_at": self.updated_at or datetime.now(timezone.utc).isoformat(),
        }


def read_morning_baseline_prices(path: Optional[Path] = None) -> Optional[MorningBaselinePrices]:
    p = path or default_morning_baseline_prices_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    prices = data.get("prices")
    if not isinstance(prices, list) or len(prices) != 3:
        return None
    try:
        tup = tuple(float(x) for x in prices)
    except (TypeError, ValueError):
        return None
    labels = data.get("labels")
    if isinstance(labels, list) and len(labels) == 3:
        lab = tuple(str(x) for x in labels)
    else:
        lab = ("plan_chinh", "plan_phu", "scalp")
    ts = str(data.get("updated_at") or "")
    return MorningBaselinePrices(prices=(tup[0], tup[1], tup[2]), labels=lab, updated_at=ts)


def write_morning_baseline_prices(
    prices: tuple[float, float, float],
    path: Optional[Path] = None,
) -> None:
    p = path or default_morning_baseline_prices_path()
    mb = MorningBaselinePrices(prices=prices)
    _atomic_write_json(p, mb.to_json_dict())


def read_last_alert_prices(path: Optional[Path] = None) -> Optional[tuple[float, float, float]]:
    p = path or default_last_alert_prices_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    prices = data.get("prices")
    if not isinstance(prices, list) or len(prices) != 3:
        return None
    try:
        return (float(prices[0]), float(prices[1]), float(prices[2]))
    except (TypeError, ValueError):
        return None


def write_last_alert_prices(
    prices: tuple[float, float, float],
    path: Optional[Path] = None,
) -> None:
    p = path or default_last_alert_prices_path()
    _atomic_write_json(
        p,
        {
            "prices": list(prices),
            "labels": ["plan_chinh", "plan_phu", "scalp"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
