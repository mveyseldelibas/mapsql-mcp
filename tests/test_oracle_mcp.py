"""End-to-end for the Oracle backend: spawn the real server over stdio and talk to it with the official MCP client SDK."""

from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import oracle_env

pytestmark = pytest.mark.oracle

EXPECTED_TOOLS = {
    "server_info", "list_layers", "describe_layer", "sample_features", "find_features", "within_distance", "nearest",
    "buffer", "spatial_filter", "bbox_of", "aggregate", "spatial_join", "run_sql", "render_map",
}


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


def _is_error(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


@pytest.fixture()
def params(oracle_svc, oracle_creds, tmp_path):
    env = {**os.environ, **oracle_env(oracle_creds), "MAPSQL_SCHEMAS": "MAPSQL_DEMO"}
    env.pop("MAPSQL_BACKEND", None)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "mapsql_mcp", "--no-basemap", "--output-dir", str(tmp_path)], env=env
    )


async def test_oracle_stdio_end_to_end(params, tmp_path) -> None:
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert "Ask, do not guess" in (init.instructions or "")
            assert "Oracle Spatial" in init.instructions and "FETCH FIRST" in init.instructions

            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert EXPECTED_TOOLS <= set(tools) and "execute_sql" not in tools
            assert "Oracle" in tools["run_sql"].description

            res = await s.call_tool("server_info", {})
            info = json.loads(_text(res))
            assert info["backend"] == "oracle" and info["mode"] == "read-only"

            res = await s.call_tool("list_layers", {})
            layers = json.loads(_text(res))
            assert any(l["layer"] == "mapsql_demo.ankara_pois" for l in layers["layers"])

            res = await s.call_tool(
                "within_distance",
                {"layer": "ankara_pois", "distance_m": 500, "lon": 32.8541, "lat": 39.9208,
                 "filters": [{"column": "kind", "value": "pharmacy"}], "columns": ["name"], "limit": 100},
            )
            assert not _is_error(res)
            data = json.loads(_text(res))
            assert data["count"] == 20

            res = await s.call_tool("render_map", {"result_ids": [data["result_id"]], "title": "oracle smoke", "save_html": True})
            assert not _is_error(res)
            assert [c.type for c in res.content][0] == "image"
            summary = json.loads(res.content[-1].text)
            with open(summary["png_path"], "rb") as fh:
                assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
            assert os.path.exists(summary["html_path"])

            # choropleth: cafes per neighbourhood
            res = await s.call_tool(
                "spatial_join",
                {"left_layer": "ankara_neighbourhoods", "right_layer": "ankara_pois", "relation": "contains",
                 "right_filters": [{"column": "kind", "value": "cafe"}], "limit": 200},
            )
            sj = json.loads(_text(res))
            res = await s.call_tool("render_map", {"result_ids": [sj["result_id"]], "value_property": "right_count",
                                                   "label_property": "name", "title": "cafes per neighbourhood"})
            assert not _is_error(res) and res.content[0].type == "image"

            # safety over the wire
            for bad in ("DELETE FROM mapsql_demo.ankara_pois", "SELECT * FROM mapsql_demo.ankara_pois@lnk",
                        "SELECT DBMS_LOCK.SLEEP(1) FROM dual", "SELECT * FROM all_users"):
                res = await s.call_tool("run_sql", {"query": bad})
                assert _is_error(res), bad
            res = await s.call_tool("run_sql", {"query": "SELECT * FROM hr.employees"})
            assert _is_error(res) and "allowlist" in _text(res)
            res = await s.call_tool("find_features", {"layer": "world_cities"})  # schema allowlist is MAPSQL_DEMO: visible
            assert not _is_error(res)
            # ask, do not guess
            res = await s.call_tool("within_distance", {"layer": "ankara_pois", "distance_m": 100})
            assert _is_error(res) and "near WHAT" in _text(res)
            # no connection details anywhere
            res = await s.call_tool("run_sql", {"query": "SELECT * FROM mapsql_demo.nope"})
            assert _is_error(res)
            low = _text(res).lower()
            assert "oraclecloud" not in low and "description=" not in low and "password" not in low


async def test_oracle_write_mode_is_refused_at_startup(oracle_svc, oracle_creds) -> None:
    import asyncio

    env = {**os.environ, **oracle_env(oracle_creds)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mapsql_mcp", "--allow-write", env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 2 and b"not supported for the Oracle backend" in err and out == b""
