from pathlib import Path


EA_SOURCE = Path(__file__).resolve().parents[1] / "EA_Zone_NeverDie.mq5"


def _source() -> str:
    return EA_SOURCE.read_text()


def _function_body(source: str, name: str) -> str:
    marker = f"void {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0

    for pos in range(brace_start, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start : pos + 1]

    raise AssertionError(f"Could not parse function body for {name}")


def test_ea_no_longer_blocks_trade_sides() -> None:
    calls = [
        line.strip()
        for line in _source().splitlines()
        if "BlockSide(" in line and not line.strip().startswith("void BlockSide(")
    ]

    assert calls == []


def test_watch_zone_activation_allows_only_one_trade_zone_globally() -> None:
    source = _source()
    activate_body = _function_body(source, "ActivateWatchZones")
    single_activate_body = _function_body(source, "ActivateSingleWatchZone")

    assert "ActivateSingleWatchZone" in activate_body
    assert "g_buyZones[i].mode = ZONE_WATCH" in activate_body
    assert "g_sellZones[i].mode = ZONE_WATCH" in activate_body
    assert "mode = ZONE_TRADE" in single_activate_body
    assert "ActivateSideWatchZones(" not in source
