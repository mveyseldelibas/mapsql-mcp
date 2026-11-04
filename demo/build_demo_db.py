#!/usr/bin/env python
"""Build the mapsql-mcp demo database from openly licensed data.

* ``world`` schema  - Natural Earth 1:50m (public domain): countries, provinces, populated places, rivers, lakes
* ``ankara`` schema - OpenStreetMap extract of central Ankara via Overpass API (ODbL):
                      POIs, roads, parks and neighbourhood (mahalle) boundaries

Everything is downloaded once into ``demo/.cache`` (~15 MB) and loaded with plain INSERTs, so no
GDAL / osm2pgsql is needed. Only an empty PostGIS-enabled database and psycopg are required.

    python demo/build_demo_db.py --database-url postgresql://postgres@127.0.0.1:55432/mapsql_demo

Attribution (see also demo/ATTRIBUTION.md):
    Natural Earth - public domain, https://www.naturalearthdata.com
    (c) OpenStreetMap contributors - ODbL 1.0, https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import psycopg
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

HERE = Path(__file__).resolve().parent
NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
NE_FILES = [
    "ne_50m_admin_0_countries",
    "ne_50m_admin_1_states_provinces",
    "ne_50m_populated_places_simple",
    "ne_50m_rivers_lake_centerlines",
    "ne_50m_lakes",
]
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA = "mapsql-mcp-demo-builder/0.1 (open-source; https://github.com/mveyseldelibas/mapsql-mcp)"
# central Ankara (Turkey): S, W, N, E  (~6.5 km x 7.5 km around Kizilay)
BBOX = (39.895, 32.810, 39.955, 32.900)

POI_QUERY = """
[out:json][timeout:180][bbox:{s},{w},{n},{e}];
(
  nwr["amenity"~"^(restaurant|cafe|fast_food|bar|pub|pharmacy|hospital|clinic|school|university|college|bank|library|cinema|theatre|place_of_worship|police|fuel|marketplace|post_office|dentist|kindergarten)$"];
  nwr["shop"~"^(supermarket|mall|convenience|bakery)$"];
  nwr["tourism"~"^(hotel|museum|attraction|hostel|gallery)$"];
  nwr["railway"="station"];
  nwr["station"="subway"];
);
out center tags;
"""
ROAD_QUERY = """
[out:json][timeout:180][bbox:{s},{w},{n},{e}];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|pedestrian)$"];
out geom tags;
"""
PARK_QUERY = """
[out:json][timeout:180][bbox:{s},{w},{n},{e}];
way["leisure"~"^(park|garden)$"];
out geom tags;
"""
HOOD_QUERY = """
[out:json][timeout:180];
rel["boundary"="administrative"]["admin_level"="8"]({s},{w},{n},{e});
out geom;
"""


def log(*a):
    print("[demo]", *a, flush=True)


REFRESH = False


def download(url: str, dest: Path, *, data: dict | None = None) -> None:
    """Fetch ``url`` into the cache. A pinned snapshot in demo/data/*.gz is preferred so the
    demo database (and therefore the benchmark gold answers) is reproducible offline."""
    if dest.exists() and dest.stat().st_size > 100:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    snap = HERE / "data" / (dest.name + ".gz")
    if snap.exists() and not REFRESH:
        import gzip

        dest.write_bytes(gzip.decompress(snap.read_bytes()))
        return
    last = None
    urls = [url] if data is None else OVERPASS
    for u in urls:
        for attempt in range(3):
            try:
                log("downloading", dest.name, "from", u.split("/")[2])
                if data is None:
                    r = httpx.get(u, headers={"User-Agent": UA}, timeout=120, follow_redirects=True)
                else:
                    r = httpx.post(u, data=data, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=240)
                r.raise_for_status()
                dest.write_bytes(r.content)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(3 * (attempt + 1))
    raise SystemExit(f"download failed for {dest.name}: {last}")


def overpass(name: str, query: str, cache: Path) -> dict:
    s, w, n, e = BBOX
    dest = cache / f"osm_ankara_{name}.json"
    download(OVERPASS[0], dest, data={"data": query.format(s=s, w=w, n=n, e=e)})
    return json.loads(dest.read_text(encoding="utf-8"))["elements"]


def ne(name: str, cache: Path) -> list[dict]:
    dest = cache / f"{name}.geojson"
    download(f"{NE_BASE}/{name}.geojson", dest)
    return json.loads(dest.read_text(encoding="utf-8"))["features"]


# ---------------------------------------------------------------------------- geometry helpers


def to_multi(geom) -> object:
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    return geom


def fix_poly(geom):
    if not geom.is_valid:
        geom = make_valid(geom)
    polys = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type in ("Polygon", "MultiPolygon")]
    flat = []
    for g in polys:
        flat.extend(g.geoms if g.geom_type == "MultiPolygon" else [g])
    return MultiPolygon(flat) if flat else None


def relation_polygon(rel: dict):
    outer, inner = [], []
    for m in rel.get("members", []):
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        line = LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        (inner if m.get("role") == "inner" else outer).append(line)
    if not outer:
        return None
    polys = list(polygonize(unary_union(outer)))
    if not polys:
        return None
    shell = unary_union(polys)
    if inner:
        holes = list(polygonize(unary_union(inner)))
        if holes:
            shell = shell.difference(unary_union(holes))
    return fix_poly(shell)


# ---------------------------------------------------------------------------- DDL / load

DDL = """
DROP SCHEMA IF EXISTS world CASCADE;
DROP SCHEMA IF EXISTS ankara CASCADE;
CREATE SCHEMA world;
CREATE SCHEMA ankara;

CREATE TABLE world.countries (
    iso_a3 text, name text, continent text, subregion text, income_group text,
    pop_est bigint, gdp_musd bigint, geom geometry(MultiPolygon, 4326)
);
CREATE TABLE world.provinces (
    name text, country text, iso_3166_2 text, type text, geom geometry(MultiPolygon, 4326)
);
CREATE TABLE world.cities (
    name text, country text, admin1 text, iso_a3 text, is_capital boolean, megacity boolean,
    pop_max integer, geom geometry(Point, 4326)
);
CREATE TABLE world.rivers (name text, scalerank smallint, geom geometry(MultiLineString, 4326));
CREATE TABLE world.lakes (name text, scalerank smallint, geom geometry(MultiPolygon, 4326));

CREATE TABLE ankara.pois (
    osm_id bigint, osm_type text, name text, category text, kind text, cuisine text,
    opening_hours text, wheelchair text, geom geometry(Point, 4326)
);
CREATE TABLE ankara.roads (
    osm_id bigint, name text, highway text, oneway text, maxspeed text, lanes text, surface text,
    geom geometry(LineString, 4326)
);
CREATE TABLE ankara.parks (osm_id bigint, name text, leisure text, geom geometry(MultiPolygon, 4326));
CREATE TABLE ankara.neighbourhoods (osm_id bigint, name text, admin_level int, population text, geom geometry(MultiPolygon, 4326));
"""

INDEXES = """
CREATE INDEX ON world.countries USING gist (geom);
CREATE INDEX ON world.provinces USING gist (geom);
CREATE INDEX ON world.cities USING gist (geom);
CREATE INDEX ON world.rivers USING gist (geom);
CREATE INDEX ON world.lakes USING gist (geom);
CREATE INDEX ON ankara.pois USING gist (geom);
CREATE INDEX ON ankara.roads USING gist (geom);
CREATE INDEX ON ankara.parks USING gist (geom);
CREATE INDEX ON ankara.neighbourhoods USING gist (geom);
CREATE INDEX ON ankara.pois (category, kind);
COMMENT ON TABLE world.countries IS 'Natural Earth 1:50m admin-0 countries (public domain)';
COMMENT ON TABLE world.provinces IS 'Natural Earth 1:50m admin-1 states/provinces (public domain)';
COMMENT ON TABLE world.cities IS 'Natural Earth 1:50m populated places (public domain)';
COMMENT ON TABLE world.rivers IS 'Natural Earth 1:50m rivers and lake centerlines (public domain)';
COMMENT ON TABLE world.lakes IS 'Natural Earth 1:50m lakes (public domain)';
COMMENT ON TABLE ankara.pois IS 'OpenStreetMap points of interest, central Ankara (c) OpenStreetMap contributors, ODbL';
COMMENT ON TABLE ankara.roads IS 'OpenStreetMap roads, central Ankara (c) OpenStreetMap contributors, ODbL';
COMMENT ON TABLE ankara.parks IS 'OpenStreetMap parks and gardens, central Ankara (c) OpenStreetMap contributors, ODbL';
COMMENT ON TABLE ankara.neighbourhoods IS 'OpenStreetMap admin_level=8 boundaries (mahalle), central Ankara (c) OpenStreetMap contributors, ODbL';
"""


def insert_many(cur, table: str, cols: list[str], rows: list[tuple], geom_kind: str | None = None) -> None:
    """rows: attribute values followed by a shapely geometry (or None)."""
    if not rows:
        return
    ph = ", ".join(["%s"] * (len(cols)))
    sql = f"INSERT INTO {table} ({', '.join(cols)}, geom) VALUES ({ph}, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
    payload = []
    for r in rows:
        *vals, g = r
        payload.append((*vals, json.dumps(mapping(g))))
    cur.executemany(sql, payload)
    log(f"  {table}: {len(rows)} rows")


def tags(el: dict) -> dict:
    return el.get("tags", {})


def load_world(cur, cache: Path) -> None:
    log("loading Natural Earth")
    rows = []
    for f in ne("ne_50m_admin_0_countries", cache):
        p = f["properties"]
        g = fix_poly(__import__("shapely").geometry.shape(f["geometry"]))
        if g is None:
            continue
        iso = p["ISO_A3"] if p["ISO_A3"] not in (None, "-99") else p["ADM0_A3"]
        rows.append((iso, p["NAME"], p["CONTINENT"], p["SUBREGION"], p["INCOME_GRP"], int(p["POP_EST"] or 0), int(p["GDP_MD"] or 0), g))
    insert_many(cur, "world.countries", ["iso_a3", "name", "continent", "subregion", "income_group", "pop_est", "gdp_musd"], rows)

    from shapely.geometry import shape

    rows = []
    for f in ne("ne_50m_admin_1_states_provinces", cache):
        p = f["properties"]
        g = fix_poly(shape(f["geometry"]))
        if g is None:
            continue
        rows.append((p["name"], p["admin"], p["iso_3166_2"], p["type_en"], g))
    insert_many(cur, "world.provinces", ["name", "country", "iso_3166_2", "type"], rows)

    rows = []
    for f in ne("ne_50m_populated_places_simple", cache):
        p = f["properties"]
        rows.append((p["name"], p["adm0name"], p["adm1name"], p["adm0_a3"], bool(p["adm0cap"]), bool(p["megacity"]), int(p["pop_max"] or 0), shape(f["geometry"])))
    insert_many(cur, "world.cities", ["name", "country", "admin1", "iso_a3", "is_capital", "megacity", "pop_max"], rows)

    rows = []
    for f in ne("ne_50m_rivers_lake_centerlines", cache):
        p = f["properties"]
        g = shape(f["geometry"])
        if g.geom_type == "LineString":
            from shapely.geometry import MultiLineString

            g = MultiLineString([g])
        if p.get("featurecla") == "River":
            rows.append((p["name"], p["scalerank"], g))
    insert_many(cur, "world.rivers", ["name", "scalerank"], rows)

    rows = []
    for f in ne("ne_50m_lakes", cache):
        p = f["properties"]
        g = fix_poly(shape(f["geometry"]))
        if g is not None:
            rows.append((p["name"], p["scalerank"], g))
    insert_many(cur, "world.lakes", ["name", "scalerank"], rows)


def poi_category(t: dict) -> tuple[str, str] | None:
    for key in ("amenity", "shop", "tourism"):
        if key in t:
            return key, t[key]
    if t.get("railway") == "station" or t.get("station") == "subway":
        return "railway", "subway_station" if t.get("station") == "subway" else "station"
    return None


def load_ankara(cur, cache: Path) -> None:
    log("loading OpenStreetMap (central Ankara)")
    rows, seen = [], set()
    for el in overpass("pois", POI_QUERY, cache):
        t = tags(el)
        cat = poi_category(t)
        if cat is None:
            continue
        if el["type"] == "node":
            lon, lat = el["lon"], el["lat"]
        elif "center" in el:
            lon, lat = el["center"]["lon"], el["center"]["lat"]
        else:
            continue
        key = (el["type"], el["id"])
        if key in seen:
            continue
        seen.add(key)
        rows.append((el["id"], el["type"], t.get("name"), cat[0], cat[1], t.get("cuisine"), t.get("opening_hours"), t.get("wheelchair"), Point(lon, lat)))
    insert_many(cur, "ankara.pois", ["osm_id", "osm_type", "name", "category", "kind", "cuisine", "opening_hours", "wheelchair"], rows)

    rows = []
    for el in overpass("roads", ROAD_QUERY, cache):
        pts = el.get("geometry")
        if not pts or len(pts) < 2:
            continue
        t = tags(el)
        rows.append((el["id"], t.get("name"), t["highway"], t.get("oneway"), t.get("maxspeed"), t.get("lanes"), t.get("surface"), LineString([(p["lon"], p["lat"]) for p in pts])))
    insert_many(cur, "ankara.roads", ["osm_id", "name", "highway", "oneway", "maxspeed", "lanes", "surface"], rows)

    rows = []
    for el in overpass("parks", PARK_QUERY, cache):
        pts = el.get("geometry")
        if not pts or len(pts) < 4 or pts[0] != pts[-1]:
            continue
        g = fix_poly(Polygon([(p["lon"], p["lat"]) for p in pts]))
        if g is None or g.is_empty:
            continue
        t = tags(el)
        rows.append((el["id"], t.get("name"), t["leisure"], g))
    insert_many(cur, "ankara.parks", ["osm_id", "name", "leisure"], rows)

    rows = []
    for el in overpass("hoods", HOOD_QUERY, cache):
        if el["type"] != "relation":
            continue
        g = relation_polygon(el)
        if g is None or g.is_empty:
            continue
        t = tags(el)
        rows.append((el["id"], t.get("name"), int(t.get("admin_level", 8)), t.get("population"), g))
    insert_many(cur, "ankara.neighbourhoods", ["osm_id", "name", "admin_level", "population"], rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("MAPSQL_DEMO_URL", "postgresql://postgres@127.0.0.1:55432/mapsql_demo"))
    ap.add_argument("--cache-dir", default=str(HERE / ".cache"))
    ap.add_argument("--refresh", action="store_true", help="ignore the pinned snapshot in demo/data and download fresh data (gold answers of the benchmark may change)")
    args = ap.parse_args()
    global REFRESH
    REFRESH = args.refresh
    cache = Path(args.cache_dir)
    with psycopg.connect(args.database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute(DDL)
            load_world(cur, cache)
            load_ankara(cur, cache)
            cur.execute(INDEXES)
        conn.commit()
        conn.autocommit = True
        conn.execute("ANALYZE")
    log("done. Attribution: Natural Earth (public domain); (c) OpenStreetMap contributors (ODbL).")


if __name__ == "__main__":
    main()
