"""Layer catalog: discover PostGIS geometry/geography columns and build safe SQL fragments."""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg import sql
from pydantic import BaseModel, Field

from .db import Database
from .errors import LayerNotFound, MapsqlError, NeedsClarification

# types we never return as ordinary attribute columns
_NON_ATTRIBUTE_TYPES = ("geometry", "geography", "bytea", "raster", "tsvector", "box2d", "box3d", "tsquery")


@dataclass(frozen=True)
class Crs:
    srid: int
    kind: str  # "geographic" | "projected" | "unknown"
    metre: bool = False
    name: str | None = None

    @property
    def label(self) -> str:
        return f"EPSG:{self.srid}" if self.srid else "unknown"


@dataclass
class Column:
    name: str
    type: str

    @property
    def is_spatial(self) -> bool:
        return self.type.split("(")[0] in ("geometry", "geography", "raster", "box2d", "box3d")

    @property
    def is_attribute(self) -> bool:
        return not any(self.type.startswith(t) for t in _NON_ATTRIBUTE_TYPES)

    @property
    def is_numeric(self) -> bool:
        t = self.type.split("(")[0]
        return t in ("integer", "bigint", "smallint", "numeric", "double precision", "real", "decimal")


@dataclass
class Layer:
    schema: str
    table: str
    geom_column: str
    geom_type: str
    srid: int
    kind: str  # table | view | materialized view | foreign table | partitioned table
    is_geography: bool
    crs: Crs
    columns: list[Column] = field(default_factory=list)
    other_geom_columns: list[str] = field(default_factory=list)
    row_estimate: int | None = None
    oid: int = 0

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def attribute_columns(self) -> list[Column]:
        return [c for c in self.columns if c.is_attribute]

    def table_sql(self, alias: str | None = None) -> sql.Composable:
        t = sql.Identifier(self.schema, self.table)
        return t + sql.SQL(" AS ") + sql.Identifier(alias) if alias else t

    def geom(self, alias: str | None = None) -> sql.Composable:
        return sql.Identifier(alias, self.geom_column) if alias else sql.Identifier(self.geom_column)

    def column(self, name: str, alias: str | None = None) -> sql.Composable:
        col = self.find_column(name)
        return sql.Identifier(alias, col.name) if alias else sql.Identifier(col.name)

    def find_column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        low = name.lower()
        for c in self.columns:
            if c.name.lower() == low:
                return c
        names = [c.name for c in self.columns]
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else f" Available columns: {', '.join(names[:30])}"
        raise MapsqlError(f"Layer {self.qualified} has no column {name!r}.{hint}")

    # ---- SQL fragments -------------------------------------------------------

    def to_4326(self, alias: str | None = None) -> sql.Composable:
        """The geometry as a geometry in EPSG:4326."""
        g = self.geom(alias)
        if self.is_geography:
            return sql.SQL("{}::geometry").format(g)
        if self.srid in (4326, 0):
            return g
        return sql.SQL("ST_Transform({}, 4326)").format(g)

    def metric(self, alias: str | None = None) -> sql.Composable:
        """The geometry in a space where distances/areas are in metres."""
        g = self.geom(alias)
        if self.is_geography:
            return g
        c = self.crs
        if c.kind == "geographic":
            return sql.SQL("{}::geography").format(g) if self.srid == 4326 else sql.SQL(
                "ST_Transform({}, 4326)::geography"
            ).format(g)
        if c.kind == "projected" and c.metre:
            return g
        if c.kind == "unknown":
            raise MapsqlError(
                f"Layer {self.qualified} has an unknown coordinate system (SRID {self.srid}); "
                "metric distances/areas are not available."
            )
        return sql.SQL("ST_Transform({}, 4326)::geography").format(g)

    def ref_metric(self, ref4326: sql.Composable) -> sql.Composable:
        """A EPSG:4326 geometry expression expressed in this layer's metric space."""
        if self.is_geography:
            return sql.SQL("({})::geography").format(ref4326)
        c = self.crs
        if c.kind == "projected" and c.metre:
            return sql.SQL("ST_Transform({}, {})").format(ref4326, sql.Literal(self.srid))
        return sql.SQL("({})::geography").format(ref4326)

    def native_geom(self, alias: str | None = None) -> sql.Composable:
        """The geometry column as a *geometry* (geography columns are cast)."""
        g = self.geom(alias)
        return sql.SQL("{}::geometry").format(g) if self.is_geography else g

    def geom_as(self, alias: str | None, target_srid: int) -> sql.Composable:
        """The geometry expressed in ``target_srid`` (ST_Transform only if needed)."""
        g = self.native_geom(alias)
        src = 4326 if self.is_geography else self.srid
        if src in (0, target_srid):
            return g
        return sql.SQL("ST_Transform({}, {})").format(g, sql.Literal(target_srid))

    def ref_native(self, ref4326: sql.Composable) -> sql.Composable:
        """A EPSG:4326 geometry expression expressed in the layer's native SRID (for index-friendly predicates)."""
        if self.is_geography:
            return sql.SQL("({})").format(ref4326)
        if self.srid in (4326, 0):
            return sql.SQL("({})").format(ref4326)
        return sql.SQL("ST_Transform({}, {})").format(ref4326, sql.Literal(self.srid))

    @property
    def metric_is_planar(self) -> bool:
        return (not self.is_geography) and self.crs.kind == "projected" and self.crs.metre

    def prefilter(self, alias: str | None, ref4326: sql.Composable, dist_m: Any) -> sql.Composable | None:
        """Bounding-box prefilter so GiST indexes are used for geographic (lon/lat) layers."""
        if self.is_geography or self.metric_is_planar:
            return None  # geography has its own index; planar distance predicates are index-aware
        if (self.row_estimate or 0) < 20000:
            return None
        env = sql.SQL("ST_Envelope(ST_Buffer(({})::geography, {})::geometry)").format(ref4326, sql.Literal(dist_m))
        if self.srid in (4326, 0):
            return sql.SQL("{} && {}").format(self.geom(alias), env)
        return sql.SQL("{} && ST_Transform({}, {})").format(self.geom(alias), env, sql.Literal(self.srid))

    def summary(self) -> dict[str, Any]:
        return {
            "layer": self.qualified,
            "geometry_column": self.geom_column,
            "geometry_type": self.geom_type,
            "srid": self.srid,
            "crs": self.crs.label,
            "kind": self.kind,
            "row_estimate": self.row_estimate,
        }


# --------------------------------------------------------------------------
# attribute filters
# --------------------------------------------------------------------------

FilterOp = Literal[
    "=", "!=", "<", "<=", ">", ">=", "like", "ilike", "contains", "in", "not_in", "is_null", "not_null", "between"
]


class Filter(BaseModel):
    """One attribute condition. A list of filters is combined with AND."""

    column: str = Field(description="Column name of the layer.")
    op: FilterOp = Field(
        default="=",
        description="Comparison: = != < <= > >= | like ilike (use % wildcards) | contains (case-insensitive substring) "
        "| in not_in (value is a list) | between (value is [low, high]) | is_null not_null (no value).",
    )
    value: Any = Field(default=None, description="Value to compare with (list for in/not_in/between).")


def compile_filters(layer: Layer, filters: list[Filter] | None, alias: str | None = None) -> sql.Composable | None:
    """Compile filters to a WHERE fragment. Values are inlined as properly escaped SQL literals
    (psycopg ``Literal``), identifiers are validated against the layer's real column list."""
    if not filters:
        return None
    parts: list[sql.Composable] = []
    for f in filters:
        col = layer.column(f.column, alias)
        c = layer.find_column(f.column)
        if c.is_spatial:
            raise MapsqlError(f"Column {c.name!r} is a geometry column; use the spatial tools to filter on geometry.")
        op = f.op
        if op in ("is_null", "not_null"):
            parts.append(sql.SQL("{} IS {}NULL").format(col, sql.SQL("NOT ") if op == "not_null" else sql.SQL("")))
            continue
        if f.value is None:
            raise MapsqlError(f"Filter on {c.name!r} with op {op!r} needs a value.")
        if op in ("like", "ilike", "contains"):
            val = str(f.value)
            if op == "contains":
                val = "%" + val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            parts.append(sql.SQL("{}::text {} {}").format(col, sql.SQL("LIKE" if op == "like" else "ILIKE"), sql.Literal(val)))
        elif op in ("in", "not_in"):
            if not isinstance(f.value, (list, tuple)) or not f.value:
                raise MapsqlError(f"Filter op {op!r} on {c.name!r} needs a non-empty list value.")
            if len(f.value) > 500:
                raise MapsqlError("Too many values in an 'in' filter (max 500).")
            lits = sql.SQL(", ").join(_lit(c, v) for v in f.value)
            parts.append(sql.SQL("{} {} ({})").format(col, sql.SQL("IN" if op == "in" else "NOT IN"), lits))
        elif op == "between":
            if not isinstance(f.value, (list, tuple)) or len(f.value) != 2:
                raise MapsqlError(f"Filter op 'between' on {c.name!r} needs [low, high].")
            parts.append(sql.SQL("{} BETWEEN {} AND {}").format(col, _lit(c, f.value[0]), _lit(c, f.value[1])))
        elif op in ("=", "!=", "<", "<=", ">", ">="):
            sql_op = "<>" if op == "!=" else op
            if isinstance(f.value, (list, dict)):
                raise MapsqlError(f"Filter op {op!r} on {c.name!r} needs a scalar value.")
            parts.append(sql.SQL("{} {} {}").format(col, sql.SQL(sql_op), _lit(c, f.value)))
        else:  # pragma: no cover
            raise MapsqlError(f"Unknown filter op {op!r}")
    return sql.SQL(" AND ").join(parts)


def _lit(c: Column, value: Any) -> sql.Composable:
    """Literal for a comparison value, validated against the column type where cheap to do so."""
    if isinstance(value, (list, dict)):
        raise MapsqlError(f"Unsupported value {value!r} for column {c.name!r}.")
    if c.is_numeric and isinstance(value, str):
        try:
            return sql.Literal(float(value) if any(ch in value for ch in ".eE") else int(value))
        except ValueError:
            raise MapsqlError(f"Column {c.name!r} is numeric ({c.type}) but the filter value is {value!r}.") from None
    if c.is_numeric and isinstance(value, bool):
        raise MapsqlError(f"Column {c.name!r} is numeric but the filter value is boolean.")
    if isinstance(value, float) and value != value:
        raise MapsqlError("NaN is not a valid filter value.")
    return sql.Literal(value)


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------

_KIND = {"r": "table", "v": "view", "m": "materialized view", "f": "foreign table", "p": "partitioned table"}


class Catalog:
    TTL = 60.0

    def __init__(self, db: Database):
        self.db = db
        self.settings = db.settings
        self._layers: list[Layer] | None = None
        self._relations: dict[str, set[str]] | None = None
        self._ts = 0.0

    def invalidate(self) -> None:
        self._layers = None
        self._relations = None

    def _fresh(self) -> bool:
        return self._layers is not None and (time.monotonic() - self._ts) < self.TTL

    def layers(self, refresh: bool = False) -> list[Layer]:
        if refresh or not self._fresh():
            self._load()
        assert self._layers is not None
        return self._layers

    def known_relations(self) -> dict[str, set[str]]:
        if self._relations is None or not self._fresh():
            self._load()
        assert self._relations is not None
        return self._relations

    def _load(self) -> None:
        db = self.db
        db.geometry_oids()  # also discovers the PostGIS schema for the restricted search_path
        try:
            geo = db.query(
                """
                SELECT n.nspname, c.relname, c.relkind::text, c.oid::bigint, c.reltuples::bigint,
                       g.col, g.srid, g.type, g.is_geog
                FROM (
                    SELECT f_table_schema AS s, f_table_name AS t, f_geometry_column AS col, srid, type, false AS is_geog
                    FROM geometry_columns
                    UNION ALL
                    SELECT f_table_schema, f_table_name, f_geography_column, srid, type, true
                    FROM geography_columns
                ) g
                JOIN pg_namespace n ON n.nspname = g.s
                JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = g.t
                ORDER BY n.nspname, c.relname, g.col
                """,
                max_rows=5000,
            )
        except MapsqlError as e:
            raise MapsqlError(
                "Could not read PostGIS metadata (geometry_columns). Is the PostGIS extension installed "
                f"in this database and visible to the role? ({e})"
            ) from None

        rels = db.query(
            """
            SELECT n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','v','m','f','p')
              AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%%' AND n.nspname NOT LIKE 'pg_toast_temp%%'
            """,
            max_rows=100000,
        )
        relations: dict[str, set[str]] = {}
        for sch, name in rels.rows:
            relations.setdefault(sch.lower(), set()).add(name.lower())

        visible = [r for r in geo.rows if self.settings.relation_allowed(r[0], r[1])]
        oids = sorted({r[3] for r in visible})
        cols_by_oid: dict[int, list[Column]] = {}
        if oids:
            cres = db.query(
                """
                SELECT a.attrelid::bigint, a.attname, format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attrelid, a.attnum
                """,
                [oids],
                max_rows=100000,
            )
            for oid, name, typ in cres.rows:
                cols_by_oid.setdefault(oid, []).append(Column(name, typ))

        srids = sorted({r[6] for r in visible if r[6]})
        crs_map: dict[int, Crs] = {}
        if srids:
            sres = db.query(
                "SELECT srid, auth_name, auth_srid, srtext, proj4text FROM spatial_ref_sys WHERE srid = ANY(%s)",
                [srids],
                max_rows=5000,
            )
            for srid, auth, auth_srid, srtext, proj4 in sres.rows:
                crs_map[srid] = _classify_crs(srid, srtext or "", proj4 or "")

        layers: list[Layer] = []
        group: dict[tuple[str, str], list[str]] = {}
        for sch, rel, relkind, oid, tuples, col, srid, gtype, is_geog in visible:
            group.setdefault((sch, rel), []).append(col)
        for sch, rel, relkind, oid, tuples, col, srid, gtype, is_geog in visible:
            srid = srid or 0
            crs = crs_map.get(srid, Crs(srid, "unknown"))
            layer = Layer(
                schema=sch, table=rel, geom_column=col, geom_type=(gtype or "GEOMETRY").upper(), srid=srid,
                kind=_KIND.get(relkind, relkind), is_geography=bool(is_geog), crs=crs,
                columns=cols_by_oid.get(oid, []),
                other_geom_columns=[c for c in group[(sch, rel)] if c != col],
                row_estimate=int(tuples) if tuples is not None and tuples >= 0 else None, oid=oid,
            )
            layers.append(layer)

        # views/typmod-less columns report SRID 0: sample one row to learn the real SRID
        for layer in layers:
            if layer.srid == 0 and not layer.is_geography:
                try:
                    r = db.query(
                        sql.SQL("SELECT ST_SRID({g}) FROM {t} WHERE {g} IS NOT NULL LIMIT 1").format(
                            g=sql.Identifier(layer.geom_column), t=sql.Identifier(layer.schema, layer.table)
                        ),
                        max_rows=1,
                        timeout_ms=3000,
                    )
                    if r.rows and r.rows[0][0]:
                        layer.srid = int(r.rows[0][0])
                        layer.crs = crs_map.get(layer.srid) or _crs_lookup(db, layer.srid)
                except MapsqlError:
                    pass

        self._layers = layers
        self._relations = relations
        self._ts = time.monotonic()

    # ---- lookup --------------------------------------------------------------

    def resolve(self, name: str, geometry_column: str | None = None) -> Layer:
        if not isinstance(name, str) or not name.strip():
            raise NeedsClarification("A layer name is required. Call list_layers to see the available layers.")
        raw = name.strip().strip('"')
        low = raw.lower().replace('"', "")
        layers = self.layers()
        if "." in low:
            sch, tbl = low.split(".", 1)
            cand = [l for l in layers if l.schema.lower() == sch and l.table.lower() == tbl]
        else:
            cand = [l for l in layers if l.table.lower() == low]
        if geometry_column:
            cand = [l for l in cand if l.geom_column.lower() == geometry_column.lower()]
        if not cand:
            names = [l.qualified for l in layers]
            close = difflib.get_close_matches(raw, names + [l.table for l in layers], n=4, cutoff=0.4)
            hint = f" Did you mean: {', '.join(dict.fromkeys(close))}?" if close else ""
            raise LayerNotFound(
                f"Layer {name!r} not found (or not visible).{hint} Call list_layers to see available layers."
            )
        # prefer a single (schema, table); a table with several geometry columns -> first unless specified
        distinct = {(l.schema, l.table) for l in cand}
        if len(distinct) > 1:
            raise NeedsClarification(
                f"Layer name {name!r} is ambiguous: {', '.join(sorted(f'{s}.{t}' for s, t in distinct))}. "
                "Use the schema-qualified name."
            )
        return cand[0]


def _classify_crs(srid: int, srtext: str, proj4: str) -> Crs:
    up = srtext.upper()
    if up.startswith(("GEOGCS", "GEOGCRS", "GEODCRS", "GEODETICCRS")) or "+proj=longlat" in proj4:
        kind = "geographic"
    elif up.startswith(("PROJCS", "PROJCRS")) or "+proj=" in proj4:
        kind = "projected"
    else:
        kind = "unknown"
    metre = False
    if kind == "projected":
        metre = ('UNIT["METRE"' in up) or ('UNIT["METER"' in up) or ('LENGTHUNIT["METRE"' in up) or "+units=m" in proj4
    return Crs(srid=srid, kind=kind, metre=metre)


def _crs_lookup(db: Database, srid: int) -> Crs:
    r = db.query(
        "SELECT srtext, proj4text FROM spatial_ref_sys WHERE srid = %s", [srid], max_rows=1
    )
    if not r.rows:
        return Crs(srid, "unknown")
    return _classify_crs(srid, r.rows[0][0] or "", r.rows[0][1] or "")
