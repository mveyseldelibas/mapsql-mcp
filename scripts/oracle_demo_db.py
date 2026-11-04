#!/usr/bin/env python
"""Build the Oracle Spatial demo schema for mapsql-mcp (re-runnable).

Creates (as an administrator) two users and loads the same layers as the PostGIS demo
(``demo/build_demo_db.py``: Natural Earth + OpenStreetMap central Ankara, pinned offline snapshot):

* ``MAPSQL_DEMO``  owns the tables (single schema, so ``world.countries`` becomes ``WORLD_COUNTRIES`` ...),
                   ``USER_SDO_GEOM_METADATA`` rows and spatial indexes, plus a TUREF TM30 (EPSG:5254)
                   copy of two Ankara layers to exercise projected, metre-based coordinate systems.
* ``MAPSQL_RO``    least-privilege role for the MCP server: CREATE SESSION + SELECT on the demo tables only.

Connection details come from an ini file (default ``oracle-test.ini`` in the repository root, git-ignored)::

    [oracle]            ; administrator, e.g. ADMIN on an Autonomous Database
    user = ADMIN
    password = ...
    dsn = (description=...)  | host:port/service | tcps://host:1522/service

The script generates random passwords for the two users on first run and writes them to the same file as
``[oracle_demo]`` and ``[oracle_ro]`` sections. Passwords are never printed.

    python scripts/oracle_demo_db.py                 # everything
    python scripts/oracle_demo_db.py --users-only    # just (re)create users / grants
    python scripts/oracle_demo_db.py --no-users      # reload data only (users must exist)

Attribution: Natural Earth (public domain); (c) OpenStreetMap contributors (ODbL 1.0).
"""

from __future__ import annotations

import argparse
import configparser
import importlib.util
import os
import secrets
import string
import sys
import time
from pathlib import Path

import oracledb
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEMO_USER, RO_USER = "MAPSQL_DEMO", "MAPSQL_RO"
TM30_SRID = 5254  # "TUREF / TM30" in MDSYS.CS_SRS (EPSG:5254)

# --------------------------------------------------------------------------- layers
# name -> (columns [(name, oracle type)], layer_gtype for the spatial index)
LAYERS: dict[str, tuple[list[tuple[str, str]], str]] = {
    "WORLD_COUNTRIES": (
        [("ISO_A3", "VARCHAR2(16)"), ("NAME", "VARCHAR2(200)"), ("CONTINENT", "VARCHAR2(100)"),
         ("SUBREGION", "VARCHAR2(100)"), ("INCOME_GROUP", "VARCHAR2(100)"), ("POP_EST", "NUMBER(19)"),
         ("GDP_MUSD", "NUMBER(19)")], "POLYGON"),
    "WORLD_PROVINCES": (
        [("NAME", "VARCHAR2(200)"), ("COUNTRY", "VARCHAR2(200)"), ("ISO_3166_2", "VARCHAR2(20)"),
         ("TYPE", "VARCHAR2(100)")], "POLYGON"),
    "WORLD_CITIES": (
        [("NAME", "VARCHAR2(200)"), ("COUNTRY", "VARCHAR2(200)"), ("ADMIN1", "VARCHAR2(200)"),
         ("ISO_A3", "VARCHAR2(16)"), ("IS_CAPITAL", "NUMBER(1)"), ("MEGACITY", "NUMBER(1)"),
         ("POP_MAX", "NUMBER(12)")], "POINT"),
    "WORLD_RIVERS": ([("NAME", "VARCHAR2(200)"), ("SCALERANK", "NUMBER(5)")], "LINE"),
    "WORLD_LAKES": ([("NAME", "VARCHAR2(200)"), ("SCALERANK", "NUMBER(5)")], "POLYGON"),
    "ANKARA_POIS": (
        [("OSM_ID", "NUMBER(19)"), ("OSM_TYPE", "VARCHAR2(20)"), ("NAME", "VARCHAR2(400)"),
         ("CATEGORY", "VARCHAR2(50)"), ("KIND", "VARCHAR2(80)"), ("CUISINE", "VARCHAR2(400)"),
         ("OPENING_HOURS", "VARCHAR2(1000)"), ("WHEELCHAIR", "VARCHAR2(50)")], "POINT"),
    "ANKARA_ROADS": (
        [("OSM_ID", "NUMBER(19)"), ("NAME", "VARCHAR2(400)"), ("HIGHWAY", "VARCHAR2(50)"),
         ("ONEWAY", "VARCHAR2(50)"), ("MAXSPEED", "VARCHAR2(50)"), ("LANES", "VARCHAR2(50)"),
         ("SURFACE", "VARCHAR2(80)")], "LINE"),
    "ANKARA_PARKS": ([("OSM_ID", "NUMBER(19)"), ("NAME", "VARCHAR2(400)"), ("LEISURE", "VARCHAR2(50)")], "POLYGON"),
    "ANKARA_NEIGHBOURHOODS": (
        [("OSM_ID", "NUMBER(19)"), ("NAME", "VARCHAR2(400)"), ("ADMIN_LEVEL", "NUMBER(3)"),
         ("POPULATION", "VARCHAR2(50)")], "POLYGON"),
}
#: layers that also get a TUREF TM30 copy (name of the copy -> source)
TM30_COPIES = {"ANKARA_POIS_TM30": "ANKARA_POIS", "ANKARA_NEIGHBOURHOODS_TM30": "ANKARA_NEIGHBOURHOODS"}

COMMENTS = {
    "WORLD_COUNTRIES": "Natural Earth 1:50m admin-0 countries (public domain)",
    "WORLD_PROVINCES": "Natural Earth 1:50m admin-1 states/provinces (public domain)",
    "WORLD_CITIES": "Natural Earth 1:50m populated places (public domain)",
    "WORLD_RIVERS": "Natural Earth 1:50m rivers and lake centerlines (public domain)",
    "WORLD_LAKES": "Natural Earth 1:50m lakes (public domain)",
    "ANKARA_POIS": "OpenStreetMap points of interest, central Ankara (c) OpenStreetMap contributors, ODbL",
    "ANKARA_ROADS": "OpenStreetMap roads, central Ankara (c) OpenStreetMap contributors, ODbL",
    "ANKARA_PARKS": "OpenStreetMap parks and gardens, central Ankara (c) OpenStreetMap contributors, ODbL",
    "ANKARA_NEIGHBOURHOODS": "OpenStreetMap admin_level=8 boundaries (mahalle), central Ankara (c) OpenStreetMap contributors, ODbL",
    "ANKARA_POIS_TM30": "ANKARA_POIS transformed to TUREF / TM30 (EPSG:5254, metres) to test projected CRS handling",
    "ANKARA_NEIGHBOURHOODS_TM30": "ANKARA_NEIGHBOURHOODS transformed to TUREF / TM30 (EPSG:5254, metres)",
}

GEODETIC_DIM = "SDO_DIM_ARRAY(SDO_DIM_ELEMENT('Longitude', -180, 180, 0.05), SDO_DIM_ELEMENT('Latitude', -90, 90, 0.05))"
TM30_DIM = "SDO_DIM_ARRAY(SDO_DIM_ELEMENT('X', -1000000, 2000000, 0.005), SDO_DIM_ELEMENT('Y', 0, 10000000, 0.005))"


def log(*a: object) -> None:
    print("[oracle-demo]", *a, flush=True)


# --------------------------------------------------------------------------- credentials


def read_ini(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        raise SystemExit(f"{path} not found. Create it with an [oracle] section (user, password, dsn).")
    cp.read(path, encoding="utf-8")
    if "oracle" not in cp or not all(k in cp["oracle"] for k in ("user", "password", "dsn")):
        raise SystemExit(f"{path}: the [oracle] section needs user, password and dsn.")
    return cp


def write_ini(cp: configparser.ConfigParser, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def random_password() -> str:
    """24 characters, letters + digits, starts with a letter (satisfies the Autonomous DB password policy)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = secrets.choice(string.ascii_letters) + "".join(secrets.choice(alphabet) for _ in range(23))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def connect(cp: configparser.ConfigParser, section: str) -> oracledb.Connection:
    s = cp[section]
    return oracledb.connect(user=s["user"], password=s["password"], dsn=s["dsn"])


def user_exists(cur: oracledb.Cursor, name: str) -> bool:
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=name)
    return cur.fetchone()[0] > 0


def setup_users(cp: configparser.ConfigParser, ini: Path) -> None:
    """Create / refresh MAPSQL_DEMO and MAPSQL_RO as the administrator. Nothing else is touched."""
    admin = connect(cp, "oracle")
    try:
        cur = admin.cursor()
        cur.execute("SELECT default_tablespace FROM user_users")
        tbs = cur.fetchone()[0]
        for user, section, grants in (
            (DEMO_USER, "oracle_demo", ["CREATE SESSION", "CREATE TABLE", "CREATE SEQUENCE"]),
            (RO_USER, "oracle_ro", ["CREATE SESSION"]),
        ):
            have_pw = section in cp and cp[section].get("password")
            if user_exists(cur, user):
                if not have_pw:  # user exists but we do not know the password: set a new one
                    pw = random_password()
                    cur.execute(f'ALTER USER {user} IDENTIFIED BY "{pw}"')
                    cp[section] = {"user": user, "password": pw, "dsn": cp["oracle"]["dsn"]}
                    log(f"{user}: existing user, password reset and stored in {ini.name} [{section}]")
                else:
                    log(f"{user}: exists")
            else:
                pw = cp[section]["password"] if have_pw else random_password()
                cur.execute(f'CREATE USER {user} IDENTIFIED BY "{pw}"' + (f" QUOTA UNLIMITED ON {tbs}" if user == DEMO_USER else ""))
                cp[section] = {"user": user, "password": pw, "dsn": cp["oracle"]["dsn"]}
                log(f"{user}: created (password stored in {ini.name} [{section}])")
            cur.execute("GRANT " + ", ".join(grants) + f" TO {user}")
        write_ini(cp, ini)
    finally:
        admin.close()


# --------------------------------------------------------------------------- geometry prep


def prep_geometry(g):
    """Snap to a 1e-6 degree grid, drop empties and orient polygons the way Oracle (geodetic) wants:
    exterior rings counter-clockwise, holes clockwise."""
    if g is None or g.is_empty:
        return None
    g = shapely.set_precision(g, 1e-6)
    if g.is_empty:
        return None
    if g.geom_type == "Polygon":
        return orient(g, 1.0)
    if g.geom_type == "MultiPolygon":
        parts = [orient(p, 1.0) for p in g.geoms if not p.is_empty]
        return MultiPolygon(parts) if parts else None
    return g


def wkt_of(g) -> str:
    return shapely.to_wkt(g, rounding_precision=7, trim=True)


# --------------------------------------------------------------------------- DDL / load


def drop_table(cur: oracledb.Cursor, name: str) -> None:
    cur.execute(
        "BEGIN EXECUTE IMMEDIATE 'DROP TABLE " + name + " PURGE'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
    )
    cur.execute("DELETE FROM user_sdo_geom_metadata WHERE table_name = :t", t=name)


def create_layer_table(cur: oracledb.Cursor, name: str) -> None:
    cols, _ = LAYERS[name]
    drop_table(cur, name)
    cur.execute(f"CREATE TABLE {name} (" + ", ".join(f"{c} {t}" for c, t in cols) + ", GEOM SDO_GEOMETRY)")


def register(cur: oracledb.Cursor, name: str, srid: int) -> None:
    dim = TM30_DIM if srid == TM30_SRID else GEODETIC_DIM
    cur.execute("DELETE FROM user_sdo_geom_metadata WHERE table_name = :t", t=name)
    cur.execute(
        "INSERT INTO user_sdo_geom_metadata (table_name, column_name, diminfo, srid) "
        f"VALUES (:t, 'GEOM', {dim}, :s)", t=name, s=srid,
    )


def tolerance(srid: int) -> float:
    return 0.005 if srid == TM30_SRID else 0.05


class Loader:
    """Replaces ``insert_many`` of demo/build_demo_db.py so its row builders feed Oracle instead of PostGIS."""

    def __init__(self, conn: oracledb.Connection):
        self.conn = conn
        self.cur = conn.cursor()
        self.loaded: dict[str, int] = {}
        self.skipped: dict[str, int] = {}

    def insert_many(self, _cur, table: str, cols: list[str], rows: list[tuple], geom_kind: str | None = None) -> None:
        name = table.replace(".", "_").upper()
        spec = dict(LAYERS[name][0])
        create_layer_table(self.cur, name)
        names = [c.upper() for c in cols]
        small: list[tuple] = []
        big: list[tuple] = []
        skipped = 0
        for r in rows:
            *vals, g = r
            g = prep_geometry(g)
            if g is None:
                skipped += 1
                continue
            vals = [self._coerce(v, spec[n]) for v, n in zip(vals, names)]
            wkt = wkt_of(g)
            (small if len(wkt) <= 30000 else big).append((*vals, wkt))
        sql = (
            f"INSERT INTO {name} ({', '.join(names)}, GEOM) VALUES ({', '.join(f':{i + 1}' for i in range(len(names)))}, "
            f"SDO_GEOMETRY(:{len(names) + 1}, 4326))"
        )
        for i in range(0, len(small), 500):
            self.cur.executemany(sql, small[i:i + 500])
        if big:
            for *vals, wkt in big:  # long WKT: bind a temporary CLOB
                self.cur.execute(sql, [*vals, self.conn.createlob(oracledb.DB_TYPE_CLOB, wkt)])
        self.conn.commit()
        self.loaded[name] = len(small) + len(big)
        self.skipped[name] = skipped
        log(f"  {name}: {len(small) + len(big)} rows" + (f" ({skipped} empty geometries skipped)" if skipped else ""))

    @staticmethod
    def _coerce(v, typ: str):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, str) and typ.startswith("VARCHAR2"):
            return v[: int(typ[typ.index("(") + 1: typ.index(")")])]
        return v


def validate_and_fix(cur: oracledb.Cursor, conn: oracledb.Connection, name: str, tol: float) -> tuple[int, int]:
    """Rectify invalid geometries; delete those that stay invalid. Returns (rectified, removed)."""
    bad = f"SDO_GEOM.VALIDATE_GEOMETRY_WITH_CONTEXT(geom, {tol}) <> 'TRUE'"
    cur.execute(f"SELECT COUNT(*) FROM {name} WHERE {bad}")
    n_bad = cur.fetchone()[0]
    if not n_bad:
        return 0, 0
    cur.execute(f"UPDATE {name} SET geom = SDO_UTIL.RECTIFY_GEOMETRY(geom, {tol}) WHERE {bad}")
    conn.commit()
    cur.execute(f"SELECT COUNT(*) FROM {name} WHERE {bad}")
    still = cur.fetchone()[0]
    if still:
        cur.execute(f"SELECT SDO_GEOM.VALIDATE_GEOMETRY_WITH_CONTEXT(geom, {tol}) FROM {name} WHERE {bad} FETCH FIRST 3 ROWS ONLY")
        log(f"  {name}: {still} geometry(ies) cannot be repaired and are dropped, e.g. {[r[0][:60] for r in cur.fetchall()]}")
        cur.execute(f"DELETE FROM {name} WHERE {bad}")
        conn.commit()
    return n_bad - still, still


def build_index(cur: oracledb.Cursor, name: str, gtype: str) -> None:
    # layer_gtype is only given for point layers: POLYGON/LINE would reject the multi-part variants (2007/2006)
    params = f" PARAMETERS('layer_gtype=POINT')" if gtype == "POINT" else ""
    cur.execute(f"CREATE INDEX {name[:24]}_SIDX ON {name}(geom) INDEXTYPE IS MDSYS.SPATIAL_INDEX_V2{params}")


def load_demo(cp: configparser.ConfigParser, cache: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_demo_db", ROOT / "demo" / "build_demo_db.py")
    bdb = importlib.util.module_from_spec(spec)
    sys.modules["build_demo_db"] = bdb
    spec.loader.exec_module(bdb)  # type: ignore[union-attr]

    conn = connect(cp, "oracle_demo")
    try:
        cur = conn.cursor()
        loader = Loader(conn)
        bdb.insert_many = loader.insert_many  # the row builders look this name up at call time
        t0 = time.time()
        bdb.load_world(None, cache)
        bdb.load_ankara(None, cache)

        log("TUREF TM30 copies")
        for copy, src in TM30_COPIES.items():
            cols, gtype = LAYERS[src]
            drop_table(cur, copy)
            LAYERS[copy] = (cols, gtype)
            cur.execute(
                f"CREATE TABLE {copy} AS SELECT {', '.join(c for c, _ in cols)}, SDO_CS.TRANSFORM(geom, {TM30_SRID}) AS geom FROM {src}"
            )
            log(f"  {copy}")
        conn.commit()

        for name, (_cols, gtype) in LAYERS.items():
            srid = TM30_SRID if name.endswith("_TM30") else 4326
            register(cur, name, srid)
            conn.commit()
            fixed, removed = validate_and_fix(cur, conn, name, tolerance(srid))
            if fixed or removed:
                log(f"  {name}: {fixed} geometries rectified, {removed} removed")
            build_index(cur, name, gtype)
            cur.execute(f"COMMENT ON TABLE {name} IS '{COMMENTS[name]}'")
            cur.callproc("DBMS_STATS.GATHER_TABLE_STATS", [DEMO_USER, name])
            cur.execute(f"SELECT COUNT(*) FROM {name}")
            log(f"  {name}: {cur.fetchone()[0]} rows, indexed")
        for name in LAYERS:
            cur.execute(f"GRANT SELECT ON {name} TO {RO_USER}")
        log(f"done in {time.time() - t0:.0f} s. Attribution: Natural Earth (public domain); (c) OpenStreetMap contributors (ODbL).")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ini", default=os.environ.get("MAPSQL_ORACLE_INI", str(ROOT / "oracle-test.ini")))
    ap.add_argument("--cache-dir", default=str(ROOT / "demo" / ".cache"))
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--users-only", action="store_true", help="only create/refresh the two users and their grants")
    grp.add_argument("--no-users", action="store_true", help="skip user creation (they must exist)")
    args = ap.parse_args()
    ini = Path(args.ini)
    cp = read_ini(ini)
    if not args.no_users:
        setup_users(cp, ini)
    if not args.users_only:
        if "oracle_demo" not in cp:
            raise SystemExit("[oracle_demo] section missing; run without --no-users first.")
        load_demo(cp, Path(args.cache_dir))


if __name__ == "__main__":
    main()
