"""The contract between the MCP layer (``server.py``) and a database backend.

A backend is a *service class* with the tool methods below plus a :class:`BackendSpec` that carries the
texts which differ per database (SQL dialect, server instructions). Everything above this line - tool
registration, result store, map rendering, "ask, don't guess" - is shared.

Backends:

* ``postgis`` - :class:`mapsql_mcp.service.GeoService` (psycopg, pglast guard)
* ``oracle``  - :class:`mapsql_mcp.backends.oracle.OracleService` (python-oracledb, sqlglot guard)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BackendSpec:
    name: str  # "postgis" | "oracle"
    label: str  # "PostGIS" | "Oracle Spatial"
    sql_dialect: str  # shown in the run_sql tool description
    server_info_hint: str
    run_sql_hint: str
    extra_instructions: str = ""
    supports_write: bool = True


@runtime_checkable
class SpatialService(Protocol):
    """What ``create_server`` needs from a backend (all methods are synchronous and thread-safe)."""

    backend: str
    settings: Any
    store: Any  # geo.ResultStore

    def close(self) -> None: ...
    def server_info(self) -> dict: ...
    def list_layers(self, schema: str | None = None, search: str | None = None) -> dict: ...
    def describe_layer(self, layer: str, include_value_samples: bool = True) -> dict: ...
    def sample_features(self, layer: str, n: int = 5, geometry: str = "auto") -> dict: ...
    def find_features(self, layer: str, filters: Any = None, columns: Any = None, order_by: Any = None,
                      descending: bool = False, limit: Any = None, geometry: str = "auto") -> dict: ...
    def within_distance(self, layer: str, distance_m: float, *args: Any, **kw: Any) -> dict: ...
    def nearest(self, layer: str, *args: Any, **kw: Any) -> dict: ...
    def buffer(self, distance_m: float, *args: Any, **kw: Any) -> dict: ...
    def spatial_filter(self, layer: str, *args: Any, **kw: Any) -> dict: ...
    def bbox_of(self, layer: str, filters: Any = None) -> dict: ...
    def aggregate(self, layer: str, *args: Any, **kw: Any) -> dict: ...
    def spatial_join(self, left_layer: str, right_layer: str, *args: Any, **kw: Any) -> dict: ...
    def run_sql(self, query: str, limit: Any = None, geometry: str = "auto") -> dict: ...
    def execute_sql(self, statement: str) -> dict: ...
