"""GeoJSON helpers: SQL generation for geometry output, result cache, response shaping."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Any, Iterable

from psycopg import sql

from .db import json_safe

#: simplify geometries with more vertices than this when sending them out
SIMPLIFY_ABOVE_VERTICES = 300
#: simplification tolerance = bbox diagonal-ish extent / this
SIMPLIFY_DIVISOR = 400.0
#: coordinates (vertices) that fit in a model response in "auto" geometry mode
DEFAULT_COORD_BUDGET = 1500
#: absolute cap in "full" mode
FULL_COORD_CAP = 25000


def geojson_sql(g4326: sql.Composable) -> sql.Composable:
    """SQL that turns a EPSG:4326 geometry expression into simplified GeoJSON (``json``)."""
    return sql.SQL(
        "(SELECT CASE WHEN s.g IS NULL OR ST_IsEmpty(s.g) THEN NULL ELSE ST_AsGeoJSON("
        "CASE WHEN ST_NPoints(s.g) > {npts} THEN ST_Simplify(s.g, "
        "GREATEST(ST_XMax(s.g) - ST_XMin(s.g), ST_YMax(s.g) - ST_YMin(s.g)) / {div}, true) ELSE s.g END, 6)::json END "
        "FROM (SELECT {g} AS g) s)"
    ).format(npts=sql.Literal(SIMPLIFY_ABOVE_VERTICES), div=sql.Literal(SIMPLIFY_DIVISOR), g=g4326)


def count_coords(geom: dict | None) -> int:
    if not geom:
        return 0
    t = geom.get("type")
    if t == "GeometryCollection":
        return sum(count_coords(g) for g in geom.get("geometries", []))

    def walk(c: Any) -> int:
        if not c:
            return 0
        if isinstance(c[0], (int, float)):
            return 1
        return sum(walk(x) for x in c)

    return walk(geom.get("coordinates"))


def geom_bounds(geom: dict | None) -> tuple[float, float, float, float] | None:
    if not geom:
        return None
    xs: list[float] = []
    ys: list[float] = []

    def walk(c: Any) -> None:
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries", []):
            b = geom_bounds(g)
            if b:
                xs.extend([b[0], b[2]])
                ys.extend([b[1], b[3]])
    else:
        walk(geom.get("coordinates"))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def collection_bbox(features: Iterable[dict]) -> list[float] | None:
    b: list[float] | None = None
    for f in features:
        gb = geom_bounds(f.get("geometry"))
        if gb is None:
            continue
        if b is None:
            b = list(gb)
        else:
            b = [min(b[0], gb[0]), min(b[1], gb[1]), max(b[2], gb[2]), max(b[3], gb[3])]
    return [round(x, 6) for x in b] if b else None


def centroid_point(geom: dict) -> dict | None:
    try:
        from shapely.geometry import shape

        c = shape(geom).representative_point()
        return {"type": "Point", "coordinates": [round(c.x, 6), round(c.y, 6)]}
    except Exception:
        return None


def make_feature(props: dict[str, Any], geometry: dict | None) -> dict:
    return {"type": "Feature", "properties": {k: json_safe(v, 500) for k, v in props.items()}, "geometry": geometry}


class ResultStore:
    """Keeps recent FeatureCollections server-side so ``render_map`` can reuse them by id
    without the model having to copy large GeoJSON around."""

    def __init__(self, max_items: int = 40):
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_items
        self._n = 0

    def put(self, fc: dict, title: str) -> str:
        with self._lock:
            self._n += 1
            rid = f"res_{self._n}_{uuid.uuid4().hex[:4]}"
            self._items[rid] = {"fc": fc, "title": title}
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return rid

    def get(self, rid: str) -> dict:
        with self._lock:
            item = self._items.get(rid)
        if item is None:
            from .errors import MapsqlError

            raise MapsqlError(
                f"Unknown result_id {rid!r} (results are kept in memory for this server session only). Re-run the query."
            )
        return item


def shape_response(
    fc: dict,
    *,
    geometry: str = "auto",
    budget: int = DEFAULT_COORD_BUDGET,
) -> tuple[dict, str | None]:
    """Return a copy of ``fc`` suitable for sending to the model, and an optional note.

    geometry: "auto" | "full" | "centroid" | "none"
    """
    feats = fc["features"]
    total = sum(count_coords(f.get("geometry")) for f in feats)
    note = None
    out_feats: list[dict] = []
    mode = geometry
    if mode == "auto":
        mode = "full" if total <= budget else "centroid"
        if mode == "centroid":
            note = (
                f"Geometries have {total} vertices (> {budget}); returned as representative points to save context. "
                "Use render_map with the result_id to see the full geometry, or pass geometry='full'."
            )
    elif mode == "full" and total > FULL_COORD_CAP:
        mode = "centroid"
        note = f"Geometries have {total} vertices (> {FULL_COORD_CAP}); returned as representative points."
    for f in feats:
        g = f.get("geometry")
        if mode == "none" or g is None:
            ng = None
        elif mode == "centroid":
            ng = g if g.get("type") == "Point" else centroid_point(g)
        else:
            ng = g
        out_feats.append({"type": "Feature", "properties": f["properties"], "geometry": ng})
    shaped = dict(fc)
    shaped["features"] = out_feats
    return shaped, note
