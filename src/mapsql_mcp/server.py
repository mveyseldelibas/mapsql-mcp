"""MCP server wiring: tools, prompts, resources."""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import sys
import time
from typing import Annotated, Any, Awaitable, Callable, Literal

import anyio
from pydantic import Field

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]
    from mcp.server.fastmcp import Image  # type: ignore[assignment]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[assignment]

from .backends import create_service, spec_by_name
from .backends.base import SpatialService
from .config import Settings
from .errors import MapsqlError, redact
from .layers import Filter
from .prompts import guide_for, instructions_for
from .render import MapLayer, render_html, render_png, safe_filename
from .service import Metric

log = logging.getLogger("mapsql_mcp")

GeometryMode = Literal["auto", "full", "centroid", "none"]
Limit = Annotated[int | None, Field(description="Max rows to return (default 100; server-side cap applies).", ge=1)]
Geometry = Annotated[
    GeometryMode,
    Field(
        description="'auto' (default): full geometry if small, else representative points; 'full'; 'centroid'; "
        "'none' (attributes only). The complete geometry is always kept server-side under result_id for render_map."
    ),
]
Lon = Annotated[float | None, Field(description="Longitude (WGS84, east-west, -180..180).")]
Lat = Annotated[float | None, Field(description="Latitude (WGS84, north-south, -90..90).")]
RefLayer = Annotated[
    str | None,
    Field(description="Measure from the geometry of feature(s) in this layer instead of a lon/lat point."),
]
RefFilters = Annotated[
    list[Filter] | None,
    Field(description="Filters that pick the reference feature(s) in reference_layer (e.g. name = 'X')."),
]
Filters = Annotated[list[Filter] | None, Field(description="Attribute filters on the layer, combined with AND.")]
Columns = Annotated[list[str] | None, Field(description="Attribute columns to return (default: all non-geometry).")]


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _open_in_viewer(path: str | None) -> bool:
    """Open a rendered map in the desktop's default app (``--open-maps``). Never raises: a missing viewer must not fail
    the tool call."""
    if not path:
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        log.warning("could not open %s in a viewer", path)
        return False


class _StrictServer(MCPServer):  # type: ignore[misc, valid-type]
    """Rejects unknown tool arguments instead of silently dropping them.

    Without this a model that invents a parameter (e.g. ``where="kind = 'hospital'"`` on ``nearest``) gets an
    unfiltered answer that looks plausible; an explicit error makes it retry with the real parameter."""

    async def call_tool(self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        tool = self._tool_manager.get_tool(name)
        if tool is not None and arguments:
            allowed = set((tool.parameters or {}).get("properties", {}))
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                hint = " Use 'filters' for attribute conditions." if allowed & {"filters", "left_filters"} else ""
                raise ToolError(
                    f"Unknown parameter(s) for {name}: {', '.join(unknown)}. "
                    f"Allowed: {', '.join(sorted(allowed))}.{hint}"
                )
        return await super().call_tool(name, arguments, *args, **kwargs)


def create_server(settings: Settings, service: SpatialService | None = None) -> MCPServer:
    svc = service or create_service(settings)
    spec = spec_by_name(getattr(svc, "backend", "postgis"))
    GUIDE = guide_for(spec)
    mcp = _StrictServer("mapsql-mcp", instructions=instructions_for(spec))
    try:
        mcp._mapsql_service = svc  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

    def tool(**kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Awaitable[str]]]:
        """Register a sync service call as an async MCP tool: runs in a worker thread, errors redacted,
        result serialised as compact JSON text."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Awaitable[str]]:
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> str:
                t0 = time.perf_counter()
                try:
                    result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
                except MapsqlError as e:
                    raise ToolError(str(e)) from None
                except ToolError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("tool %s failed", fn.__name__)
                    raise ToolError(redact(f"Internal error in {fn.__name__}: {type(e).__name__}")) from None
                log.info("tool %s ok in %.0f ms", fn.__name__, (time.perf_counter() - t0) * 1000)
                return _dump(result)

            kw.setdefault("structured_output", False)
            return mcp.tool(**kw)(wrapper)  # type: ignore[return-value]

        return deco

    # ------------------------------------------------------------------ discovery

    @tool(description=f"Server status: {spec.server_info_hint}, mode (read-only), limits, allowlists. Call once if unsure what is available.")
    def server_info() -> dict:
        return svc.server_info()

    @tool(
        description="START HERE. List the spatial layers (geometry/geography tables & views) with geometry type, SRID, "
        "approx. row count, extent and column names. Use `search` to find layers by name/column (e.g. 'road', 'population')."
    )
    def list_layers(
        schema: Annotated[str | None, Field(description="Only layers in this schema.")] = None,
        search: Annotated[str | None, Field(description="Case-insensitive text matched against layer and column names.")] = None,
    ) -> dict:
        return svc.list_layers(schema, search)

    @tool(
        description="Describe one layer in detail: all columns and types, exact row count, extent, geometry types, "
        "the most common values of text columns and min/max of numeric columns. Call this BEFORE filtering so you use "
        "real column names and values."
    )
    def describe_layer(
        layer: Annotated[str, Field(description="Layer name, e.g. 'cities' or 'osm.roads'.")],
        include_value_samples: Annotated[bool, Field(description="Include top values / numeric ranges.")] = True,
    ) -> dict:
        return svc.describe_layer(layer, include_value_samples)

    @tool(description="Return a few example features of a layer (random for small tables) to see real data.")
    def sample_features(
        layer: str,
        n: Annotated[int, Field(description="How many features (1-50).", ge=1, le=50)] = 5,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.sample_features(layer, n, geometry)

    # ------------------------------------------------------------------ retrieval

    @tool(
        description="Find features by attribute filters (no spatial predicate). Returns GeoJSON + result_id for render_map. "
        "Use describe_layer first to learn column names and values."
    )
    def find_features(
        layer: str,
        filters: Filters = None,
        columns: Columns = None,
        order_by: Annotated[str | None, Field(description="Column to sort by.")] = None,
        descending: bool = False,
        limit: Limit = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.find_features(layer, filters, columns, order_by, descending, limit, geometry)

    @tool(
        description="Features of `layer` within `distance_m` METRES of a reference: a lon/lat point OR feature(s) from "
        "another layer. Distances are geodesic/metric regardless of the layer's SRID. Results include distance_m, nearest first. "
        "If the user did not say what to measure from, or the reference matches several features, ASK the user."
    )
    def within_distance(
        layer: str,
        distance_m: Annotated[float, Field(description="Radius in metres.", gt=0)],
        lon: Lon = None,
        lat: Lat = None,
        reference_layer: RefLayer = None,
        reference_filters: RefFilters = None,
        use_all_matches: Annotated[bool, Field(description="If the reference matches several features, use all of them (union) instead of asking.")] = False,
        filters: Filters = None,
        columns: Columns = None,
        limit: Limit = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.within_distance(
            layer, distance_m, lon, lat, reference_layer, reference_filters, use_all_matches, filters, columns, limit, geometry
        )

    @tool(
        description="The k nearest features of `layer` to a reference (lon/lat point or feature(s) of another layer), with "
        "distance_m in metres. Optional max_distance_m. If the user did not say what 'nearest' is relative to, ASK."
    )
    def nearest(
        layer: str,
        k: Annotated[int, Field(description="Number of neighbours (1-100).", ge=1, le=100)] = 5,
        lon: Lon = None,
        lat: Lat = None,
        reference_layer: RefLayer = None,
        reference_filters: RefFilters = None,
        use_all_matches: bool = False,
        max_distance_m: Annotated[float | None, Field(description="Ignore features farther than this (metres).")] = None,
        filters: Filters = None,
        columns: Columns = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.nearest(
            layer, k, lon, lat, reference_layer, reference_filters, use_all_matches, max_distance_m, filters, columns, geometry
        )

    @tool(
        description="Buffer geometries by a distance in METRES (geodesic). Buffer a point (lon/lat) or the features of a layer "
        "(+filters). dissolve=true merges into one polygon (also returns its area_km2). Returns GeoJSON + result_id."
    )
    def buffer(
        distance_m: Annotated[float, Field(description="Buffer radius in metres.", gt=0)],
        layer: Annotated[str | None, Field(description="Layer to buffer (omit when buffering a lon/lat point).")] = None,
        filters: Filters = None,
        lon: Lon = None,
        lat: Lat = None,
        dissolve: bool = True,
        limit: Limit = None,
    ) -> dict:
        return svc.buffer(distance_m, layer, filters, lon, lat, dissolve, limit)

    @tool(
        description="Spatial predicate filter: features of `layer` that intersect / are within / contain / touch / cross / overlap / "
        "are disjoint from (a) features of another layer (other_layer + other_filters), (b) a bbox [min_lon,min_lat,max_lon,max_lat], "
        "or (c) a GeoJSON geometry. Example: pois within the polygon 'Central Park'. Ask the user if the second operand is unclear."
    )
    def spatial_filter(
        layer: str,
        relation: Literal["intersects", "within", "contains", "touches", "crosses", "overlaps", "disjoint"] = "intersects",
        other_layer: str | None = None,
        other_filters: Annotated[list[Filter] | None, Field(description="Filters selecting the features of other_layer.")] = None,
        bbox: Annotated[list[float] | None, Field(description="[min_lon, min_lat, max_lon, max_lat] in WGS84.")] = None,
        geojson_geometry: Annotated[dict | None, Field(description="A GeoJSON geometry object in WGS84.")] = None,
        filters: Filters = None,
        columns: Columns = None,
        limit: Limit = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.spatial_filter(
            layer, relation, other_layer, other_filters, bbox, geojson_geometry, filters, columns, limit, geometry
        )

    @tool(description="Bounding box (WGS84) and centre of a layer or a filtered subset. Useful for framing maps or checking coverage.")
    def bbox_of(layer: str, filters: Filters = None) -> dict:
        return svc.bbox_of(layer, filters)

    # ------------------------------------------------------------------ analysis

    @tool(
        description="Group-by statistics over a layer: count, count_distinct, sum, avg, min, max, area_km2 (geodesic polygon area), "
        "length_km (geodesic line length), optionally grouped by columns and filtered. Returns a table (no geometry)."
    )
    def aggregate(
        layer: str,
        metrics: Annotated[list[Metric] | None, Field(description="Measures to compute (default: count).")] = None,
        group_by: Annotated[list[str] | None, Field(description="Columns to group by.")] = None,
        filters: Filters = None,
        order_by: Annotated[str | None, Field(description="Output column to sort by (default: first metric).")] = None,
        descending: bool = True,
        limit: Limit = None,
    ) -> dict:
        return svc.aggregate(layer, metrics, group_by, filters, order_by, descending, limit)

    @tool(
        description="Spatial join between two layers, per feature of the LEFT layer. mode='count' (default) counts related RIGHT "
        "features (e.g. pharmacies per district: left=districts, right=pois, relation='contains'); 'sum'/'avg' aggregate a numeric "
        "right_column; 'attach' lists matched pairs with right_columns. relation: intersects | within | contains | dwithin "
        "(needs distance_m). Returns the LEFT geometry so it can be mapped (choropleth-like)."
    )
    def spatial_join(
        left_layer: str,
        right_layer: str,
        relation: Literal["intersects", "within", "contains", "dwithin"] = "intersects",
        distance_m: Annotated[float | None, Field(description="Required for relation='dwithin' (metres).")] = None,
        mode: Literal["count", "sum", "avg", "attach"] = "count",
        right_column: Annotated[str | None, Field(description="Numeric right column for mode sum/avg.")] = None,
        right_columns: Annotated[list[str] | None, Field(description="Right columns to attach (mode 'attach').")] = None,
        left_filters: Filters = None,
        right_filters: Annotated[list[Filter] | None, Field(description="Filters on the right layer.")] = None,
        order_desc: bool = True,
        min_count: Annotated[int | None, Field(description="Keep only left features with at least this many matches.")] = None,
        limit: Limit = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.spatial_join(
            left_layer, right_layer, relation, distance_m, mode, right_column, right_columns, left_filters, right_filters,
            order_desc, min_count, limit, geometry,
        )

    @tool(
        description=f"Run a custom READ-ONLY SQL query ({spec.run_sql_hint}) when the structured tools do not "
        "fit. Validated by a SQL parser, executed in a READ ONLY transaction with a timeout and row cap. Geometry columns are "
        "returned as GeoJSON (EPSG:4326). Prefer list_layers/describe_layer first; alias every column."
    )
    def run_sql(
        query: Annotated[str, Field(description="One SELECT statement (WITH/UNION allowed). No semicolon-separated batches.")],
        limit: Limit = None,
        geometry: Geometry = "auto",
    ) -> dict:
        return svc.run_sql(query, limit, geometry)

    if settings.allow_write and spec.supports_write:

        @tool(
            description="WRITE MODE (enabled by the operator with --allow-write): run one INSERT/UPDATE/DELETE (WHERE required)/"
            "CREATE TABLE|INDEX|VIEW/ALTER TABLE ADD COLUMN statement and commit. Always confirm with the user before writing."
        )
        def execute_sql(statement: str) -> dict:
            return svc.execute_sql(statement)

    # ------------------------------------------------------------------ maps

    @mcp.tool(
        description="Draw a MAP and return it as a PNG image (with an OpenStreetMap basemap when online). Pass result_ids from "
        "earlier tools (find_features, within_distance, nearest, buffer, spatial_join, bbox_of, run_sql ...) - each becomes a "
        "coloured layer - or a GeoJSON string/object, or a SQL query that returns a geometry column. Also saves the PNG and, "
        "with save_html=true, an interactive Leaflet HTML map to disk. Use this whenever the answer is spatial.",
        structured_output=False,
    )
    async def render_map(
        result_ids: Annotated[list[str] | None, Field(description="result_id values returned by other tools (drawn in order).")] = None,
        geojson: Annotated[str | dict | None, Field(description="A GeoJSON FeatureCollection/Feature/Geometry (object or JSON string).")] = None,
        sql: Annotated[str | None, Field(description="Read-only SQL returning a geometry column (rendered as one layer).")] = None,
        title: Annotated[str | None, Field(description="Map title.")] = None,
        label_property: Annotated[str | None, Field(description="Feature property to print as label (default: 'name' if few features).")] = None,
        value_property: Annotated[str | None, Field(description="Numeric property for a graduated colour fill (choropleth), e.g. 'right_count' from spatial_join.")] = None,
        basemap: Annotated[bool, Field(description="Draw the OpenStreetMap basemap (needs internet). false = plain background.")] = True,
        width: Annotated[int, Field(ge=300, le=2400)] = 1000,
        height: Annotated[int, Field(ge=300, le=2400)] = 700,
        save_html: Annotated[bool, Field(description="Also write an interactive Leaflet HTML file.")] = False,
    ) -> list:
        def work() -> tuple[bytes, dict]:
            layers = _collect_layers(svc, result_ids, geojson, sql)
            if not layers:
                raise MapsqlError("Nothing to draw: provide result_ids, geojson or sql.")
            if title is None and len(layers) == 1:
                map_title = layers[0].name
            else:
                map_title = title
            for l in layers:
                if label_property:
                    l.label_property = label_property
                if value_property:
                    l.value_property = value_property
            out_dir = settings.output_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            cache = out_dir / ".tiles"
            use_basemap = "none" if (not basemap or settings.basemap == "none") else settings.basemap
            png, info = render_png(layers, title=map_title, width=width, height=height, basemap=use_basemap, cache_dir=cache)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            stem = f"{stamp}_{safe_filename(map_title or 'map')}"
            png_path = out_dir / f"{stem}.png"
            png_path.write_bytes(png)
            info.png_path = str(png_path)
            if save_html:
                html_path = out_dir / f"{stem}.html"
                html_path.write_text(render_html(layers, map_title), encoding="utf-8")
                info.html_path = str(html_path)
            summary = {
                "png_path": info.png_path,
                "html_path": info.html_path,
                "layers": [{"name": l.name, "features": len(l.features)} for l in layers],
                "bbox": info.bbox,
                "basemap": info.basemap,
            }
            if info.notes:
                summary["notes"] = info.notes
            if settings.open_maps:
                summary["opened"] = _open_in_viewer(info.html_path or info.png_path)
            return png, summary

        try:
            png, summary = await anyio.to_thread.run_sync(work)
        except MapsqlError as e:
            raise ToolError(str(e)) from None
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("render_map failed")
            raise ToolError(redact(f"Could not render the map: {type(e).__name__}: {e}")) from None
        return [Image(data=png, format="png"), _dump(summary)]

    # ------------------------------------------------------------------ prompts & resources

    @mcp.prompt(name="gis_assistant", description="Guidance for answering spatial questions with this server (ask when ambiguous, units, maps).")
    def gis_assistant() -> str:
        return GUIDE

    @mcp.prompt(name="explore_database", description="Walk through the available layers and summarise what the database contains.")
    def explore_database() -> str:
        return (
            f"Explore this {spec.label} database with the mapsql-mcp tools: call list_layers, then describe_layer for the 3-5 most "
            "interesting layers, then render_map with a bounding-box overview. Summarise what data is available, its coverage, "
            "coordinate systems and 5 example questions the user could ask. Do not guess; use only what the tools return."
        )

    @mcp.prompt(name="spatial_question", description="Answer one spatial question: asks for missing details, then queries and maps.")
    def spatial_question(question: str) -> str:
        return (
            f"{GUIDE}\n\nUser question: {question}\n\nFirst decide whether anything essential is missing (reference place, distance, "
            "which layer, time/filters). If so, ask ONE concise clarifying question and stop. Otherwise answer with the structured "
            "tools, state the numbers precisely, and finish with render_map when the answer is geographic."
        )

    @mcp.resource("mapsql://guide", name="guide", description="How to use mapsql-mcp effectively and safely.", mime_type="text/markdown")
    def guide_resource() -> str:
        return GUIDE

    @mcp.resource("mapsql://layers", name="layers", description="Current layer catalog (JSON).", mime_type="application/json")
    def layers_resource() -> str:
        return _dump(svc.list_layers())

    return mcp


def _collect_layers(
    svc: SpatialService, result_ids: list[str] | None, geojson: Any, sql: str | None
) -> list[MapLayer]:
    layers: list[MapLayer] = []
    for rid in result_ids or []:
        item = svc.store.get(rid)
        layers.append(MapLayer(name=item["title"], features=item["fc"]["features"]))
    if geojson:
        obj = geojson
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                raise MapsqlError("geojson is not valid JSON.") from None
        feats = _features_of(obj)
        layers.append(MapLayer(name="GeoJSON", features=feats))
    if sql:
        res = svc.run_sql(sql, limit=5000, geometry="none")
        if "result_id" not in res:
            raise MapsqlError("The SQL returned no geometry column; select a geometry (e.g. geom) to draw it.")
        item = svc.store.get(res["result_id"])
        layers.append(MapLayer(name="SQL result", features=item["fc"]["features"]))
    return layers


def _features_of(obj: Any) -> list[dict]:
    if not isinstance(obj, dict) or "type" not in obj:
        raise MapsqlError("geojson must be a GeoJSON object with a 'type'.")
    t = obj["type"]
    if t == "FeatureCollection":
        return [f for f in obj.get("features", []) if isinstance(f, dict)]
    if t == "Feature":
        return [obj]
    return [{"type": "Feature", "properties": {}, "geometry": obj}]
