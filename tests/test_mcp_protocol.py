"""End-to-end: spawn the real server over stdio and talk to it with the official MCP client SDK."""

from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.db

EXPECTED_TOOLS = {
    "server_info", "list_layers", "describe_layer", "sample_features", "find_features", "within_distance", "nearest",
    "buffer", "spatial_filter", "bbox_of", "aggregate", "spatial_join", "run_sql", "render_map",
}


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


def _is_error(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


@pytest.fixture()
def params(db_url, tmp_path):
    env = {**os.environ, "DATABASE_URL": db_url, "MAPSQL_SCHEMAS": "mt"}
    return StdioServerParameters(
        command=sys.executable, args=["-m", "mapsql_mcp", "--no-basemap", "--output-dir", str(tmp_path)], env=env
    )


async def test_stdio_smoke(params, tmp_path):
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            info = getattr(init, "server_info", None) or init.serverInfo  # mcp 2.x / 1.x spelling
            assert info.name == "mapsql-mcp"
            assert "Ask, do not guess" in (init.instructions or "")

            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert EXPECTED_TOOLS <= set(tools)
            assert "execute_sql" not in tools  # write mode is off by default
            for t in tools.values():
                assert t.description and len(t.description) > 20
            schema = getattr(tools["within_distance"], "input_schema", None) or tools["within_distance"].inputSchema
            assert {"layer", "distance_m"} <= set(schema["required"]) and "lon" in schema["properties"]

            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert {"gis_assistant", "spatial_question", "explore_database"} <= prompts
            guide = await s.get_prompt("gis_assistant")
            assert "Ask when ambiguous" in guide.messages[0].content.text

            # discovery -> query -> map
            res = await s.call_tool("list_layers", {})
            layers = json.loads(_text(res))
            assert any(l["layer"] == "mt.cities" for l in layers["layers"])

            res = await s.call_tool(
                "within_distance",
                {"layer": "cities", "distance_m": 1500, "lon": 0, "lat": 0, "columns": ["name"]},
            )
            assert not _is_error(res)
            data = json.loads(_text(res))
            assert [f["properties"]["name"] for f in data["features"]] == ["Alpha", "Bravo"]

            res = await s.call_tool("render_map", {"result_ids": [data["result_id"]], "title": "smoke", "save_html": True})
            assert not _is_error(res)
            kinds = [c.type for c in res.content]
            assert kinds[0] == "image" and "text" in kinds
            assert getattr(res.content[0], "mime_type", getattr(res.content[0], "mimeType", "image/png")) == "image/png"
            summary = json.loads(res.content[-1].text)
            assert os.path.exists(summary["png_path"]) and os.path.exists(summary["html_path"])
            with open(summary["png_path"], "rb") as fh:
                assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

            # safety over the wire
            res = await s.call_tool("run_sql", {"query": "DROP TABLE mt.cities"})
            assert _is_error(res) and "Only SELECT" in _text(res)
            res = await s.call_tool("run_sql", {"query": "SELECT * FROM mt_other.secret"})
            assert _is_error(res)
            # ask-when-ambiguous over the wire
            res = await s.call_tool("within_distance", {"layer": "cities", "distance_m": 100})
            assert _is_error(res) and "near WHAT" in _text(res)
            res = await s.call_tool("find_features", {"layer": "mt_other.secret"})
            assert _is_error(res)
            # no connection string / password anywhere in errors
            res = await s.call_tool("run_sql", {"query": "SELECT * FROM mt.nope"})
            assert _is_error(res) and "postgresql://" not in _text(res)


async def test_write_tool_only_with_flag(db_url, tmp_path):
    env = {**os.environ, "DATABASE_URL": db_url}
    p = StdioServerParameters(command=sys.executable, args=["-m", "mapsql_mcp", "--allow-write", "--output-dir", str(tmp_path)], env=env)
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert "execute_sql" in names


async def test_server_refuses_to_start_without_database_url():
    import asyncio

    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mapsql_mcp", env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 2 and b"DATABASE_URL" in err and out == b""


async def test_unknown_argument_is_rejected(params):
    # A model that invents a parameter must get an error, not a silently unfiltered answer.
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_layers", {"where": "x = 1"})
            assert _is_error(res)
            assert "Unknown parameter(s) for list_layers: where" in _text(res)
