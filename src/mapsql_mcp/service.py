"""The spatial toolbox: every MCP tool is a thin wrapper around a method of :class:`GeoService`.

All SQL for the structured tools is built from validated identifiers (``psycopg.sql.Identifier``)
and escaped literals - the model never gets to inject SQL through them. Free-form SQL goes
through :mod:`mapsql_mcp.guard`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import sql

from .config import Settings
from .db import Database, QueryResult, json_safe
from .errors import MapsqlError, NeedsClarification
from .geo import ResultStore, collection_bbox, geojson_sql, make_feature, shape_response
from .guard import validate_sql
from .layers import Catalog, Filter, Layer, compile_filters

GeometryMode = Literal["auto", "full", "centroid", "none"]
Relation = Literal["intersects", "within", "contains", "touches", "crosses", "overlaps", "disjoint"]

_REL_FUNC = {
    "intersects": "ST_Intersects",
    "within": "ST_Within",
    "contains": "ST_Contains",
    "touches": "ST_Touches",
    "crosses": "ST_Crosses",
    "overlaps": "ST_Overlaps",
    "disjoint": "ST_Disjoint",
}

MAX_DISTANCE_M = 20_000_000.0


@dataclass
class Ref:
    """A reference geometry in EPSG:4326, usable as a scalar SQL expression."""

    g4326: sql.Composable
    description: str
    matched: int = 1


def _sql_text(q: sql.Composable) -> str:
    try:
        return q.as_string()
    except Exception:  # pragma: no cover
        return "<sql>"


def _where(parts: list[sql.Composable | None]) -> sql.Composable:
    parts = [p for p in parts if p is not None]
    if not parts:
        return sql.SQL("")
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(sql.SQL("({})").format(p) for p in parts)


def _num(name: str, v: Any, lo: float | None = None, hi: float | None = None) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise MapsqlError(f"{name} must be a number, got {v!r}.") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise MapsqlError(f"{name} must be a finite number.")
    if lo is not None and f < lo:
        raise MapsqlError(f"{name} must be >= {lo}.")
    if hi is not None and f > hi:
        raise MapsqlError(f"{name} must be <= {hi}.")
    return f


class GeoService:
    """PostGIS implementation of the backend contract (see ``backends/base.py``)."""

    backend = "postgis"

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings)
        self.catalog = Catalog(self.db)
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

    def _columns(self, layer: Layer, columns: list[str] | None) -> list[str]:
        if columns:
            out = []
            for c in columns:
                col = layer.find_column(c)
                if col.is_spatial:
                    continue
                out.append(col.name)
            return out
        return [c.name for c in layer.attribute_columns[:40]]

    def _point(self, lon: float, lat: float) -> sql.Composable:
        lon_f = _num("lon", lon)
        lat_f = _num("lat", lat)
        if not (-180 <= lon_f <= 180) or not (-90 <= lat_f <= 90):
            hint = ""
            if -180 <= lat_f <= 180 and -90 <= lon_f <= 90:
                hint = " (lon and lat look swapped: lon is the X/east-west value, lat the Y/north-south value)"
            raise MapsqlError(f"Coordinates out of range for WGS84: lon={lon_f}, lat={lat_f}.{hint}")
        return sql.SQL("ST_SetSRID(ST_MakePoint({}, {}), 4326)").format(sql.Literal(lon_f), sql.Literal(lat_f))

    def _reference(
        self,
        lon: float | None,
        lat: float | None,
        reference_layer: str | None,
        reference_filters: list[Filter] | None,
        use_all_matches: bool = False,
    ) -> Ref:
        has_pt = lon is not None or lat is not None
        if has_pt and (lon is None or lat is None):
            raise NeedsClarification("Both lon and lat are required for a reference point.")
        if has_pt and reference_layer:
            raise NeedsClarification(
                "Give either a reference point (lon, lat) or a reference feature (reference_layer + "
                "reference_filters), not both."
            )
        if has_pt:
            return Ref(self._point(lon, lat), f"point ({lon}, {lat})")  # type: ignore[arg-type]
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
        where = _where([compile_filters(rl, reference_filters, "r")])
        cols = [c.name for c in rl.attribute_columns[:6]]
        sel = sql.SQL(", ").join(sql.SQL("{} AS {}").format(rl.column(c, "r"), sql.Identifier(c)) for c in cols) or sql.SQL("1")
        res = self.db.query(
            sql.SQL("SELECT {sel} FROM {t} AS r{w} LIMIT 11").format(sel=sel, t=rl.table_sql(), w=where), max_rows=11
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
        sub = sql.SQL("(SELECT ST_Union({g}) FROM {t} AS r{w})").format(g=rl.to_4326("r"), t=rl.table_sql(), w=where)
        desc = f"{n if n <= 10 else '10+'} feature(s) of {rl.qualified}"
        return Ref(sub, desc, n)

    def _feature_query(
        self,
        layer: Layer,
        *,
        title: str,
        where: list[sql.Composable | None],
        order: sql.Composable | None = None,
        limit: int,
        columns: list[str] | None = None,
        geometry: GeometryMode = "auto",
        select_extra: list[tuple[sql.Composable, str]] | None = None,
        from_sql: sql.Composable | None = None,
        geom_expr: sql.Composable | None = None,
        count_total: bool = True,
        report_truncation: bool = True,
        extra_meta: dict[str, Any] | None = None,
    ) -> dict:
        cols = self._columns(layer, columns)
        sel: list[sql.Composable] = [
            sql.SQL("{} AS {}").format(layer.column(c, "t"), sql.Identifier(c)) for c in cols
        ]
        for expr, name in select_extra or []:
            sel.append(sql.SQL("{} AS {}").format(expr, sql.Identifier(name)))
        sel.append(sql.SQL("{} AS {}").format(geojson_sql(geom_expr or layer.to_4326("t")), sql.Identifier("__geom")))
        frm = from_sql if from_sql is not None else layer.table_sql("t")
        w = _where(where)
        q = sql.SQL("SELECT {sel} FROM {frm}{w}{order} LIMIT {lim}").format(
            sel=sql.SQL(", ").join(sel),
            frm=frm,
            w=w,
            order=(sql.SQL(" ORDER BY ") + order) if order is not None else sql.SQL(""),
            lim=sql.Literal(limit + 1),
        )
        res = self.db.query(q, max_rows=limit)
        fc = self._to_fc(res)
        total = None
        if res.truncated and count_total:
            try:
                cq = sql.SQL("SELECT count(*) FROM {frm}{w}").format(frm=frm, w=w)
                total = self.db.query(cq, max_rows=1, timeout_ms=min(self.settings.statement_timeout_ms, 8000)).rows[0][0]
            except MapsqlError:
                total = None
        meta: dict[str, Any] = {"layer": layer.qualified, "count": len(fc["features"])}
        if report_truncation:
            meta.update({"truncated": res.truncated, "limit": limit})
        if total is not None:
            meta["total_matches"] = total
        if extra_meta:
            meta.update(extra_meta)
        return self._finish(fc, title, geometry, meta, _sql_text(q))

    def _to_fc(self, res: QueryResult) -> dict:
        names = res.columns[:-1]
        feats = []
        for row in res.rows:
            props = dict(zip(names, row[:-1]))
            feats.append(make_feature(props, row[-1]))
        return {"type": "FeatureCollection", "features": feats}

    def _finish(self, fc: dict, title: str, geometry: GeometryMode, meta: dict, sql_text: str | None) -> dict:
        bbox = collection_bbox(fc["features"])
        if bbox:
            fc["bbox"] = bbox
        rid = self.store.put(fc, title)
        shaped, note = shape_response(fc, geometry=geometry)
        out: dict[str, Any] = {"result_id": rid, **meta}
        if bbox:
            out["bbox"] = bbox
        if note:
            out["note"] = note
        out["type"] = "FeatureCollection"
        out["features"] = shaped["features"]
        if sql_text:
            out["sql"] = sql_text if len(sql_text) <= 1500 else sql_text[:1500] + " ..."
        if meta.get("truncated"):
            out.setdefault(
                "note",
                "Result truncated at the row limit. Add filters, raise 'limit', or aggregate instead of listing.",
            )
        return out

    # ------------------------------------------------------------- discovery

    def server_info(self) -> dict:
        v = self.db.server_versions()
        return {
            "name": "mapsql-mcp",
            "backend": "postgis",
            **v,
            "mode": "read-write" if self.settings.allow_write else "read-only",
            "statement_timeout_ms": self.settings.statement_timeout_ms,
            "default_limit": self.settings.default_limit,
            "max_rows": self.settings.max_rows,
            "allowed_schemas": list(self.settings.schemas) if self.settings.schemas else None,
            "allowed_layers": list(self.settings.layers) if self.settings.layers else None,
            "layers": len(self.catalog.layers()),
            "basemap": "none" if self.settings.basemap == "none" else "enabled",
        }

    def list_layers(self, schema: str | None = None, search: str | None = None) -> dict:
        layers = self.catalog.layers()
        if schema:
            layers = [l for l in layers if l.schema.lower() == schema.lower()]
        if search:
            s = search.lower()
            layers = [
                l for l in layers
                if s in l.qualified.lower() or any(s in c.name.lower() for c in l.columns)
            ]
        out = []
        for l in layers:
            item = l.summary()
            item["columns"] = [f"{c.name}:{_short_type(c.type)}" for c in l.columns if not c.is_spatial][:30]
            if l.other_geom_columns:
                item["other_geometry_columns"] = l.other_geom_columns
            ext = self._estimated_extent(l)
            if ext:
                item["extent_wgs84"] = ext
            out.append(item)
        return {"count": len(out), "layers": out}

    def _estimated_extent(self, l: Layer) -> list[float] | None:
        if l.kind in ("view", "foreign table") or l.is_geography or l.srid == 0:
            return None
        try:
            r = self.db.query(
                sql.SQL(
                    "SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b) FROM "
                    "(SELECT ST_Transform(ST_SetSRID(ST_EstimatedExtent({s}, {t}, {c})::geometry, {srid}), 4326) AS b) x"
                ).format(
                    s=sql.Literal(l.schema), t=sql.Literal(l.table), c=sql.Literal(l.geom_column),
                    srid=sql.Literal(l.srid),
                ),
                max_rows=1, timeout_ms=3000,
            )
            if r.rows and r.rows[0][0] is not None:
                return [round(float(x), 5) for x in r.rows[0]]
        except MapsqlError:
            return None
        return None

    def describe_layer(self, layer: str, include_value_samples: bool = True) -> dict:
        l = self.catalog.resolve(layer)
        out = l.summary()
        out["columns"] = [{"name": c.name, "type": c.type} for c in l.columns]
        if l.other_geom_columns:
            out["other_geometry_columns"] = l.other_geom_columns
        if l.crs.kind != "unknown":
            out["crs_kind"] = l.crs.kind + (" (metres)" if l.crs.metre else "")
        # exact count + extent (bounded by the statement timeout)
        t = l.table_sql("t")
        r = self.db.query(
            sql.SQL(
                "SELECT n, ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
                "(SELECT count(*) AS n, ST_Extent({g}) AS e FROM {t}) s"
            ).format(g=l.to_4326("t"), t=t),
            max_rows=1,
            timeout_ms=max(self.settings.statement_timeout_ms, 20000),
        )
        n = r.rows[0][0]
        out["row_count"] = n
        if r.rows[0][1] is not None:
            out["extent_wgs84"] = [round(float(x), 6) for x in r.rows[0][1:5]]
        gt = self.db.query(
            sql.SQL(
                "SELECT GeometryType(g), count(*) FROM (SELECT {g} AS g FROM {t} WHERE {g} IS NOT NULL LIMIT 2000) s GROUP BY 1"
            ).format(g=l.geom("t"), t=t),
            max_rows=10,
        )
        out["geometry_types_in_sample"] = {k: v for k, v in gt.rows}
        if l.crs.kind != "unknown":
            out["area_length_units"] = "metres (geography or metric CRS handled automatically)"
        if include_value_samples and (n or 0) <= 3_000_000:
            samples: dict[str, Any] = {}
            text_cols = [c for c in l.columns if c.is_attribute and c.type.split("(")[0] in ("text", "character varying", "character")][:6]
            for c in text_cols:
                try:
                    rr = self.db.query(
                        sql.SQL(
                            "SELECT v, n FROM (SELECT {c}::text AS v, count(*) AS n FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 "
                            "ORDER BY 2 DESC, 1 LIMIT 6) s"
                        ).format(c=l.column(c.name, "t"), t=t),
                        max_rows=6, timeout_ms=5000,
                    )
                    distinct_hint = [{"value": json_safe(v, 60), "count": k} for v, k in rr.rows]
                    samples[c.name] = distinct_hint
                except MapsqlError:
                    continue
            if samples:
                out["top_values"] = samples
            num_stats: dict[str, Any] = {}
            num_cols = [c for c in l.columns if c.is_numeric][:6]
            if num_cols:
                parts = []
                for c in num_cols:
                    ci = l.column(c.name, "t")
                    parts.append(sql.SQL("min({0}), max({0}), avg({0})::float8").format(ci))
                try:
                    rr = self.db.query(
                        sql.SQL("SELECT {p} FROM {t}").format(p=sql.SQL(", ").join(parts), t=t), max_rows=1, timeout_ms=8000
                    )
                    row = rr.rows[0]
                    for i, c in enumerate(num_cols):
                        mn, mx, av = row[3 * i: 3 * i + 3]
                        num_stats[c.name] = {"min": json_safe(mn), "max": json_safe(mx), "avg": json_safe(av)}
                    out["numeric_stats"] = num_stats
                except MapsqlError:
                    pass
        return out

    # --------------------------------------------------------------- retrieval

    def sample_features(self, layer: str, n: int = 5, geometry: GeometryMode = "auto") -> dict:
        l = self.catalog.resolve(layer)
        n = max(1, min(int(n), 50))
        order = sql.SQL("random()") if (l.row_estimate or 0) <= 100_000 else None
        return self._feature_query(
            l, title=f"sample of {l.qualified}", where=[], order=order, limit=n, geometry=geometry, count_total=False,
            report_truncation=False,
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
        order = None
        if order_by:
            c = l.find_column(order_by)
            if c.is_spatial:
                raise MapsqlError("Cannot order by a geometry column.")
            order = sql.SQL("{} {} NULLS LAST").format(l.column(c.name, "t"), sql.SQL("DESC" if descending else "ASC"))
        return self._feature_query(
            l,
            title=f"{l.qualified}" + (" (filtered)" if filters else ""),
            where=[compile_filters(l, filters, "t")],
            order=order,
            limit=self._limit(limit),
            columns=columns,
            geometry=geometry,
        )

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
        ref = self._reference(lon, lat, reference_layer, reference_filters, use_all_matches)
        metric = l.metric("t")
        refm = l.ref_metric(ref.g4326)
        pre = l.prefilter("t", ref.g4326, d)
        dist = sql.SQL("ROUND(ST_Distance({}, {})::numeric, 1)").format(metric, refm)
        return self._feature_query(
            l,
            title=f"{l.qualified} within {d:g} m of {ref.description}",
            where=[
                pre,
                sql.SQL("ST_DWithin({}, {}, {})").format(metric, refm, sql.Literal(d)),
                compile_filters(l, filters, "t"),
            ],
            order=sql.SQL("ST_Distance({}, {}), 1").format(metric, refm),
            limit=self._limit(limit),
            columns=columns,
            geometry=geometry,
            select_extra=[(dist, "distance_m")],
            extra_meta={"reference": ref.description, "distance_m": d},
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
        ref = self._reference(lon, lat, reference_layer, reference_filters, use_all_matches)
        metric = l.metric("t")
        refm = l.ref_metric(ref.g4326)
        dist_expr = sql.SQL("ST_Distance({}, {})").format(metric, refm)
        where = [compile_filters(l, filters, "t")]
        from_sql = None
        if max_distance_m is not None:
            md = _num("max_distance_m", max_distance_m, 0, MAX_DISTANCE_M)
            where.append(l.prefilter("t", ref.g4326, md))
            where.append(sql.SQL("ST_DWithin({}, {}, {})").format(metric, refm, sql.Literal(md)))
        elif (l.row_estimate or 0) > 50_000 and not l.metric_is_planar and not l.is_geography:
            # big lon/lat table: use the GiST KNN operator to pre-select candidates, then rank exactly
            cand = max(k * 20, 100)
            inner_where = _where([compile_filters(l, filters, "c")])
            from_sql = sql.SQL("(SELECT * FROM {t} AS c{w} ORDER BY {g} <-> {r} LIMIT {n}) AS t").format(
                t=l.table_sql(), w=inner_where, g=l.geom("c"), r=l.ref_native(ref.g4326), n=sql.Literal(cand)
            )
            where = []
        return self._feature_query(
            l,
            title=f"{k} nearest {l.qualified} to {ref.description}",
            where=where,
            order=sql.SQL("{}, 1").format(dist_expr),
            limit=k,
            columns=columns,
            geometry=geometry,
            select_extra=[(sql.SQL("ROUND({}::numeric, 1)").format(dist_expr), "distance_m")],
            from_sql=from_sql,
            count_total=False,
            report_truncation=False,
            extra_meta={"reference": ref.description},
        )

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
            g = sql.SQL(
                "ST_Buffer(({})::geography, {}, 'quad_segs=16')::geometry"
            ).format(self._point(lon, lat), sql.Literal(d))
            q = sql.SQL(
                "SELECT 1 AS n, ROUND((ST_Area({g}::geography) / 1e6)::numeric, 4) AS area_km2, {gj} AS __geom"
            ).format(g=g, gj=geojson_sql(g))
            res = self.db.query(q, max_rows=1)
            fc = self._to_fc(QueryResult(["n_features", "area_km2", "__geom"], [], [(1, *res.rows[0][1:])]))
            return self._finish(fc, f"{d:g} m buffer around point", "auto", {"distance_m": d, "count": 1}, _sql_text(q))
        l = self.catalog.resolve(layer)
        where = _where([compile_filters(l, filters, "t")])
        buf = sql.SQL("ST_Buffer({}, {}, 'quad_segs=16')::geometry").format(
            sql.SQL("({})::geography").format(l.to_4326("t")), sql.Literal(d)
        )
        if dissolve:
            q = sql.SQL(
                "SELECT n, ROUND((ST_Area(u::geography) / 1e6)::numeric, 4) AS area_km2, {gj} AS __geom FROM "
                "(SELECT count(*) AS n, ST_Union(b) AS u FROM (SELECT {buf} AS b FROM {t} AS t{w} LIMIT 20000) s) x"
            ).format(gj=geojson_sql(sql.SQL("u")), buf=buf, t=l.table_sql(), w=where)
            res = self.db.query(q, max_rows=1)
            if not res.rows or res.rows[0][0] == 0:
                raise MapsqlError("No features matched the filters; nothing to buffer.")
            row = res.rows[0]
            fc = self._to_fc(QueryResult(["n_source_features", "area_km2", "__geom"], [], [tuple(row)]))
            return self._finish(
                fc, f"{d:g} m buffer of {l.qualified}", "auto",
                {"layer": l.qualified, "distance_m": d, "dissolved": True, "count": 1}, _sql_text(q),
            )
        lim = min(self._limit(limit), 500)
        cols = [c.name for c in l.attribute_columns[:6]]
        sel = [sql.SQL("{} AS {}").format(l.column(c, "t"), sql.Identifier(c)) for c in cols]
        sel.append(sql.SQL("ROUND((ST_Area({}::geography) / 1e6)::numeric, 4) AS area_km2").format(buf))
        sel.append(sql.SQL("{} AS __geom").format(geojson_sql(buf)))
        q = sql.SQL("SELECT {s} FROM {t} AS t{w} LIMIT {n}").format(
            s=sql.SQL(", ").join(sel), t=l.table_sql(), w=where, n=sql.Literal(lim + 1)
        )
        res = self.db.query(q, max_rows=lim)
        fc = self._to_fc(res)
        return self._finish(
            fc, f"{d:g} m buffers of {l.qualified}", "auto",
            {"layer": l.qualified, "distance_m": d, "dissolved": False, "count": len(fc["features"]), "truncated": res.truncated},
            _sql_text(q),
        )

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
        fn = _REL_FUNC.get(relation)
        if fn is None:
            raise MapsqlError(f"Unknown relation {relation!r}. Use one of: {', '.join(_REL_FUNC)}.")
        operands = sum(x is not None and x != [] for x in (other_layer, bbox, geojson_geometry))
        if operands == 0:
            raise NeedsClarification(
                "Intersect/contain WITH WHAT? Provide other_layer (+other_filters), a bbox "
                "[min_lon,min_lat,max_lon,max_lat], or a GeoJSON geometry. Ask the user if unclear."
            )
        if operands > 1:
            raise NeedsClarification("Give exactly one of other_layer, bbox or geojson_geometry.")
        lg = l.native_geom("t")
        if other_layer:
            o = self.catalog.resolve(other_layer)
            if o.qualified == l.qualified and not other_filters:
                raise NeedsClarification("other_filters are needed when comparing a layer with itself.")
            og = o.geom_as("o", 4326 if l.is_geography else (l.srid or 4326))
            ow = compile_filters(o, other_filters, "o")
            # "disjoint" must hold against ALL matching features, so it is NOT EXISTS (intersects)
            inner = [sql.SQL("{}({}, {})").format(sql.SQL("ST_Intersects" if relation == "disjoint" else fn), lg, og)]
            if ow is not None:
                inner.append(ow)
            pred = sql.SQL("{}EXISTS (SELECT 1 FROM {ot} AS o{w})").format(
                sql.SQL("NOT " if relation == "disjoint" else ""), ot=o.table_sql(), w=_where(inner)
            )
            desc = f"{o.qualified}" + (" (filtered)" if other_filters else "")
        else:
            if bbox is not None:
                if len(bbox) != 4:
                    raise MapsqlError("bbox must be [min_lon, min_lat, max_lon, max_lat].")
                x1, y1, x2, y2 = (_num("bbox", v) for v in bbox)
                if x1 > x2 or y1 > y2:
                    raise MapsqlError("bbox must satisfy min <= max for both axes.")
                ref = sql.SQL("ST_MakeEnvelope({}, {}, {}, {}, 4326)").format(*(sql.Literal(v) for v in (x1, y1, x2, y2)))
                desc = "bbox"
            else:
                import json as _json

                gj = _json.dumps(geojson_geometry)
                if len(gj) > 200_000:
                    raise MapsqlError("geojson_geometry is too large (max 200 KB).")
                ref = sql.SQL("ST_SetSRID(ST_GeomFromGeoJSON({}), 4326)").format(sql.Literal(gj))
                desc = "given geometry"
            pred = sql.SQL("{}({}, {})").format(sql.SQL(fn), lg, l.ref_native(ref))
        return self._feature_query(
            l,
            title=f"{l.qualified} {relation} {desc}",
            where=[pred, compile_filters(l, filters, "t")],
            limit=self._limit(limit),
            columns=columns,
            geometry=geometry,
            extra_meta={"relation": relation, "other": desc},
        )

    def bbox_of(self, layer: str, filters: list[Filter] | None = None) -> dict:
        l = self.catalog.resolve(layer)
        w = _where([compile_filters(l, filters, "t")])
        q = sql.SQL(
            "SELECT n, ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
            "(SELECT count(*) AS n, ST_Extent({g}) AS e FROM {t} AS t{w}) s"
        ).format(g=l.to_4326("t"), t=l.table_sql(), w=w)
        row = self.db.query(q, max_rows=1).rows[0]
        n, box = row[0], row[1]
        if not n or box is None:
            return {"layer": l.qualified, "count": n or 0, "bbox": None, "note": "No features with geometry matched."}
        coords = row[1:5]
        minx, miny, maxx, maxy = (round(float(v), 6) for v in coords)
        poly = {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }
        fc = {"type": "FeatureCollection", "features": [make_feature({"layer": l.qualified, "features": n}, poly)]}
        rid = self.store.put(fc, f"bbox of {l.qualified}")
        return {
            "layer": l.qualified,
            "count": n,
            "bbox": [minx, miny, maxx, maxy],
            "crs": "EPSG:4326",
            "center": [round((minx + maxx) / 2, 6), round((miny + maxy) / 2, 6)],
            "result_id": rid,
        }

    # -------------------------------------------------------------- analysis

    def aggregate(
        self,
        layer: str,
        metrics: list["Metric"] | None = None,
        group_by: list[str] | None = None,
        filters: list[Filter] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = None,
    ) -> dict:
        l = self.catalog.resolve(layer)
        metrics = metrics or [Metric(fn="count")]
        sel: list[sql.Composable] = []
        names: list[str] = []
        group_cols: list[sql.Composable] = []
        for g in group_by or []:
            c = l.find_column(g)
            if c.is_spatial:
                raise MapsqlError("Cannot group by a geometry column.")
            ident = l.column(c.name, "t")
            sel.append(sql.SQL("{} AS {}").format(ident, sql.Identifier(c.name)))
            names.append(c.name)
            group_cols.append(ident)
        for m in metrics:
            expr, alias = m.compile(l)
            if alias in names:
                alias = f"{alias}_{len(names)}"
            sel.append(sql.SQL("{} AS {}").format(expr, sql.Identifier(alias)))
            names.append(alias)
        order = None
        if order_by:
            if order_by not in names:
                raise MapsqlError(f"order_by must be one of the output columns: {names}")
            order = sql.SQL(" ORDER BY {} {} NULLS LAST").format(
                sql.Identifier(order_by), sql.SQL("DESC" if descending else "ASC")
            )
        elif group_cols:
            order = sql.SQL(" ORDER BY {} {} NULLS LAST").format(
                sql.Identifier(names[len(group_cols)]), sql.SQL("DESC" if descending else "ASC")
            )
        lim = self._limit(limit)
        q = sql.SQL("SELECT {sel} FROM {t} AS t{w}{gb}{ob} LIMIT {n}").format(
            sel=sql.SQL(", ").join(sel),
            t=l.table_sql(),
            w=_where([compile_filters(l, filters, "t")]),
            gb=(sql.SQL(" GROUP BY ") + sql.SQL(", ").join(sql.Literal(i + 1) for i in range(len(group_cols))))
            if group_cols else sql.SQL(""),
            ob=order or sql.SQL(""),
            n=sql.Literal(lim + 1),
        )
        res = self.db.query(q, max_rows=lim)
        rows = [{k: json_safe(v) for k, v in zip(res.columns, r)} for r in res.rows]
        out = {
            "layer": l.qualified,
            "columns": res.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": res.truncated,
            "sql": _sql_text(q),
        }
        return out

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
        b = self.catalog.resolve(right_layer)
        if relation == "dwithin":
            if distance_m is None:
                raise NeedsClarification("relation 'dwithin' needs distance_m (metres). Ask the user for the distance.")
            d = _num("distance_m", distance_m, 0, MAX_DISTANCE_M)
        elif relation not in ("intersects", "within", "contains"):
            raise MapsqlError("relation must be one of: intersects, within, contains, dwithin.")
        lim = self._limit(limit)
        a_in_b = a.geom_as("t", 4326 if b.is_geography else (b.srid or 4326))
        bg = b.native_geom("r")
        if relation == "dwithin":
            if b.metric_is_planar:
                pred = sql.SQL("ST_DWithin({}, {}, {})").format(a_in_b, bg, sql.Literal(d))
            else:
                pred = sql.SQL("ST_DWithin(({})::geography, ({})::geography, {})").format(
                    a.to_4326("t"), b.to_4326("r"), sql.Literal(d)
                )
        else:
            fn = {"intersects": "ST_Intersects", "within": "ST_Within", "contains": "ST_Contains"}[relation]
            pred = sql.SQL("{}({}, {})").format(sql.SQL(fn), a_in_b, bg)
        rw = _where([pred, compile_filters(b, right_filters, "r")])
        lw = [compile_filters(a, left_filters, "t")]

        if mode == "attach":
            rcols = self._columns(b, right_columns)[:8]
            lcols = [c.name for c in a.attribute_columns[:8]]
            sel = [sql.SQL("{} AS {}").format(a.column(c, "t"), sql.Identifier(c)) for c in lcols]
            for c in rcols:
                sel.append(sql.SQL("{} AS {}").format(b.column(c, "r"), sql.Identifier(f"right_{c}")))
            sel.append(sql.SQL("{} AS __geom").format(geojson_sql(a.to_4326("t"))))
            q = sql.SQL("SELECT {s} FROM {ta} AS t JOIN {tb} AS r ON {pred}{rf}{w} LIMIT {n}").format(
                s=sql.SQL(", ").join(sel), ta=a.table_sql(), tb=b.table_sql(), pred=pred,
                rf=(sql.SQL(" AND ") + compile_filters(b, right_filters, "r")) if right_filters else sql.SQL(""),
                w=_where(lw), n=sql.Literal(lim + 1),
            )
            res = self.db.query(q, max_rows=lim)
            fc = self._to_fc(res)
            return self._finish(
                fc, f"{a.qualified} joined with {b.qualified}", geometry,
                {"left_layer": a.qualified, "right_layer": b.qualified, "mode": mode, "count": len(fc["features"]),
                 "truncated": res.truncated, "limit": lim},
                _sql_text(q),
            )

        agg_sel: list[sql.Composable] = [sql.SQL("count(*) AS right_count")]
        if mode in ("sum", "avg"):
            if not right_column:
                raise NeedsClarification(f"mode={mode!r} needs right_column (a numeric column of {b.qualified}).")
            rc = b.find_column(right_column)
            if not rc.is_numeric:
                raise MapsqlError(f"right_column {rc.name!r} is not numeric ({rc.type}).")
            fn = "sum" if mode == "sum" else "avg"
            agg_sel.append(sql.SQL("{}({})::float8 AS {}").format(sql.SQL(fn), b.column(rc.name, "r"), sql.Identifier(f"right_{fn}_{rc.name}")))
        lcols = [c.name for c in a.attribute_columns[:10]]
        sel = [sql.SQL("{} AS {}").format(a.column(c, "t"), sql.Identifier(c)) for c in lcols]
        sel.append(sql.SQL("s.right_count"))
        extra_names = []
        if mode in ("sum", "avg"):
            nm = f"right_{'sum' if mode == 'sum' else 'avg'}_{b.find_column(right_column).name}"  # type: ignore[arg-type]
            sel.append(sql.SQL("s.{}").format(sql.Identifier(nm)))
            extra_names.append(nm)
        sel.append(sql.SQL("{} AS __geom").format(geojson_sql(a.to_4326("t"))))
        order_expr = sql.SQL("s.right_count") if mode == "count" else sql.SQL("s.{}").format(sql.Identifier(extra_names[0]))
        wh = lw + ([sql.SQL("s.right_count >= {}").format(sql.Literal(int(min_count)))] if min_count else [])
        q = sql.SQL(
            "SELECT {s} FROM {ta} AS t CROSS JOIN LATERAL (SELECT {agg} FROM {tb} AS r{rw}) s{w} ORDER BY {o} {dir} NULLS LAST LIMIT {n}"
        ).format(
            s=sql.SQL(", ").join(sel), ta=a.table_sql(), agg=sql.SQL(", ").join(agg_sel), tb=b.table_sql(), rw=rw,
            w=_where(wh), o=order_expr, dir=sql.SQL("DESC" if order_desc else "ASC"), n=sql.Literal(lim + 1),
        )
        res = self.db.query(q, max_rows=lim, timeout_ms=max(self.settings.statement_timeout_ms, 30000))
        fc = self._to_fc(res)
        return self._finish(
            fc, f"{a.qualified} x {b.qualified} ({mode})", geometry,
            {"left_layer": a.qualified, "right_layer": b.qualified, "relation": relation, "mode": mode,
             "count": len(fc["features"]), "truncated": res.truncated, "limit": lim},
            _sql_text(q),
        )

    # ---------------------------------------------------------------- raw SQL

    def run_sql(self, query: str, limit: int | None = None, geometry: GeometryMode = "auto") -> dict:
        """Guarded, read-only SQL. Geometry columns are returned as GeoJSON (EPSG:4326)."""
        validate_sql(query, self.settings, known_relations=self._known_for_guard())
        lim = self._limit(limit)
        q = query.strip().rstrip(";").strip()
        names, oids = self.db.describe(q)
        geoms = self.db.geometry_oids()
        geom_idx = [i for i, o in enumerate(oids) if o in geoms]
        if geom_idx and len(set(names)) != len(names):
            raise MapsqlError("Result has duplicate column names; alias them (AS ...) so geometries can be converted.")
        if geom_idx:
            geog = self.db.geography_oid()
            sel = []
            for i, n in enumerate(names):
                ident = sql.Identifier("q", n)
                if i in geom_idx:
                    g = sql.SQL("{}::geometry").format(ident) if oids[i] == geog else ident
                    g4326 = sql.SQL("(CASE WHEN ST_SRID({g}) IN (0, 4326) THEN {g} ELSE ST_Transform({g}, 4326) END)").format(g=g)
                    sel.append(sql.SQL("{} AS {}").format(geojson_sql(g4326), sql.Identifier(n)))
                else:
                    sel.append(sql.SQL("{} AS {}").format(ident, sql.Identifier(n)))
            wrapped = sql.SQL("SELECT {s} FROM ({u}) AS q LIMIT {n}").format(
                s=sql.SQL(", ").join(sel), u=sql.SQL(q), n=sql.Literal(lim + 1)  # validated single SELECT
            )
        else:
            wrapped = sql.SQL("SELECT * FROM ({u}) AS q LIMIT {n}").format(u=sql.SQL(q), n=sql.Literal(lim + 1))
        res = self.db.query(wrapped, max_rows=lim)
        out: dict[str, Any] = {"columns": names, "row_count": len(res.rows), "truncated": res.truncated, "limit": lim}
        if not geom_idx:
            out["rows"] = [{k: json_safe(v) for k, v in zip(names, r)} for r in res.rows]
            if res.truncated:
                out["note"] = "Truncated at the row limit; refine the query or raise 'limit'."
            return out
        gi = geom_idx[0]
        gname = names[gi]
        feats = []
        for r in res.rows:
            props = {k: v for i, (k, v) in enumerate(zip(names, r)) if i not in geom_idx}
            feats.append(make_feature(props, r[gi]))
        fc = {"type": "FeatureCollection", "features": feats}
        meta = {"columns": [n for i, n in enumerate(names) if i not in geom_idx], "geometry_column": gname,
                "row_count": len(feats), "truncated": res.truncated, "limit": lim}
        if len(geom_idx) > 1:
            meta["note"] = f"Multiple geometry columns; only {gname!r} is returned as GeoJSON."
        return self._finish(fc, "SQL result", geometry, meta, None)

    def execute_sql(self, statement: str) -> dict:
        """Write-mode SQL (only registered when --allow-write is set)."""
        if not self.settings.allow_write:
            raise MapsqlError("Write mode is disabled. Start the server with --allow-write to enable execute_sql.")
        validate_sql(statement, self.settings, allow_write=True, known_relations=self._known_for_guard())
        res = self.db.execute_write(statement.strip().rstrip(";"))
        self.catalog.invalidate()
        out: dict[str, Any] = {"rowcount": res.rowcount, "committed": True}
        if res.columns:
            out["columns"] = res.columns
            out["rows"] = [{k: json_safe(v) for k, v in zip(res.columns, r)} for r in res.rows]
        return out

    def _known_for_guard(self) -> dict[str, set[str]] | None:
        if not self.settings.restricted:
            return None
        return self.catalog.known_relations()


def _short_type(t: str) -> str:
    return (
        t.replace("character varying", "varchar").replace("double precision", "float8")
        .replace("timestamp without time zone", "timestamp").replace("timestamp with time zone", "timestamptz")
    )


# ---------------------------------------------------------------------------
# aggregate metric spec (a pydantic model so MCP exposes a clean JSON schema)
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402


class Metric(BaseModel):
    """One output measure of ``aggregate``."""

    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max", "area_km2", "length_km"] = Field(
        description="count rows | count_distinct/sum/avg/min/max of `column` | area_km2 (sum of polygon areas, "
        "geodesic) | length_km (sum of line lengths, geodesic)."
    )
    column: str | None = Field(default=None, description="Column for count_distinct/sum/avg/min/max.")
    alias: str | None = Field(default=None, description="Output column name (optional).")

    def compile(self, layer: Layer) -> tuple[sql.Composable, str]:
        fn = self.fn
        if fn == "count":
            return sql.SQL("count(*)"), self.alias or "count"
        if fn in ("area_km2", "length_km"):
            m = layer.metric("t")
            if fn == "area_km2":
                if "POLYGON" not in layer.geom_type and layer.geom_type not in ("GEOMETRY", "GEOGRAPHY", "GEOMETRYCOLLECTION"):
                    raise MapsqlError(f"area_km2 needs a polygon layer; {layer.qualified} is {layer.geom_type}.")
                return sql.SQL("ROUND((SUM(ST_Area({}))/1e6)::numeric, 3)").format(m), self.alias or "area_km2"
            if "LINE" not in layer.geom_type and layer.geom_type not in ("GEOMETRY", "GEOGRAPHY", "GEOMETRYCOLLECTION"):
                raise MapsqlError(f"length_km needs a line layer; {layer.qualified} is {layer.geom_type}.")
            return sql.SQL("ROUND((SUM(ST_Length({}))/1e3)::numeric, 3)").format(m), self.alias or "length_km"
        if not self.column:
            raise NeedsClarification(f"Metric {fn!r} needs a `column`.")
        c = layer.find_column(self.column)
        if c.is_spatial:
            raise MapsqlError("Metrics on geometry columns: use area_km2 / length_km.")
        ident = layer.column(c.name, "t")
        if fn == "count_distinct":
            return sql.SQL("count(DISTINCT {})").format(ident), self.alias or f"distinct_{c.name}"
        if fn in ("sum", "avg") and not c.is_numeric:
            raise MapsqlError(f"{fn}() needs a numeric column; {c.name!r} is {c.type}.")
        fname = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}[fn]
        expr = sql.SQL("{}({})").format(sql.SQL(fname), ident)
        if fn in ("sum", "avg"):
            expr = sql.SQL("{}::float8").format(expr)
        return expr, self.alias or f"{fn}_{c.name}"
