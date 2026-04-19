"""Bearer token cache helpers for Coinmap api_data_export bearer_request."""

from pathlib import Path

from automation_tool.coinmap import (
    CoinmapBearerCacheInvalid,
    _coinmap_bearer_shot_has_auth_failure,
    _coinmap_bearer_token_cache_resolved_path,
    _coinmap_read_bearer_token_cache,
    _coinmap_write_bearer_token_cache,
)
from automation_tool.config import default_coinmap_bearer_cache_path, _root


def test_bearer_shot_auth_failure_401_403_only() -> None:
    assert _coinmap_bearer_shot_has_auth_failure(
        {"getcandlehistory": {"status": 401, "ok": False}}
    )
    assert _coinmap_bearer_shot_has_auth_failure(
        {"getorderflowhistory": {"status": 403, "ok": False}}
    )
    assert not _coinmap_bearer_shot_has_auth_failure(
        {"getcandlehistory": {"status": 200, "ok": True, "body": {}}}
    )
    assert not _coinmap_bearer_shot_has_auth_failure(
        {"getcandlehistory": {"status": 500, "ok": False}}
    )


def test_bearer_token_cache_path_default() -> None:
    assert _coinmap_bearer_token_cache_resolved_path({}) == default_coinmap_bearer_cache_path()


def test_bearer_token_cache_path_relative_to_project() -> None:
    p = _coinmap_bearer_token_cache_resolved_path(
        {"bearer_token_cache_path": "data/custom_bearer.txt"}
    )
    assert p == _root() / "data" / "custom_bearer.txt"


def test_read_write_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "b.txt"
    _coinmap_write_bearer_token_cache(f, "Bearer test-token-xyz")
    assert _coinmap_read_bearer_token_cache(f) == "Bearer test-token-xyz"


def test_coinmap_bearer_cache_invalid_is_exception() -> None:
    assert issubclass(CoinmapBearerCacheInvalid, Exception)
