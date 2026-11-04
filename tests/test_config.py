from __future__ import annotations

import pytest

from mapsql_mcp.cli import build_parser, settings_from_args
from mapsql_mcp.config import Settings
from mapsql_mcp.errors import MapsqlError


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@h/db")
    monkeypatch.setenv("MAPSQL_SCHEMAS", "public, osm ,")
    monkeypatch.setenv("MAPSQL_LAYERS", "osm.*,cities")
    monkeypatch.setenv("MAPSQL_MAX_ROWS", "250")
    monkeypatch.setenv("MAPSQL_DEFAULT_LIMIT", "1000")
    monkeypatch.setenv("MAPSQL_STATEMENT_TIMEOUT_MS", "2500")
    s = Settings.from_env()
    assert s.schemas == ("public", "osm") and s.layers == ("osm.*", "cities")
    assert s.max_rows == 250 and s.default_limit == 250 and s.statement_timeout_ms == 2500
    assert not s.allow_write and s.restricted


def test_bad_env_values(monkeypatch):
    monkeypatch.setenv("MAPSQL_MAX_ROWS", "lots")
    with pytest.raises(MapsqlError):
        Settings.from_env()


def test_missing_database_url_message(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(MapsqlError, match="DATABASE_URL"):
        Settings.from_env().require_database_url()


def test_cli_overrides(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    args = build_parser().parse_args(["--allow-write", "--schemas", "a,b", "--no-basemap", "--max-rows", "10"])
    s = settings_from_args(args)
    assert s.allow_write and s.schemas == ("a", "b") and s.basemap == "none" and s.max_rows == 10


def test_defaults_are_safe(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    for k in ("MAPSQL_ALLOW_WRITE", "MAPSQL_SCHEMAS", "MAPSQL_LAYERS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.allow_write is False and s.statement_timeout_ms <= 30000 and s.max_rows <= 10000


def test_open_maps_flag(monkeypatch):
    monkeypatch.delenv("MAPSQL_OPEN_MAPS", raising=False)
    assert not Settings.from_env().open_maps
    monkeypatch.setenv("MAPSQL_OPEN_MAPS", "1")
    assert Settings.from_env().open_maps
    monkeypatch.delenv("MAPSQL_OPEN_MAPS")
    assert settings_from_args(build_parser().parse_args(["--open-maps"])).open_maps


def test_open_in_viewer_never_raises(monkeypatch):
    from mapsql_mcp import server

    assert server._open_in_viewer(None) is False

    def boom(*a, **k):
        raise OSError("no viewer")

    monkeypatch.setattr(server.os, "startfile", boom, raising=False)
    monkeypatch.setattr(server.subprocess, "Popen", boom)
    assert server._open_in_viewer("x.png") is False
