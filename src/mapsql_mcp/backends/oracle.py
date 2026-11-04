"""Oracle Spatial backend: the same toolbox as the PostGIS backend, expressed with SDO_* operators and functions.

Design notes (the differences from PostGIS that matter):

* Metres everywhere: spatial operators get ``unit=M`` and SDO_GEOM functions ``'unit=M'`` / ``'unit=SQ_KM'``, so
  geodetic (EPSG:4326 / Oracle 8307) and projected layers (e.g. TUREF TM30, EPSG:5254) are both metre-correct.
* ``SDO_WITHIN_DISTANCE`` / ``SDO_NN`` / ``SDO_RELATE`` need a spatial index on their first argument. Layers without
  one automatically use index-free ``SDO_GEOM.*`` functions (slower; flagged in the response).
* GeoJSON comes from ``SDO_UTIL.TO_GEOJSON`` (always in EPSG:4326, transformed with ``SDO_CS.TRANSFORM`` when needed) and
  is simplified/rounded in Python (shapely) like the PostGIS backend does in SQL.
* Values are always bind variables; identifiers come from the catalog and are double-quoted.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel  # noqa: F401  (Metric is defined in service.py and shared)

from ..config import Settings
from ..db import QueryResult, json_safe
from ..errors import MapsqlError, NeedsClarification
from ..geo import (
    SIMPLIFY_ABOVE_VERTICES, SIMPLIFY_DIVISOR, ResultStore, collection_bbox, count_coords, make_feature, shape_response,
)
from ..layers import Filter
from ..service import MAX_DISTANCE_M, Metric, _num
from .oracle_db import OracleDatabase, display_name, quote_ident
from .oracle_guard import validate_oracle_sql
from .oracle_layers import (
    Binds, OracleCatalog, OraLayer, WGS84_SRIDS, compile_filters, num_lit,
)

GeometryMode = Literal["auto", "full", "centroid", "none"]
Relation = Literal["intersects", "within", "contains", "touches", "crosses", "overlaps", "disjoint"]

#: relation -> (mask seen from the layer's side, mask seen from the other side). EQUAL is part of within/contains.
_MASKS = {
    "intersects": ("ANYINTERACT", "ANYINTERACT"),
    "within": ("INSIDE+COVEREDBY+EQUAL", "CONTAINS+COVERS+EQUAL"),
    "contains": ("CONTAINS+COVERS+EQUAL", "INSIDE+COVEREDBY+EQUAL"),
    "touches": ("TOUCH", "TOUCH"),
    "overlaps": ("OVERLAPBDYDISJOINT+OVERLAPBDYINTERSECT", "OVERLAPBDYDISJOINT+OVERLAPBDYINTERSECT"),
}


def _sql_text(q: str) -> str:
    return q


def _where(parts: list[str | None]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return " WHERE " + " AND ".join(f"({p})" for p in parts)


def _round_coords(c: Any) -> Any:
    if isinstance(c, (int, float)):
        return round(c, 6)
    return [_round_coords(x) for x in c]


def parse_geojson(text: str | None) -> dict | None:
    """GeoJSON text from SDO_UTIL.TO_GEOJSON -> geometry dict, rounded to 6 decimals and simplified when large."""
    if not text:
        return None
    try:
        g = json.loads(text)
    except ValueError:
        return None
    if not isinstance(g, dict) or "type" not in g:
        return None
    if g["type"] != "GeometryCollection" and "coordinates" in g:
        if count_coords(g) > SIMPLIFY_ABOVE_VERTICES:
            try:
                from shapely.geometry import mapping, shape

                s = shape(g)
                x0, y0, x1, y1 = s.bounds
                tol = max(x1 - x0, y1 - y0) / SIMPLIFY_DIVISOR
                s2 = s.simplify(tol, preserve_topology=True)
                if not s2.is_empty:
                    g = json.loads(json.dumps(mapping(s2)))
            except Exception:  # noqa: BLE001 - keep the unsimplified geometry
                pass
        g["coordinates"] = _round_coords(g["coordinates"])
    g.pop("crs", None)
    return g


def _geojson(g4326: str) -> str:
    return f"SDO_UTIL.TO_GEOJSON({g4326})"


def _point_sql(lon: float, lat: float, srid: int = 4326) -> str:
    return f"SDO_GEOMETRY(2001, {int(srid)}, SDO_POINT_TYPE({num_lit(lon)}, {num_lit(lat)}, NULL), NULL, NULL)"


class OraRef:
    """A reference geometry in EPSG:4326 as an SQL expression."""

    def __init__(self, g4326: str, description: str, matched: int = 1):
        self.g4326 = g4326
        self.description = description
        self.matched = matched

    def for_layer(self, layer: OraLayer) -> str:
        if not layer.srid or layer.srid == 4326:
            return self.g4326
        return f"SDO_CS.TRANSFORM({self.g4326}, {int(layer.srid)})"


def _short_type(t: str) -> str:
    return t.lower()


class OracleService:
    """Oracle Spatial implementation of the backend contract (see ``backends/base.py``)."""

    backend = "oracle"

    def __init__(self, settings: Settings, db: OracleDatabase | None = None):
        self.settings = settings
        self.db = db or OracleDatabase(settings)
        self.catalog = OracleCatalog(self.db)
        self.store = ResultStore()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ utils

    def _limit(self, limit: int | None) -> int:
        if limit is None:
            return self.settings.default_limit
        try:
            n = int(limit)
        except (TypeError, ValueError):
            raise MapsqlError("limit must be an integer.") from None
        if n < 1:
            raise MapsqlError("limit must be >= 1.")
        return min(n, self.settings.max_rows)

    def _columns(self, layer: OraLayer, columns: list[str] | None) -> list[str]:
        if columns:
            out = []
            for c in columns:
                col = layer.find_column(c)
                if col.is_spatial or not col.is_attribute:
                    continue
                out.append(col.name)
            return out
        return [c.name for c in layer.attribute_columns[:40]]

    def _point(self, lon: float, lat: float) -> str:
        lon_f = _num("lon", lon)
        lat_f = _num("lat", lat)
        if not (-180 <= lon_f <= 180) or not (-90 <= lat_f <= 90):
            hint = ""
            if -180 <= lat_f <= 180 and -90 <= lon_f <= 90:
                hint = " (lon and lat look swapped: lon is the X/east-west value, lat the Y/north-south value)"
            raise MapsqlError(f"Coordinates out of range for WGS84: lon={lon_f}, lat={lat_f}.{hint}")
        return _point_sql(lon_f, lat_f)

    def _reference(
        self,
        lon: float | None,
        lat: float | None,
        reference_layer: str | None,
        reference_filters: list[Filter] | None,
        binds: Binds,
        use_all_matches: bool = False,
    ) -> OraRef:
        has_pt = lon is not None or lat is not None
        if has_pt and (lon is None or lat is None):
            raise NeedsClarification("Both lon and lat are required for a reference point.")
        if has_pt and reference_layer:
            raise NeedsClarification(
                "Give either a reference point (lon, lat) or a reference feature (reference_layer + "
                "reference_filters), not both."
            )
        if has_pt:
            return OraRef(self._point(lon, lat), f"point ({lon}, {lat})")  # type: ignore[arg-type]
        if not reference_layer:
            raise NeedsClarification(
                "Missing reference: near WHAT? Provide lon+lat, or reference_layer+reference_filters naming a "
                "feature. If the user did not say, ask them (do not guess a location)."
            )
        rl = self.catalog.resolve(reference_layer)
        if not reference_filters:
            raise NeedsClarification(
                f"reference_filters are required to pick the feature(s) in {rl.qualified} to measure from. "
                "Ask the user which feature they mean."
            )
        rl.require_metric()
        cb = Binds("c")
        cwhere = _where([compile_filters(rl, reference_filters, "r", cb)])
        cols = [c.name for c in rl.attribute_columns[:6]]
        sel = ", ".join(f"{rl.col(c, 'r')} AS {quote_ident(display_name(c))}" for c in cols) or "1 AS one"
        res = self.db.query(
            f"SELECT {sel} FROM {rl.table_sql('r')}{cwhere} FETCH FIRST 11 ROWS ONLY", cb.values, max_rows=11
        )
        n = len(res.rows)
        if n == 0:
            raise MapsqlError(
                f"No feature in {rl.qualified} matched the reference_filters. Check spelling/values with "
                "find_features or describe_layer."
            )
        if n > 1 and not use_all_matches:
            cands = [{k: json_safe(v, 80) for k, v in zip(res.columns, row)} for row in res.rows[:10]]
            more = "10+" if n > 10 else str(n)
            raise NeedsClarification(
                f"The reference matched {more} features in {rl.qualified}; which one is meant? Ask the user, "
                f"or refine reference_filters, or set use_all_matches=true to use all of them. Candidates: {cands}"
            )
        where = _where([compile_filters(rl, reference_filters, "r", binds)])
        g = rl.to_4326("r")
        if n == 1:
            sub = f"(SELECT {g} FROM {rl.table_sql('r')}{where})"
        else:
            sub = f"(SELECT SDO_AGGR_UNION(SDOAGGRTYPE({g}, {num_lit(rl.tolerance)})) FROM {rl.table_sql('r')}{where})"
        desc = f"{n if n <= 10 else '10+'} feature(s) of {rl.qualified}"
        return OraRef(sub, desc, n)

    # ------------------------------------------------------------- query plumbing

    def _feature_query(
        self,
        layer: OraLayer,
        *,
        title: str,
        where: list[str | None],
        binds: Binds,
        order: str | None = None,
        limit: int,
        columns: list[str] | None = None,
        geometry: GeometryMode = "auto",
        select_extra: list[tuple[str, str]] | None = None,
        from_sql: str | None = None,
        geom_expr: str | None = None,
        count_total: bool = True,
        report_truncation: bool = True,
        extra_meta: dict[str, Any] | None = None,
        notes: list[str] | None = None,
    ) -> dict:
        cols = self._columns(layer, columns)
        sel = [f"{layer.col(c, 't')} AS {quote_ident(display_name(c))}" for c in cols]
        for expr, name in select_extra or []:
            sel.append(f"{expr} AS {quote_ident(name)}")
        sel.append(f'{_geojson(geom_expr or layer.to_4326("t"))} AS "__geom"')
        frm = from_sql if from_sql is not None else layer.table_sql("t")
        w = _where(where)
        q = (
            f"SELECT {', '.join(sel)} FROM {frm}{w}"
            + (f" ORDER BY {order}" if order else "")
            + f" FETCH FIRST {limit + 1} ROWS ONLY"
        )
        res = self.db.query(q, binds.values, max_rows=limit)
        fc = self._to_fc(res)
        total = None
        if res.truncated and count_total:
            try:
                total = self.db.query(
                    f"SELECT COUNT(*) FROM {frm}{w}", binds.values, max_rows=1,
                    timeout_ms=min(self.settings.statement_timeout_ms, 8000),
                ).rows[0][0]
            except MapsqlError:
                total = None
        meta: dict[str, Any] = {"layer": layer.qualified, "count": len(fc["features"])}
        if report_truncation:
            meta.update({"truncated": res.truncated, "limit": limit})
        if total is not None:
            meta["total_matches"] = total
        if extra_meta:
            meta.update(extra_meta)
        return self._finish(fc, title, geometry, meta, _sql_text(q), notes)

    def _to_fc(self, res: QueryResult) -> dict:
        names = res.columns[:-1]
        feats = []
        for row in res.rows:
            props = dict(zip(names, row[:-1]))
            feats.append(make_feature(props, parse_geojson(row[-1])))
        return {"type": "FeatureCollection", "features": feats}

    def _finish(self, fc: dict, title: str, geometry: GeometryMode, meta: dict, sql_text: str | None,
                notes: list[str] | None = None) -> dict:
        bbox = collection_bbox(fc["features"])
        if bbox:
            fc["bbox"] = bbox
        rid = self.store.put(fc, title)
        shaped, note = shape_response(fc, geometry=geometry)
        out: dict[str, Any] = {"result_id": rid, **meta}
        if bbox:
            out["bbox"] = bbox
        all_notes = list(notes or [])
        if note:
            all_notes.append(note)
        if meta.get("truncated"):
            all_notes.append("Result truncated at the row limit. Add filters, raise 'limit', or aggregate instead of listing.")
        if all_notes:
            out["note"] = " ".join(all_notes)
        out["type"] = "FeatureCollection"
        out["features"] = shaped["features"]
        if sql_text:
            out["sql"] = sql_text if len(sql_text) <= 1500 else sql_text[:1500] + " ..."
        return out

    @staticmethod
    def _no_index_note(layer: OraLayer) -> list[str]:
        return [] if layer.has_index else [
            f"{layer.qualified} has no usable spatial index; index-free SDO_GEOM functions were used (slower on big tables)."
        ]

    # ------------------------------------------------------------- discovery

    def server_info(self) -> dict:
        v = self.db.server_versions()
        return {
            "name": "mapsql-mcp",
            "backend": "oracle",
            **v,
            "mode": "read-only",
            "statement_timeout_ms": self.settings.statement_timeout_ms,
            "default_limit": self.settings.default_limit,
            "max_rows": self.settings.max_rows,
            "allowed_schemas": list(self.settings.schemas) if self.settings.schemas else None,
            "allowed_layers": list(self.settings.layers) if self.settings.layers else None,
            "layers": len(self.catalog.layers()),
            "basemap": "none" if self.settings.basemap == "none" else "enabled",
        }

    def list_layers(self, schema: str | None = None, search: str | None = None) -> dict:
        with self.db.batch():
            return self._list_layers(schema, search)

    def _list_layers(self, schema: str | None, search: str | None) -> dict:
        layers = self.catalog.layers()
        if schema:
            layers = [l for l in layers if l.owner.lower() == schema.lower()]
        if search:
            s = search.lower()
            layers = [l for l in layers if s in l.qualified.lower() or any(s in c.display.lower() for c in l.columns)]
        out = []
        for l in layers:
            item = l.summary()
            item["columns"] = [f"{c.display}:{_short_type(c.type)}" for c in l.columns if not c.is_spatial][:30]
            if l.other_geom_columns:
                item["other_geometry_columns"] = l.other_geom_columns
            ext = self._estimated_extent(l)
            if ext:
                item["extent_wgs84"] = ext
            out.append(item)
        return {"count": len(out), "layers": out}

    def _estimated_extent(self, l: OraLayer) -> list[float] | None:
        """Layer extent in WGS84 (SDO_AGGR_MBR over the layer, bounded to 3 s and to layers that are not huge; cached
        per catalog load). SDO_TUNE.EXTENT_OF is faster but does not work inside READ ONLY transactions."""
        if l.extent_done:
            return l.extent
        l.extent_done = True
        if not l.srid or (l.row_estimate or 0) > 500_000:
            return None
        try:
            r = self.db.query(
                "SELECT SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 2), "
                "SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 2) FROM "
                f"(SELECT SDO_AGGR_MBR(t.{quote_ident(l.geom_column)}) AS g FROM {l.table_sql('t')})",
                max_rows=1, timeout_ms=3000,
            )
            if not r.rows or r.rows[0][0] is None:
                return None
            x0, y0, x1, y1 = (float(v) for v in r.rows[0])
            if l.srid not in WGS84_SRIDS:  # transform the two corners of the native MBR
                pts = []
                for x, y in ((x0, y0), (x1, y1)):
                    p = self.db.query(
                        f"SELECT SDO_GEOM.SDO_MIN_MBR_ORDINATE(p, 1), SDO_GEOM.SDO_MIN_MBR_ORDINATE(p, 2) FROM "
                        f"(SELECT SDO_CS.TRANSFORM({_point_sql(x, y, l.srid)}, 4326) AS p FROM DUAL)",
                        max_rows=1, timeout_ms=3000,
                    ).rows[0]
                    pts.append((float(p[0]), float(p[1])))
                (x0, y0), (x1, y1) = pts
            l.extent = [round(v, 5) for v in (x0, y0, x1, y1)]
        except (MapsqlError, TypeError, ValueError):
            l.extent = None
        return l.extent

    def describe_layer(self, layer: str, include_value_samples: bool = True) -> dict:
        with self.db.batch(timeout_ms=max(self.settings.statement_timeout_ms, 20000)):
            return self._describe_layer(layer, include_value_samples)

    def _describe_layer(self, layer: str, include_value_samples: bool) -> dict:
        l = self.catalog.resolve(layer)
        out = l.summary()
        out["columns"] = [{"name": c.display, "type": c.type} for c in l.columns]
        if l.other_geom_columns:
            out["other_geometry_columns"] = l.other_geom_columns
        if l.crs.kind != "unknown":
            out["crs_kind"] = l.crs.kind + (" (metres)" if l.crs.metre else "")
        t = l.table_sql("t")
        tmo = max(self.settings.statement_timeout_ms, 20000)
        r = self.db.query(
            f"SELECT n, SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 2), "
            f"SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 2) FROM "
            f"(SELECT COUNT(*) AS n, SDO_AGGR_MBR({l.to_4326('t')}) AS g FROM {t})",
            max_rows=1, timeout_ms=tmo,
        )
        n = r.rows[0][0]
        out["row_count"] = n
        if r.rows[0][1] is not None:
            out["extent_wgs84"] = [round(float(x), 6) for x in r.rows[0][1:5]]
        gt = self.db.query(
            f"SELECT MOD(s.g.SDO_GTYPE, 100), COUNT(*) FROM (SELECT t.{quote_ident(l.geom_column)} AS g FROM {t} "
            f"WHERE t.{quote_ident(l.geom_column)} IS NOT NULL FETCH FIRST 2000 ROWS ONLY) s GROUP BY MOD(s.g.SDO_GTYPE, 100)",
            max_rows=10,
        )
        from .oracle_layers import _GTYPE_NAMES

        out["geometry_types_in_sample"] = {_GTYPE_NAMES.get(int(k), str(k)): v for k, v in gt.rows}
        if l.crs.kind != "unknown":
            out["area_length_units"] = "metres (geodetic and projected CRS are handled with unit=M)"
        if not l.has_index:
            out["warning"] = "No usable spatial index: SDO_* operators cannot be used; the tools fall back to slower SDO_GEOM functions."
        if include_value_samples and (n or 0) <= 3_000_000:
            samples: dict[str, Any] = {}
            text_cols = [c for c in l.columns if c.is_text][:6]
            for c in text_cols:
                try:
                    cc = f"t.{quote_ident(c.name)}"
                    rr = self.db.query(
                        f"SELECT v, n FROM (SELECT {cc} AS v, COUNT(*) AS n FROM {t} WHERE {cc} IS NOT NULL "
                        "GROUP BY " + cc + " ORDER BY 2 DESC, 1) FETCH FIRST 6 ROWS ONLY",
                        max_rows=6, timeout_ms=5000,
                    )
                    samples[c.display] = [{"value": json_safe(v, 60), "count": k} for v, k in rr.rows]
                except MapsqlError:
                    continue
            if samples:
                out["top_values"] = samples
            num_cols = [c for c in l.columns if c.is_numeric][:6]
            if num_cols:
                parts = []
                for c in num_cols:
                    ci = f"t.{quote_ident(c.name)}"
                    parts.append(f"MIN({ci}), MAX({ci}), CAST(AVG({ci}) AS BINARY_DOUBLE)")
                try:
                    rr = self.db.query(f"SELECT {', '.join(parts)} FROM {t}", max_rows=1, timeout_ms=8000)
                    row = rr.rows[0]
                    stats = {}
                    for i, c in enumerate(num_cols):
                        mn, mx, av = row[3 * i: 3 * i + 3]
                        stats[c.display] = {"min": json_safe(mn), "max": json_safe(mx), "avg": json_safe(av)}
                    out["numeric_stats"] = stats
                except MapsqlError:
                    pass
        return out

    # --------------------------------------------------------------- retrieval

    def sample_features(self, layer: str, n: int = 5, geometry: GeometryMode = "auto") -> dict:
        l = self.catalog.resolve(layer)
        n = max(1, min(int(n), 50))
        order = "DBMS_RANDOM.VALUE" if (l.row_estimate or 0) <= 100_000 else None
        try:
            return self._feature_query(
                l, title=f"sample of {l.qualified}", where=[], binds=Binds(), order=order, limit=n, geometry=geometry,
                count_total=False, report_truncation=False,
            )
        except MapsqlError:
            if order is None:
                raise
            return self._feature_query(  # DBMS_RANDOM not executable for this user: take the first rows
                l, title=f"sample of {l.qualified}", where=[], binds=Binds(), order=None, limit=n, geometry=geometry,
                count_total=False, report_truncation=False,
            )

    def find_features(
        self,
        layer: str,
        filters: list[Filter] | None = None,
        columns: list[str] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        geometry: GeometryMode = "auto",
    ) -> dict:
        l = self.catalog.resolve(layer)
        b = Binds()
        order = None
        if order_by:
            c = l.find_column(order_by)
            if c.is_spatial or not c.is_attribute:
                raise MapsqlError("Cannot order by a geometry column.")
            order = f"t.{quote_ident(c.name)} {'DESC' if descending else 'ASC'} NULLS LAST"
        return self._feature_query(
            l,
            title=f"{l.qualified}" + (" (filtered)" if filters else ""),
            where=[compile_filters(l, filters, "t", b)],
            binds=b, order=order, limit=self._limit(limit), columns=columns, geometry=geometry,
        )

    def _dist_pred(self, l: OraLayer, ref_sql: str, d: float, alias: str = "t") -> str:
        g = l.geom(alias)
        if l.has_index:
            return f"SDO_WITHIN_DISTANCE({g}, {ref_sql}, 'distance={d:.4f} unit=M') = 'TRUE'"
        return f"SDO_GEOM.WITHIN_DISTANCE({g}, {num_lit(d)}, {ref_sql}, {num_lit(l.tolerance)}, 'unit=M') = 'TRUE'"

    def _dist_expr(self, l: OraLayer, ref_sql: str, alias: str = "t") -> str:
        return f"SDO_GEOM.SDO_DISTANCE({l.geom(alias)}, {ref_sql}, {num_lit(l.tolerance)}, 'unit=M')"

    def within_distance(
        self,
        layer: str,
        distance_m: float,
        lon: float | None = None,
        lat: float | None = None,
        reference_layer: str | None = None,
        reference_filters: list[Filter] | None = None,
        use_all_matches: bool = False,
        filters: list[Filter] | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        geometry: GeometryMode = "auto",
    ) -> dict:
        l = self.catalog.resolve(layer)
        d = _num("distance_m", distance_m, 0, MAX_DISTANCE_M)
        l.require_metric()
        b = Binds()
        ref = self._reference(lon, lat, reference_layer, reference_filters, b, use_all_matches)
        refl = ref.for_layer(l)
        return self._feature_query(
            l,
            title=f"{l.qualified} within {d:g} m of {ref.description}",
            where=[self._dist_pred(l, refl, d), compile_filters(l, filters, "t", b)],
            binds=b, order='"distance_m"', limit=self._limit(limit), columns=columns, geometry=geometry,
            select_extra=[(f"ROUND({self._dist_expr(l, refl)}, 1)", "distance_m")],
            extra_meta={"reference": ref.description, "distance_m": d},
            notes=self._no_index_note(l),
        )

    def nearest(
        self,
        layer: str,
        k: int = 5,
        lon: float | None = None,
        lat: float | None = None,
        reference_layer: str | None = None,
        reference_filters: list[Filter] | None = None,
        use_all_matches: bool = False,
        max_distance_m: float | None = None,
        filters: list[Filter] | None = None,
        columns: list[str] | None = None,
        geometry: GeometryMode = "auto",
    ) -> dict:
        l = self.catalog.resolve(layer)
        k = max(1, min(int(k), 100))
        l.require_metric()
        b = Binds()
        ref = self._reference(lon, lat, reference_layer, reference_filters, b, use_all_matches)
        refl = ref.for_layer(l)
        fwhere = compile_filters(l, filters, "t", b)
        where: list[str | None] = []
        if max_distance_m is not None:
            md = _num("max_distance_m", max_distance_m, 0, MAX_DISTANCE_M)
            where += [self._dist_pred(l, refl, md), fwhere]
            dist = f"ROUND({self._dist_expr(l, refl)}, 1)"
        elif l.has_index:
            # SDO_NN: the ancillary SDO_NN_DISTANCE(1) is the distance. With attribute filters the result count is
            # not known up front, so use incremental batches (sdo_batch_size) and let FETCH FIRST stop it.
            params = f"sdo_batch_size={max(k * 4, 20)} unit=M" if fwhere else f"sdo_num_res={k} unit=M"
            where += [f"SDO_NN({l.geom('t')}, {refl}, '{params}', 1) = 'TRUE'", fwhere]
            dist = "ROUND(SDO_NN_DISTANCE(1), 1)"
        else:
            where.append(fwhere)
            dist = f"ROUND({self._dist_expr(l, refl)}, 1)"
        return self._feature_query(
            l,
            title=f"{k} nearest {l.qualified} to {ref.description}",
            where=where, binds=b, order='"distance_m"', limit=k, columns=columns, geometry=geometry,
            select_extra=[(dist, "distance_m")], count_total=False, report_truncation=False,
            extra_meta={"reference": ref.description}, notes=self._no_index_note(l),
        )

    def _arc(self, l: OraLayer | None, d: float) -> str:
        """Densification tolerance for buffers of geodetic data (metres): ~0.2% of the radius."""
        if l is not None and not l.geodetic:
            return ""
        return f" arc_tolerance={max(d * 0.002, 0.1):.4f}"

    @staticmethod
    def _densify(l: OraLayer, expr: str, d: float, tol: str) -> str:
        """Projected-CRS buffers contain circular arcs, which SDO_CS.TRANSFORM/TO_GEOJSON cannot handle: densify them."""
        if l.geodetic:
            return expr
        return f"SDO_GEOM.SDO_ARC_DENSIFY({expr}, {tol}, 'arc_tolerance={max(d * 0.002, 0.1):.4f}')"

    def buffer(
        self,
        distance_m: float,
        layer: str | None = None,
        filters: list[Filter] | None = None,
        lon: float | None = None,
        lat: float | None = None,
        dissolve: bool = True,
        limit: int | None = None,
    ) -> dict:
        d = _num("distance_m", distance_m, 0.001, MAX_DISTANCE_M)
        if layer is None:
            if lon is None or lat is None:
                raise NeedsClarification(
                    "buffer needs something to buffer: a layer (+filters) or lon+lat. Ask the user what to buffer."
                )
            g = f"SDO_GEOM.SDO_BUFFER({self._point(lon, lat)}, {num_lit(d)}, 0.05, 'unit=M{self._arc(None, d)}')"
            q = (
                f'SELECT 1 AS n, ROUND(SDO_GEOM.SDO_AREA(g, 0.05, \'unit=SQ_KM\'), 4) AS area_km2, {_geojson("g")} AS "__geom" '
                f"FROM (SELECT {g} AS g FROM DUAL)"
            )
            res = self.db.query(q, max_rows=1)
            fc = self._to_fc(QueryResult(["n_features", "area_km2", "__geom"], [], [(1, res.rows[0][1], res.rows[0][2])]))
            return self._finish(fc, f"{d:g} m buffer around point", "auto", {"distance_m": d, "count": 1}, q)
        l = self.catalog.resolve(layer)
        l.require_metric()
        b = Binds()
        where = _where([compile_filters(l, filters, "t", b)])
        tol = num_lit(l.tolerance)
        params = f"unit=M{self._arc(l, d)}"
        to4326 = (lambda x: x) if (l.srid in WGS84_SRIDS or not l.srid) else (lambda x: f"SDO_CS.TRANSFORM({x}, 4326)")
        if dissolve:
            ubuf = self._densify(l, f"SDO_GEOM.SDO_BUFFER(u, {num_lit(d)}, {tol}, '{params}')", d, tol)
            q = (
                f'SELECT n, ROUND(SDO_GEOM.SDO_AREA(g, 0.05, \'unit=SQ_KM\'), 4) AS area_km2, {_geojson("g")} AS "__geom" FROM '
                f"(SELECT n, {to4326('bg')} AS g FROM (SELECT n, {ubuf} AS bg FROM "
                f"(SELECT COUNT(*) AS n, SDO_AGGR_UNION(SDOAGGRTYPE(x.g, {tol})) AS u FROM "
                f"(SELECT t.{quote_ident(l.geom_column)} AS g FROM {l.table_sql('t')}{where} FETCH FIRST 20000 ROWS ONLY) x)))"
            )
            res = self.db.query(q, b.values, max_rows=1)
            if not res.rows or not res.rows[0][0]:
                raise MapsqlError("No features matched the filters; nothing to buffer.")
            row = res.rows[0]
            fc = self._to_fc(QueryResult(["n_source_features", "area_km2", "__geom"], [], [tuple(row)]))
            return self._finish(
                fc, f"{d:g} m buffer of {l.qualified}", "auto",
                {"layer": l.qualified, "distance_m": d, "dissolved": True, "count": 1}, q,
            )
        lim = min(self._limit(limit), 500)
        cols = [c.name for c in l.attribute_columns[:6]]
        inner = [f"{l.col(c, 't')} AS {quote_ident(display_name(c))}" for c in cols]
        buf = self._densify(l, f"SDO_GEOM.SDO_BUFFER(t.{quote_ident(l.geom_column)}, {num_lit(d)}, {tol}, '{params}')", d, tol)
        inner.append(f"{to4326(buf)} AS g")
        outer = [quote_ident(display_name(c)) for c in cols]
        outer.append("ROUND(SDO_GEOM.SDO_AREA(g, 0.05, 'unit=SQ_KM'), 4) AS area_km2")
        outer.append(f'{_geojson("g")} AS "__geom"')
        q = (
            f"SELECT {', '.join(outer)} FROM (SELECT {', '.join(inner)} FROM {l.table_sql('t')}{where} "
            f"FETCH FIRST {lim + 1} ROWS ONLY)"
        )
        res = self.db.query(q, b.values, max_rows=lim)
        fc = self._to_fc(res)
        return self._finish(
            fc, f"{d:g} m buffers of {l.qualified}", "auto",
            {"layer": l.qualified, "distance_m": d, "dissolved": False, "count": len(fc["features"]), "truncated": res.truncated},
            q,
        )

    def _window(self, bbox: list[float] | None, geojson_geometry: dict | None, binds: Binds) -> tuple[str, str]:
        """(SQL for a WGS84 window geometry, description)."""
        if bbox is not None:
            if len(bbox) != 4:
                raise MapsqlError("bbox must be [min_lon, min_lat, max_lon, max_lat].")
            x1, y1, x2, y2 = (_num("bbox", v) for v in bbox)
            if x1 > x2 or y1 > y2:
                raise MapsqlError("bbox must satisfy min <= max for both axes.")
            ring = ", ".join(num_lit(v) for v in (x1, y1, x2, y1, x2, y2, x1, y2, x1, y1))
            return (
                f"SDO_GEOMETRY(2003, 4326, NULL, SDO_ELEM_INFO_ARRAY(1, 1003, 1), SDO_ORDINATE_ARRAY({ring}))",
                "bbox",
            )
        try:
            from shapely.geometry import mapping, shape
            from shapely.geometry.polygon import orient

            g = shape(geojson_geometry)
        except Exception as e:  # noqa: BLE001
            raise MapsqlError(f"geojson_geometry is not a valid GeoJSON geometry ({type(e).__name__}).") from None
        if g.geom_type == "Polygon":
            g = orient(g, 1.0)  # Oracle wants counter-clockwise exteriors for geodetic polygons
        elif g.geom_type == "MultiPolygon":
            from shapely.geometry import MultiPolygon

            g = MultiPolygon([orient(p, 1.0) for p in g.geoms])
        gj = json.dumps(mapping(g))
        if len(gj) > 200_000:
            raise MapsqlError("geojson_geometry is too large (max 200 KB).")
        return f"SDO_UTIL.FROM_GEOJSON({binds.add(gj)}, NULL, 4326)", "given geometry"

    def spatial_filter(
        self,
        layer: str,
        relation: Relation = "intersects",
        other_layer: str | None = None,
        other_filters: list[Filter] | None = None,
        bbox: list[float] | None = None,
        geojson_geometry: dict | None = None,
        filters: list[Filter] | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        geometry: GeometryMode = "auto",
    ) -> dict:
        """Features of ``layer`` that satisfy ``<feature> <relation> <other>``."""
        l = self.catalog.resolve(layer)
        if relation == "crosses":
            raise MapsqlError(
                "relation 'crosses' has no exact Oracle Spatial equivalent (SDO_RELATE masks differ from the OGC "
                "definition). Use 'intersects', 'overlaps' or 'touches', or run_sql with an explicit SDO_RELATE mask."
            )
        if relation not in _MASKS and relation != "disjoint":
            raise MapsqlError(f"Unknown relation {relation!r}. Use one of: {', '.join([*_MASKS, 'disjoint'])}.")
        operands = sum(x is not None and x != [] for x in (other_layer, bbox, geojson_geometry))
        if operands == 0:
            raise NeedsClarification(
                "Intersect/contain WITH WHAT? Provide other_layer (+other_filters), a bbox "
                "[min_lon,min_lat,max_lon,max_lat], or a GeoJSON geometry. Ask the user if unclear."
            )
        if operands > 1:
            raise NeedsClarification("Give exactly one of other_layer, bbox or geojson_geometry.")
        b = Binds()
        tol = num_lit(l.tolerance)
        notes = self._no_index_note(l)
        if other_layer:
            o = self.catalog.resolve(other_layer)
            if o.qualified == l.qualified and not other_filters:
                raise NeedsClarification("other_filters are needed when comparing a layer with itself.")
            ow = compile_filters(o, other_filters, "o", b)
            lg_in_o = l.geom_as("t", o.srid)
            if relation == "disjoint":
                inner = f"SDO_RELATE({o.geom('o')}, {lg_in_o}, 'mask=ANYINTERACT') = 'TRUE'" if o.has_index else (
                    f"SDO_GEOM.RELATE({l.geom('t')}, 'ANYINTERACT', {o.geom_as('o', l.srid)}, {tol}) = 'TRUE'"
                )
            else:
                fwd, inv = _MASKS[relation]
                inner = f"SDO_RELATE({o.geom('o')}, {lg_in_o}, 'mask={inv}') = 'TRUE'" if o.has_index else (
                    f"SDO_GEOM.RELATE({l.geom('t')}, '{fwd}', {o.geom_as('o', l.srid)}, {tol}) <> 'FALSE'"
                )
            if not o.has_index:
                notes += self._no_index_note(o)
            pred = f"{'NOT ' if relation == 'disjoint' else ''}EXISTS (SELECT 1 FROM {o.table_sql('o')}{_where([inner, ow])})"
            desc = f"{o.qualified}" + (" (filtered)" if other_filters else "")
        else:
            win, desc = self._window(bbox, geojson_geometry, b)
            win_l = win if (not l.srid or l.srid == 4326) else f"SDO_CS.TRANSFORM({win}, {int(l.srid)})"
            g = l.geom("t")
            if relation == "disjoint":
                pred = f"SDO_GEOM.RELATE({g}, 'ANYINTERACT', {win_l}, {tol}) = 'FALSE'"
                notes.append("'disjoint' against a window uses the index-free SDO_GEOM.RELATE.")
            else:
                fwd, _inv = _MASKS[relation]
                pred = (f"SDO_RELATE({g}, {win_l}, 'mask={fwd}') = 'TRUE'" if l.has_index
                        else f"SDO_GEOM.RELATE({g}, '{fwd}', {win_l}, {tol}) <> 'FALSE'")
        return self._feature_query(
            l,
            title=f"{l.qualified} {relation} {desc}",
            where=[pred, compile_filters(l, filters, "t", b)],
            binds=b, limit=self._limit(limit), columns=columns, geometry=geometry,
            extra_meta={"relation": relation, "other": desc}, notes=notes,
        )

    def bbox_of(self, layer: str, filters: list[Filter] | None = None) -> dict:
        l = self.catalog.resolve(layer)
        b = Binds()
        w = _where([compile_filters(l, filters, "t", b)])
        q = (
            "SELECT n, SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MIN_MBR_ORDINATE(g, 2), "
            "SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 1), SDO_GEOM.SDO_MAX_MBR_ORDINATE(g, 2) FROM "
            f"(SELECT COUNT(*) AS n, SDO_AGGR_MBR({l.to_4326('t')}) AS g FROM {l.table_sql('t')}{w})"
        )
        row = self.db.query(q, b.values, max_rows=1).rows[0]
        n, box = row[0], row[1]
        if not n or box is None:
            return {"layer": l.qualified, "count": n or 0, "bbox": None, "note": "No features with geometry matched."}
        minx, miny, maxx, maxy = (round(float(v), 6) for v in row[1:5])
        poly = {"type": "Polygon", "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]}
        fc = {"type": "FeatureCollection", "features": [make_feature({"layer": l.qualified, "features": n}, poly)]}
        rid = self.store.put(fc, f"bbox of {l.qualified}")
        return {
            "layer": l.qualified, "count": n, "bbox": [minx, miny, maxx, maxy], "crs": "EPSG:4326",
            "center": [round((minx + maxx) / 2, 6), round((miny + maxy) / 2, 6)], "result_id": rid,
        }

    # -------------------------------------------------------------- analysis

    def _metric_sql(self, m: Metric, layer: OraLayer) -> tuple[str, str]:
        fn = m.fn
        if fn == "count":
            return "COUNT(*)", m.alias or "count"
        if fn in ("area_km2", "length_km"):
            layer.require_metric()
            g = layer.geom("t")
            tol = num_lit(layer.tolerance)
            any_geom = layer.geom_type in ("GEOMETRY", "GEOMETRYCOLLECTION")
            if fn == "area_km2":
                if "POLYGON" not in layer.geom_type and not any_geom:
                    raise MapsqlError(f"area_km2 needs a polygon layer; {layer.qualified} is {layer.geom_type}.")
                return f"ROUND(SUM(SDO_GEOM.SDO_AREA({g}, {tol}, 'unit=SQ_KM')), 3)", m.alias or "area_km2"
            if "LINE" not in layer.geom_type and not any_geom:
                raise MapsqlError(f"length_km needs a line layer; {layer.qualified} is {layer.geom_type}.")
            return f"ROUND(SUM(SDO_GEOM.SDO_LENGTH({g}, {tol}, 'unit=KM')), 3)", m.alias or "length_km"
        if not m.column:
            raise NeedsClarification(f"Metric {fn!r} needs a `column`.")
        c = layer.find_column(m.column)
        if c.is_spatial:
            raise MapsqlError("Metrics on geometry columns: use area_km2 / length_km.")
        ident = f"t.{quote_ident(c.name)}"
        if fn == "count_distinct":
            return f"COUNT(DISTINCT {ident})", m.alias or f"distinct_{c.display}"
        if fn in ("sum", "avg") and not c.is_numeric:
            raise MapsqlError(f"{fn}() needs a numeric column; {c.display!r} is {c.type}.")
        expr = f"{fn.upper()}({ident})"
        if fn in ("sum", "avg"):
            expr = f"CAST({expr} AS BINARY_DOUBLE)"
        return expr, m.alias or f"{fn}_{c.display}"

    def aggregate(
        self,
        layer: str,
        metrics: list[Metric] | None = None,
        group_by: list[str] | None = None,
        filters: list[Filter] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = None,
    ) -> dict:
        l = self.catalog.resolve(layer)
        metrics = metrics or [Metric(fn="count")]
        sel: list[str] = []
        names: list[str] = []
        group_cols: list[str] = []
        for g in group_by or []:
            c = l.find_column(g)
            if c.is_spatial or not c.is_attribute:
                raise MapsqlError("Cannot group by a geometry column.")
            ident = f"t.{quote_ident(c.name)}"
            sel.append(f"{ident} AS {quote_ident(c.display)}")
            names.append(c.display)
            group_cols.append(ident)
        for m in metrics:
            expr, alias = self._metric_sql(m, l)
            if alias in names:
                alias = f"{alias}_{len(names)}"
            sel.append(f"{expr} AS {quote_ident(alias)}")
            names.append(alias)
        order = ""
        if order_by:
            if order_by not in names:
                raise MapsqlError(f"order_by must be one of the output columns: {names}")
            order = f" ORDER BY {quote_ident(order_by)} {'DESC' if descending else 'ASC'} NULLS LAST"
        elif group_cols:
            order = f" ORDER BY {quote_ident(names[len(group_cols)])} {'DESC' if descending else 'ASC'} NULLS LAST"
        lim = self._limit(limit)
        b = Binds()
        q = (
            f"SELECT {', '.join(sel)} FROM {l.table_sql('t')}{_where([compile_filters(l, filters, 't', b)])}"
            + (" GROUP BY " + ", ".join(group_cols) if group_cols else "")
            + order + f" FETCH FIRST {lim + 1} ROWS ONLY"
        )
        res = self.db.query(q, b.values, max_rows=lim)
        rows = [{k: json_safe(v) for k, v in zip(res.columns, r)} for r in res.rows]
        return {
            "layer": l.qualified, "columns": res.columns, "rows": rows, "row_count": len(rows),
            "truncated": res.truncated, "sql": _sql_text(q),
        }

    def _join_pred(self, a: OraLayer, b_: OraLayer, relation: str, d: float | None, ta: str = "t", tb: str = "r") -> str:
        """Predicate relating RIGHT (``tb``) features to the LEFT (``ta``) feature; ``tb`` is the (indexed) first argument."""
        a_in_b = a.geom_as(ta, b_.srid)
        bg = b_.geom(tb)
        if relation == "dwithin":
            assert d is not None
            if b_.has_index:
                return f"SDO_WITHIN_DISTANCE({bg}, {a_in_b}, 'distance={d:.4f} unit=M') = 'TRUE'"
            return f"SDO_GEOM.WITHIN_DISTANCE({bg}, {num_lit(d)}, {a_in_b}, {num_lit(b_.tolerance)}, 'unit=M') = 'TRUE'"
        # left <relation> right, seen from the right feature's side
        fwd, inv = _MASKS[relation]
        mask_right_first = inv  # right is the first argument: invert the left-side relation
        if b_.has_index:
            return f"SDO_RELATE({bg}, {a_in_b}, 'mask={mask_right_first}') = 'TRUE'"
        return f"SDO_GEOM.RELATE({a.geom(ta)}, '{fwd}', {b_.geom_as(tb, a.srid)}, {num_lit(a.tolerance)}) <> 'FALSE'"

    def spatial_join(
        self,
        left_layer: str,
        right_layer: str,
        relation: Literal["intersects", "within", "contains", "dwithin"] = "intersects",
        distance_m: float | None = None,
        mode: Literal["count", "sum", "avg", "attach"] = "count",
        right_column: str | None = None,
        right_columns: list[str] | None = None,
        left_filters: list[Filter] | None = None,
        right_filters: list[Filter] | None = None,
        order_desc: bool = True,
        min_count: int | None = None,
        limit: int | None = None,
        geometry: GeometryMode = "auto",
    ) -> dict:
        """For each LEFT feature, summarise the RIGHT features that relate to it."""
        a = self.catalog.resolve(left_layer)
        bl = self.catalog.resolve(right_layer)
        d: float | None = None
        if relation == "dwithin":
            if distance_m is None:
                raise NeedsClarification("relation 'dwithin' needs distance_m (metres). Ask the user for the distance.")
            d = _num("distance_m", distance_m, 0, MAX_DISTANCE_M)
            a.require_metric()
            bl.require_metric()
        elif relation not in ("intersects", "within", "contains"):
            raise MapsqlError("relation must be one of: intersects, within, contains, dwithin.")
        lim = self._limit(limit)
        b = Binds()
        pred = self._join_pred(a, bl, relation, d)
        rwhere = _where([pred, compile_filters(bl, right_filters, "r", b)])
        lw = compile_filters(a, left_filters, "t", b)
        notes = self._no_index_note(bl)
        lcols = [c.name for c in a.attribute_columns[:8 if mode == "attach" else 10]]
        sel = [f"{a.col(c, 't')} AS {quote_ident(display_name(c))}" for c in lcols]

        if mode == "attach":
            rcols = self._columns(bl, right_columns)[:8]
            for c in rcols:
                sel.append(f"{bl.col(c, 'r')} AS {quote_ident('right_' + display_name(c))}")
            sel.append(f'{_geojson(a.to_4326("t"))} AS "__geom"')
            # the predicate is written for the lateral/EXISTS form; here r joins t
            w = _where([pred, compile_filters(bl, right_filters, "r", b) if right_filters else None, lw])
            q = (f"SELECT {', '.join(sel)} FROM {a.table_sql('t')}, {bl.table_sql('r')}{w} "
                 f"FETCH FIRST {lim + 1} ROWS ONLY")
            res = self.db.query(q, b.values, max_rows=lim)
            fc = self._to_fc(res)
            return self._finish(
                fc, f"{a.qualified} joined with {bl.qualified}", geometry,
                {"left_layer": a.qualified, "right_layer": bl.qualified, "mode": mode, "count": len(fc["features"]),
                 "truncated": res.truncated, "limit": lim}, q, notes,
            )

        agg = ['COUNT(*) AS "right_count"']
        extra_names: list[str] = []
        if mode in ("sum", "avg"):
            if not right_column:
                raise NeedsClarification(f"mode={mode!r} needs right_column (a numeric column of {bl.qualified}).")
            rc = bl.find_column(right_column)
            if not rc.is_numeric:
                raise MapsqlError(f"right_column {rc.display!r} is not numeric ({rc.type}).")
            nm = f"right_{mode}_{rc.display}"
            agg.append(f"CAST({mode.upper()}(r.{quote_ident(rc.name)}) AS BINARY_DOUBLE) AS {quote_ident(nm)}")
            extra_names.append(nm)
        order_name = "right_count" if mode == "count" else extra_names[0]
        direction = "DESC" if order_desc else "ASC"
        if a.kind != "view":
            # Join form: one set-based spatial join, grouped by ROWID. Oracle runs this through its (fast) spatial join
            # plan; the per-row correlated (LATERAL) form below is 10-100x slower because every row starts a new
            # index probe.
            pred_l = self._join_pred(a, bl, relation, d, ta="l")
            inner_where = _where([pred_l, compile_filters(bl, right_filters, "r", b), compile_filters(a, left_filters, "l", b)])
            inner_agg = ", ".join(agg)
            sel.append('NVL(s."right_count", 0) AS "right_count"')
            for nm in extra_names:
                sel.append(f"s.{quote_ident(nm)}")
            sel.append(f'{_geojson(a.to_4326("t"))} AS "__geom"')
            wh = [compile_filters(a, left_filters, "t", b)] + ([f'NVL(s."right_count", 0) >= {int(min_count)}'] if min_count else [])
            q = (
                f"SELECT {', '.join(sel)} FROM {a.table_sql('t')} LEFT JOIN "
                f"(SELECT l.ROWID AS rid, {inner_agg} FROM {a.table_sql('l')}, {bl.table_sql('r')}{inner_where} GROUP BY l.ROWID) s "
                f"ON s.rid = t.ROWID{_where(wh)} "
                f"ORDER BY {'NVL(s.' + quote_ident(order_name) + ', 0)' if mode == 'count' else 's.' + quote_ident(order_name)} "
                f"{direction} NULLS LAST FETCH FIRST {lim + 1} ROWS ONLY"
            )
        else:
            sel.append('s."right_count"')
            for nm in extra_names:
                sel.append(f"s.{quote_ident(nm)}")
            sel.append(f'{_geojson(a.to_4326("t"))} AS "__geom"')
            wh = [lw] + ([f's."right_count" >= {int(min_count)}'] if min_count else [])
            notes.append("The left layer is a view (no ROWID): the slower per-row join plan was used.")
            q = (
                f"SELECT {', '.join(sel)} FROM {a.table_sql('t')} CROSS JOIN LATERAL "
                f"(SELECT {', '.join(agg)} FROM {bl.table_sql('r')}{rwhere}) s{_where(wh)} "
                f"ORDER BY s.{quote_ident(order_name)} {direction} NULLS LAST FETCH FIRST {lim + 1} ROWS ONLY"
            )
        res = self.db.query(q, b.values, max_rows=lim, timeout_ms=max(self.settings.statement_timeout_ms, 60000))
        fc = self._to_fc(res)
        return self._finish(
            fc, f"{a.qualified} x {bl.qualified} ({mode})", geometry,
            {"left_layer": a.qualified, "right_layer": bl.qualified, "relation": relation, "mode": mode,
             "count": len(fc["features"]), "truncated": res.truncated, "limit": lim}, q, notes,
        )

    # ---------------------------------------------------------------- raw SQL

    def run_sql(self, query: str, limit: int | None = None, geometry: GeometryMode = "auto") -> dict:
        """Guarded, read-only SQL. SDO_GEOMETRY columns are returned as GeoJSON (EPSG:4326)."""
        validate_oracle_sql(query, self.settings, known=self._known_for_guard())
        with self.db.batch():
            return self._run_sql(query, limit, geometry)

    def _run_sql(self, query: str, limit: int | None, geometry: GeometryMode) -> dict:
        lim = self._limit(limit)
        q = query.strip().rstrip(";").strip()
        cols = self.db.describe(q)
        names = [c.name for c in cols]
        geom_idx = [i for i, c in enumerate(cols) if c.is_geometry]
        if geom_idx and len(set(names)) != len(names):
            raise MapsqlError("Result has duplicate column names; alias them (AS ...) so geometries can be converted.")
        sel = []
        for i, c in enumerate(cols):
            ident = f"q.{quote_ident(c.name)}"
            if i in geom_idx:
                g = (f"CASE WHEN {ident}.SDO_SRID IS NULL OR {ident}.SDO_SRID IN (4326, 8307) THEN {ident} "
                     f"ELSE SDO_CS.TRANSFORM({ident}, 4326) END")
                sel.append(f"{_geojson(g)} AS {quote_ident(c.name)}")
            else:
                sel.append(f"{ident} AS {quote_ident(c.name)}")
        wrapped = f"SELECT {', '.join(sel)} FROM (\n{q}\n) q FETCH FIRST {lim + 1} ROWS ONLY"
        res = self.db.query(wrapped, max_rows=lim)
        shown = [display_name(n) for n in names]
        out: dict[str, Any] = {"columns": shown, "row_count": len(res.rows), "truncated": res.truncated, "limit": lim}
        if not geom_idx:
            out["rows"] = [{k: json_safe(v) for k, v in zip(shown, r)} for r in res.rows]
            if res.truncated:
                out["note"] = "Truncated at the row limit; refine the query or raise 'limit'."
            return out
        gi = geom_idx[0]
        feats = []
        for r in res.rows:
            props = {k: v for i, (k, v) in enumerate(zip(shown, r)) if i not in geom_idx}
            feats.append(make_feature(props, parse_geojson(r[gi])))
        fc = {"type": "FeatureCollection", "features": feats}
        meta = {"columns": [n for i, n in enumerate(shown) if i not in geom_idx], "geometry_column": shown[gi],
                "row_count": len(feats), "truncated": res.truncated, "limit": lim}
        if len(geom_idx) > 1:
            meta["note"] = f"Multiple geometry columns; only {shown[gi]!r} is returned as GeoJSON."
        return self._finish(fc, "SQL result", geometry, meta, None)

    def execute_sql(self, statement: str) -> dict:
        raise MapsqlError(
            "Write mode is not available for the Oracle backend (mapsql-mcp 0.2 is read-only on Oracle). "
            "Use a database tool of your choice for changes."
        )

    def _known_for_guard(self):
        if not self.settings.restricted:
            return None
        return self.catalog.known_for_guard()
