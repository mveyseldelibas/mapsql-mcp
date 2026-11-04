"""Oracle layer catalog: SDO_GEOMETRY columns, SRIDs, spatial indexes, and safe SQL fragments (bind variables)."""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..errors import LayerNotFound, MapsqlError, NeedsClarification
from ..layers import Filter
from .oracle_db import OracleDatabase, display_name, quote_ident, used_binds  # noqa: F401
from .oracle_guard import OracleKnown

#: SRIDs that mean "WGS84 longitude/latitude" (4326 = EPSG definition, 8307 = Oracle's legacy definition)
WGS84_SRIDS = (4326, 8307)

_GTYPE_NAMES = {
    1: "POINT", 2: "LINESTRING", 3: "POLYGON", 4: "GEOMETRYCOLLECTION", 5: "MULTIPOINT", 6: "MULTILINESTRING",
    7: "MULTIPOLYGON", 8: "SOLID", 9: "MULTISOLID",
}

_NUMERIC = {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "INTEGER", "INT", "SMALLINT", "DECIMAL", "NUMERIC", "REAL",
            "DOUBLE PRECISION"}
_TEXT = {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR"}
_LOB_TEXT = {"CLOB", "NCLOB"}
_SCALARS = _NUMERIC | _TEXT | _LOB_TEXT | {"DATE", "ROWID", "UROWID", "BOOLEAN"}


@dataclass(frozen=True)
class OraCrs:
    srid: int
    kind: str  # "geographic" | "projected" | "unknown"
    metre: bool = False
    name: str | None = None

    @property
    def label(self) -> str:
        if not self.srid:
            return "unknown"
        base = f"EPSG:{self.srid}" if self.srid < 8000 else f"Oracle SRID {self.srid}"
        return f"{base} ({self.name})" if self.name else base


@dataclass
class OraColumn:
    name: str  # real Oracle name (usually upper case)
    type: str  # e.g. VARCHAR2(100), NUMBER(10,2), SDO_GEOMETRY

    @property
    def display(self) -> str:
        return display_name(self.name)

    @property
    def base(self) -> str:
        return re.split(r"[( ]", self.type.upper(), maxsplit=1)[0] if not self.type.upper().startswith("DOUBLE") else "DOUBLE PRECISION"

    @property
    def is_spatial(self) -> bool:
        return self.base.startswith("SDO_")

    @property
    def is_numeric(self) -> bool:
        return self.base in _NUMERIC

    @property
    def is_text(self) -> bool:
        return self.base in _TEXT

    @property
    def is_date(self) -> bool:
        return self.base == "DATE" or self.base.startswith("TIMESTAMP")

    @property
    def is_attribute(self) -> bool:
        b = self.base
        return b in _SCALARS or b.startswith("TIMESTAMP") or b.startswith("INTERVAL")


class Binds:
    """Collects bind-variable values: ``b.add(v)`` returns the placeholder (``:b1``) to splice into the SQL."""

    def __init__(self, prefix: str = "b") -> None:
        self.prefix = prefix
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> str:
        name = f"{self.prefix}{len(self.values) + 1}"
        self.values[name] = value
        return ":" + name


def num_lit(v: float) -> str:
    """A finite float as an SQL numeric literal."""
    return repr(float(v))


@dataclass
class OraLayer:
    owner: str
    table: str
    geom_column: str
    geom_type: str
    srid: int
    kind: str  # table | view | materialized view
    crs: OraCrs
    tolerance: float
    has_index: bool
    columns: list[OraColumn] = field(default_factory=list)
    other_geom_columns: list[str] = field(default_factory=list)
    row_estimate: int | None = None
    registered: bool = True  # present in ALL_SDO_GEOM_METADATA
    extent: list[float] | None = None
    extent_done: bool = False

    # ---- naming ---------------------------------------------------------------------

    @property
    def qualified(self) -> str:
        return f"{display_name(self.owner)}.{display_name(self.table)}"

    @property
    def attribute_columns(self) -> list[OraColumn]:
        return [c for c in self.columns if c.is_attribute]

    def table_sql(self, alias: str | None = None) -> str:
        t = f"{quote_ident(self.owner)}.{quote_ident(self.table)}"
        return f"{t} {alias}" if alias else t

    def geom(self, alias: str) -> str:
        return f"{alias}.{quote_ident(self.geom_column)}"

    def col(self, name: str, alias: str) -> str:
        return f"{alias}.{quote_ident(self.find_column(name).name)}"

    def find_column(self, name: str) -> OraColumn:
        for c in self.columns:
            if c.name == name:
                return c
        low = str(name).lower()
        for c in self.columns:
            if c.name.lower() == low or c.display.lower() == low:
                return c
        names = [c.display for c in self.columns]
        close = difflib.get_close_matches(str(name), names, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else f" Available columns: {', '.join(names[:30])}"
        raise MapsqlError(f"Layer {self.qualified} has no column {name!r}.{hint}")

    # ---- spatial helpers ------------------------------------------------------------

    @property
    def geodetic(self) -> bool:
        return self.crs.kind == "geographic"

    def require_metric(self) -> None:
        if self.crs.kind == "unknown":
            raise MapsqlError(
                f"Layer {self.qualified} has an unknown coordinate system (SRID {self.srid or 'NULL'}); metric "
                "distances/areas are not available."
            )

    def to_4326(self, alias: str) -> str:
        """The geometry as SDO_GEOMETRY in EPSG:4326 (lon/lat)."""
        g = self.geom(alias)
        if self.srid in WGS84_SRIDS or not self.srid:
            return g
        return f"SDO_CS.TRANSFORM({g}, 4326)"

    def geom_as(self, alias: str, target_srid: int) -> str:
        """The geometry expressed in ``target_srid`` (SDO_CS.TRANSFORM only if needed)."""
        g = self.geom(alias)
        if not self.srid or not target_srid or self.srid == target_srid:
            return g
        return f"SDO_CS.TRANSFORM({g}, {int(target_srid)})"

    def summary(self) -> dict[str, Any]:
        return {
            "layer": self.qualified,
            "geometry_column": display_name(self.geom_column),
            "geometry_type": self.geom_type,
            "srid": self.srid,
            "crs": self.crs.label,
            "kind": self.kind,
            "row_estimate": self.row_estimate,
            "spatial_index": self.has_index,
        }


# --------------------------------------------------------------------------- attribute filters


def compile_filters(layer: OraLayer, filters: list[Filter] | None, alias: str, binds: Binds) -> str | None:
    """Compile filters to a WHERE fragment. Values become bind variables; identifiers come from the catalog."""
    if not filters:
        return None
    parts: list[str] = []
    for f in filters:
        c = layer.find_column(f.column)
        col = f"{alias}.{quote_ident(c.name)}"
        if c.is_spatial:
            raise MapsqlError(f"Column {c.display!r} is a geometry column; use the spatial tools to filter on geometry.")
        if not c.is_attribute:
            raise MapsqlError(f"Column {c.display!r} ({c.type}) cannot be used in filters.")
        op = f.op
        if op in ("is_null", "not_null"):
            parts.append(f"{col} IS {'NOT ' if op == 'not_null' else ''}NULL")
            continue
        if f.value is None:
            raise MapsqlError(f"Filter on {c.display!r} with op {op!r} needs a value.")
        if op in ("like", "ilike", "contains"):
            val = str(f.value)
            text = col if (c.is_text or c.base in ("CLOB", "NCLOB")) else f"TO_CHAR({col})"
            if op == "contains":
                val = "%" + val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                parts.append(f"UPPER({text}) LIKE UPPER({binds.add(val)}) ESCAPE '\\'")
            elif op == "like":
                parts.append(f"{text} LIKE {binds.add(val)}")
            else:
                parts.append(f"UPPER({text}) LIKE UPPER({binds.add(val)})")
        elif op in ("in", "not_in"):
            if not isinstance(f.value, (list, tuple)) or not f.value:
                raise MapsqlError(f"Filter op {op!r} on {c.display!r} needs a non-empty list value.")
            if len(f.value) > 500:
                raise MapsqlError("Too many values in an 'in' filter (max 500).")
            ph = ", ".join(binds.add(_value(c, v)) for v in f.value)
            parts.append(f"{col} {'IN' if op == 'in' else 'NOT IN'} ({ph})")
        elif op == "between":
            if not isinstance(f.value, (list, tuple)) or len(f.value) != 2:
                raise MapsqlError(f"Filter op 'between' on {c.display!r} needs [low, high].")
            parts.append(f"{col} BETWEEN {binds.add(_value(c, f.value[0]))} AND {binds.add(_value(c, f.value[1]))}")
        elif op in ("=", "!=", "<", "<=", ">", ">="):
            if isinstance(f.value, (list, dict)):
                raise MapsqlError(f"Filter op {op!r} on {c.display!r} needs a scalar value.")
            if f.value == "" and op in ("=", "!="):  # Oracle stores '' as NULL
                parts.append(f"{col} IS {'NOT ' if op == '!=' else ''}NULL")
                continue
            parts.append(f"{col} {'<>' if op == '!=' else op} {binds.add(_value(c, f.value))}")
        else:  # pragma: no cover
            raise MapsqlError(f"Unknown filter op {op!r}")
    return " AND ".join(parts)


def _value(c: OraColumn, value: Any) -> Any:
    """A comparison value coerced and validated against the column type."""
    if isinstance(value, (list, dict)):
        raise MapsqlError(f"Unsupported value {value!r} for column {c.display!r}.")
    if isinstance(value, float) and value != value:
        raise MapsqlError("NaN is not a valid filter value.")
    if c.is_numeric:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            try:
                return float(value) if any(ch in value for ch in ".eE") else int(value)
            except ValueError:
                raise MapsqlError(f"Column {c.display!r} is numeric ({c.type}) but the filter value is {value!r}.") from None
        return value
    if c.is_date:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                raise MapsqlError(f"Column {c.display!r} is a date; use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS), got {value!r}.") from None
        return value
    if isinstance(value, bool):
        return int(value)  # Oracle SQL has no boolean literal before 23ai; flags are NUMBER(1)/CHAR(1)
    return value


# --------------------------------------------------------------------------- catalog


def _chunks(seq: Sequence, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _pairs_filter(pairs: Sequence[tuple[str, str]], owner_col: str, table_col: str) -> tuple[str, dict[str, str]]:
    b = Binds("p")
    items = ", ".join(f"({b.add(o)}, {b.add(t)})" for o, t in pairs)
    return f"({owner_col}, {table_col}) IN ({items})", b.values


def _type_string(dt: str, length: int | None, char_len: int | None, prec: int | None, scale: int | None) -> str:
    dt = dt.upper()
    if dt in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return f"{dt}({char_len or length})"
    if dt == "NUMBER":
        if prec is None:
            return "NUMBER"
        return f"NUMBER({prec},{scale})" if scale else f"NUMBER({prec})"
    return dt


class OracleCatalog:
    TTL = 60.0

    def __init__(self, db: OracleDatabase):
        self.db = db
        self.settings = db.settings
        self._layers: list[OraLayer] | None = None
        self._known: OracleKnown | None = None
        self._ts = 0.0
        self._known_ts = 0.0

    def invalidate(self) -> None:
        self._layers = None
        self._known = None

    def _fresh(self) -> bool:
        return self._layers is not None and (time.monotonic() - self._ts) < self.TTL

    def layers(self, refresh: bool = False) -> list[OraLayer]:
        if refresh or not self._fresh():
            self._load()
        assert self._layers is not None
        return self._layers

    # ---- guard context -----------------------------------------------------------------

    def known_for_guard(self) -> OracleKnown:
        if self._known is not None and (time.monotonic() - self._known_ts) < self.TTL:
            return self._known
        with self.db.batch():
            return self._known_for_guard_inner()

    def _known_for_guard_inner(self) -> OracleKnown:
        cur = self.db.query("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL", max_rows=1).rows[0][0]
        objs = self.db.query(
            "SELECT object_name, object_type FROM all_objects WHERE owner = :o "
            "AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM')",
            {"o": cur}, max_rows=100000, catalog=True,
        )
        names = {r[0].lower() for r in objs.rows if r[1] != "SYNONYM"}
        syns: dict[str, tuple[str, str]] = {}
        sres = self.db.query(
            "SELECT synonym_name, table_owner, table_name, db_link FROM all_synonyms WHERE owner = :o",
            {"o": cur}, max_rows=100000, catalog=True,
        )
        for sname, towner, tname, dblink in sres.rows:
            syns[sname.lower()] = ("", "") if dblink else ((towner or "").lower(), (tname or "").lower())
        self._known = OracleKnown(current_schema=cur, objects={cur.lower(): names}, synonyms=syns)
        self._known_ts = time.monotonic()
        return self._known

    # ---- loading -------------------------------------------------------------------------

    def _load(self) -> None:
        with self.db.batch(timeout_ms=60000):
            self._load_inner()

    def _load_inner(self) -> None:
        db = self.db
        s = self.settings
        meta = db.query(
            "SELECT m.owner, m.table_name, m.column_name, m.srid, "
            "(SELECT d.sdo_tolerance FROM TABLE(m.diminfo) d WHERE ROWNUM = 1) "
            "FROM all_sdo_geom_metadata m ORDER BY m.owner, m.table_name, m.column_name",
            max_rows=100000, catalog=True,
        )
        registered = {(o, t, c): (srid, tol) for o, t, c, srid, tol in meta.rows}
        try:
            unreg = db.query(
                "SELECT c.owner, c.table_name, c.column_name FROM all_tab_columns c "
                "WHERE c.data_type = 'SDO_GEOMETRY' "
                "AND c.owner NOT IN (SELECT username FROM all_users WHERE oracle_maintained = 'Y') "
                "ORDER BY 1, 2, 3",
                max_rows=100000, catalog=True,
            )
        except MapsqlError:
            unreg = db.query(
                "SELECT c.owner, c.table_name, c.column_name FROM all_tab_columns c "
                "WHERE c.data_type = 'SDO_GEOMETRY' AND c.owner NOT IN ('MDSYS','SYS','SYSTEM','XDB','CTXSYS','ORDSYS') "
                "ORDER BY 1, 2, 3",
                max_rows=100000, catalog=True,
            )
        found: dict[tuple[str, str, str], tuple[int | None, float | None, bool]] = {}
        for (o, t, c), (srid, tol) in registered.items():
            found[(o, t, c)] = (srid, tol, True)
        for o, t, c in unreg.rows:
            found.setdefault((o, t, c), (None, None, False))
        visible = {k: v for k, v in found.items() if s.relation_allowed(k[0], k[1])}
        pairs = sorted({(o, t) for o, t, _ in visible})

        kinds: dict[tuple[str, str], str] = {}
        columns: dict[tuple[str, str], list[OraColumn]] = {}
        rows_est: dict[tuple[str, str], int] = {}
        indexed: set[tuple[str, str, str]] = set()
        for chunk in _chunks(pairs, 100):
            cond, p = _pairs_filter(chunk, "owner", "object_name")
            for o, t, ot in db.query(
                f"SELECT owner, object_name, object_type FROM all_objects WHERE object_type IN "
                f"('TABLE','VIEW','MATERIALIZED VIEW') AND {cond}", p, max_rows=100000, catalog=True,
            ).rows:
                # a materialized view shows up as TABLE and MATERIALIZED VIEW: prefer the latter
                if ot == "MATERIALIZED VIEW" or (o, t) not in kinds:
                    kinds[(o, t)] = ot.lower()
            cond, p = _pairs_filter(chunk, "owner", "table_name")
            cres = db.query(
                f"SELECT owner, table_name, column_name, data_type, data_length, char_length, data_precision, data_scale "
                f"FROM all_tab_columns WHERE {cond} ORDER BY owner, table_name, column_id",
                p, max_rows=200000, catalog=True,
            )
            for o, t, cn, dt, ln, cl, prec, scale in cres.rows:
                columns.setdefault((o, t), []).append(OraColumn(cn, _type_string(dt, ln, cl, prec, scale)))
            cond, p = _pairs_filter(chunk, "owner", "table_name")
            for o, t, n in db.query(
                f"SELECT owner, table_name, num_rows FROM all_tables WHERE {cond}", p, max_rows=100000, catalog=True
            ).rows:
                if n is not None:
                    rows_est[(o, t)] = int(n)
            cond, p = _pairs_filter(chunk, "ic.table_owner", "ic.table_name")
            for o, t, cn in db.query(
                "SELECT ic.table_owner, ic.table_name, ic.column_name FROM all_indexes i JOIN all_ind_columns ic "
                "ON ic.index_owner = i.owner AND ic.index_name = i.index_name "
                "WHERE i.ityp_owner = 'MDSYS' AND i.ityp_name LIKE 'SPATIAL_INDEX%' AND i.domidx_status = 'VALID' "
                f"AND i.domidx_opstatus = 'VALID' AND {cond}", p, max_rows=100000, catalog=True,
            ).rows:
                indexed.add((o, t, cn))

        crs_map = self._load_crs({srid for srid, _, _ in visible.values() if srid})

        layers: list[OraLayer] = []
        group: dict[tuple[str, str], list[str]] = {}
        for (o, t, c) in visible:
            group.setdefault((o, t), []).append(c)
        for (o, t, c), (srid, tol, reg) in sorted(visible.items()):
            if (o, t) not in kinds or not any(col.name == c for col in columns.get((o, t), [])):
                continue  # registered in the metadata but the object/column is gone or not accessible
            gtype, sampled_srid = self._sample_geometry(o, t, c)
            if not srid:
                srid = sampled_srid or 0
            crs = crs_map.get(srid) or OraCrs(srid, "unknown")
            if tol is None:
                tol = 0.05 if crs.kind == "geographic" else 0.005
            layers.append(
                OraLayer(
                    owner=o, table=t, geom_column=c, geom_type=gtype, srid=int(srid or 0), kind=kinds[(o, t)], crs=crs,
                    tolerance=float(tol), has_index=(o, t, c) in indexed, columns=columns.get((o, t), []),
                    other_geom_columns=[display_name(x) for x in group[(o, t)] if x != c],
                    row_estimate=rows_est.get((o, t)), registered=reg,
                )
            )
        self._layers = layers
        self._ts = time.monotonic()

    def _load_crs(self, srids: set[int]) -> dict[int, OraCrs]:
        out: dict[int, OraCrs] = {}
        if not srids:
            return out
        b = Binds("s")
        ph = ", ".join(b.add(int(x)) for x in sorted(srids))
        try:
            res = self.db.query(
                "SELECT c.srid, c.coord_ref_sys_name, c.coord_ref_sys_kind, s.wktext FROM mdsys.sdo_coord_ref_sys c "
                f"LEFT JOIN mdsys.cs_srs s ON s.srid = c.srid WHERE c.srid IN ({ph})",
                b.values, max_rows=5000, catalog=True,
            )
        except MapsqlError:
            return out
        for srid, name, kind, wkt in res.rows:
            k = (kind or "").upper()
            if k.startswith("GEOGRAPHIC"):
                ck = "geographic"
            elif k == "PROJECTED":
                ck = "projected"
            else:
                ck = "unknown"
            metre = False
            if ck == "projected" and wkt:
                units = re.findall(r'UNIT\s*\[\s*"([^"]+)"', wkt, flags=re.I)
                metre = bool(units) and units[-1].strip().lower() in ("meter", "metre")
            out[int(srid)] = OraCrs(int(srid), ck, metre, name)
        return out

    def _sample_geometry(self, owner: str, table: str, col: str) -> tuple[str, int | None]:
        """Geometry type (and SRID) from a sample of rows: SDO_GTYPE is not part of the metadata."""
        q = (
            f"SELECT MOD(s.g.SDO_GTYPE, 100), s.g.SDO_SRID, COUNT(*) FROM (SELECT t.{quote_ident(col)} AS g FROM "
            f"{quote_ident(owner)}.{quote_ident(table)} t WHERE t.{quote_ident(col)} IS NOT NULL FETCH FIRST 500 ROWS ONLY) s "
            "GROUP BY MOD(s.g.SDO_GTYPE, 100), s.g.SDO_SRID"
        )
        try:
            res = self.db.query(q, max_rows=50, timeout_ms=4000)
        except MapsqlError:
            return "GEOMETRY", None
        types = {_GTYPE_NAMES.get(int(r[0]), "GEOMETRY") for r in res.rows if r[0] is not None}
        srids = [r[1] for r in res.rows if r[1]]
        if len(types) == 1:
            gt = next(iter(types))
        elif types and len({t.replace("MULTI", "") for t in types}) == 1:
            gt = "MULTI" + next(iter(types)).replace("MULTI", "")  # POLYGON + MULTIPOLYGON is one family in Oracle
        else:
            gt = "GEOMETRY"
        return gt, (int(srids[0]) if srids else None)

    # ---- lookup --------------------------------------------------------------------------

    def resolve(self, name: str, geometry_column: str | None = None) -> OraLayer:
        if not isinstance(name, str) or not name.strip():
            raise NeedsClarification("A layer name is required. Call list_layers to see the available layers.")
        raw = name.strip()
        low = raw.replace('"', "").lower()
        layers = self.layers()
        if "." in low:
            sch, tbl = low.split(".", 1)
            cand = [l for l in layers if l.owner.lower() == sch and l.table.lower() == tbl]
        else:
            cand = [l for l in layers if l.table.lower() == low]
        if geometry_column:
            cand = [l for l in cand if l.geom_column.lower() == geometry_column.lower()]
        if not cand:
            names = [l.qualified for l in layers]
            close = difflib.get_close_matches(low, names + [display_name(l.table) for l in layers], n=4, cutoff=0.4)
            hint = f" Did you mean: {', '.join(dict.fromkeys(close))}?" if close else ""
            raise LayerNotFound(
                f"Layer {name!r} not found (or not visible).{hint} Call list_layers to see available layers."
            )
        distinct = {(l.owner, l.table) for l in cand}
        if len(distinct) > 1:
            raise NeedsClarification(
                f"Layer name {name!r} is ambiguous: {', '.join(sorted(f'{display_name(o)}.{display_name(t)}' for o, t in distinct))}. "
                "Use the schema-qualified name."
            )
        return cand[0]
