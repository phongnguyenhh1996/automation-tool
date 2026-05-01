from __future__ import annotations

from types import SimpleNamespace

from automation_tool.openai_agents import AgentRole, resolve_vector_store_ids_for_role


def _settings(
    *,
    map_path,
    auto_create: bool = True,
    full: list[str] | None = None,
    alert: list[str] | None = None,
    update: list[str] | None = None,
    manage: list[str] | None = None,
    retro: list[str] | None = None,
):
    return SimpleNamespace(
        openai_vector_store_ids_full_analysis=list(full or []),
        openai_vector_store_ids_intraday_alert=list(alert or []),
        openai_vector_store_ids_intraday_update=list(update or []),
        openai_vector_store_ids_trade_management=list(manage or []),
        openai_vector_store_ids_retrospective=list(retro or []),
        openai_agent_vector_stores_map_path=map_path,
        openai_auto_create_agent_vector_store=auto_create,
    )


def test_vector_store_env_ids_take_priority(tmp_path) -> None:
    map_path = tmp_path / "agent_vs_map.json"
    map_path.write_text('{"intraday_alert":"vs_from_map"}', encoding="utf-8")
    s = _settings(map_path=map_path, alert=["vs_from_env"])

    ids = resolve_vector_store_ids_for_role(
        settings=s,
        role=AgentRole.INTRADAY_ALERT,
        api_key="dummy",
    )
    assert ids == ["vs_from_env"]


def test_vector_store_uses_mapping_before_autocreate(monkeypatch, tmp_path) -> None:
    map_path = tmp_path / "agent_vs_map.json"
    map_path.write_text('{"trade_management":"vs_from_map"}', encoding="utf-8")
    s = _settings(map_path=map_path, manage=[])

    def _should_not_create(*_a, **_k):
        raise AssertionError("auto-create should not run when mapping already exists")

    monkeypatch.setattr("automation_tool.openai_agents._create_vector_store_for_role", _should_not_create)

    ids = resolve_vector_store_ids_for_role(
        settings=s,
        role=AgentRole.TRADE_MANAGEMENT,
        api_key="dummy",
    )
    assert ids == ["vs_from_map"]


def test_vector_store_autocreate_is_idempotent(monkeypatch, tmp_path) -> None:
    map_path = tmp_path / "agent_vs_map.json"
    s = _settings(map_path=map_path, full=[])
    calls = {"n": 0}

    def _fake_create(_api_key: str, _role: AgentRole) -> str:
        calls["n"] += 1
        return "vs_created_once"

    monkeypatch.setattr("automation_tool.openai_agents._create_vector_store_for_role", _fake_create)

    first = resolve_vector_store_ids_for_role(
        settings=s,
        role=AgentRole.FULL_ANALYSIS,
        api_key="dummy",
    )
    second = resolve_vector_store_ids_for_role(
        settings=s,
        role=AgentRole.FULL_ANALYSIS,
        api_key="dummy",
    )

    assert first == ["vs_created_once"]
    assert second == ["vs_created_once"]
    assert calls["n"] == 1
