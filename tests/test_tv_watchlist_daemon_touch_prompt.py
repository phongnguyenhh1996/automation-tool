from __future__ import annotations

from automation_tool.tv_watchlist_daemon import _touch_prompt
from automation_tool.zones_state import Zone


def test_touch_prompt_includes_trigger_price_and_anti_chase_rule() -> None:
    zone = Zone(
        id="z-sell",
        label="plan_phu",
        vung_cho="4549.5–4552.5",
        side="SELL",
        trade_line="SELL LIMIT 4551.0 | SL 4560.0 | TP1 4538.0 | Lot 0.01",
    )

    prompt = _touch_prompt(zone=zone, last_price=4551.76)

    assert "Giá trigger realtime khi chạm vùng: 4551.76." in prompt
    assert 'intraday_hanh_dong="chờ"' in prompt
