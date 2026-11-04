#!/usr/bin/env python
"""Generate the Oracle showcase maps (docs/images/oracle_*.png) by calling the same service layer the MCP tools use.

    python scripts/make_oracle_demo_maps.py [--no-basemap] [--ini oracle-test.ini]

Needs the Oracle demo schema (scripts/oracle_demo_db.py). Connection: the usual environment (DATABASE_URL=oracle://... or
MAPSQL_ORACLE_DSN/USER/PASSWORD) or, with --ini, the [oracle_ro] section of an ini file.
Map data (c) OpenStreetMap contributors (ODbL); Natural Earth is public domain.
"""

from __future__ import annotations

import argparse
import configparser
from pathlib import Path

from mapsql_mcp.backends.oracle import OracleService
from mapsql_mcp.config import Settings
from mapsql_mcp.layers import Filter
from mapsql_mcp.render import MapLayer, render_html, render_png

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
KIZILAY = (32.8541, 39.9208)  # Kizilay square, Ankara


def layer_from(svc: OracleService, res: dict, name: str, color: str | None = None, **kw) -> MapLayer:
    item = svc.store.get(res["result_id"])
    return MapLayer(name=name, features=item["fc"]["features"], color=color, **kw)


def settings_from(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    s.backend = "oracle"
    if args.ini:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(args.ini, encoding="utf-8")
        sec = cp[args.section]
        s.oracle_dsn, s.oracle_user, s.oracle_password = sec["dsn"], sec["user"], sec["password"]
    s.register_secrets()
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-basemap", action="store_true")
    ap.add_argument("--ini", default=None, help="ini file with an [oracle_ro] section (instead of environment variables)")
    ap.add_argument("--section", default="oracle_ro")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / ".tiles"
    basemap = "none" if args.no_basemap else "osm"
    svc = OracleService(settings_from(args))
    pharm = [Filter(column="kind", value="pharmacy")]
    try:
        # 1. "Pharmacies within 500 m of Kizilay" (geodetic layer, SDO_WITHIN_DISTANCE ... unit=M)
        ring = svc.buffer(500, lon=KIZILAY[0], lat=KIZILAY[1])
        res = svc.within_distance("ankara_pois", 500, lon=KIZILAY[0], lat=KIZILAY[1], filters=pharm, columns=["name", "kind"], limit=200)
        layers = [layer_from(svc, ring, "500 m radius", "#64748b"),
                  layer_from(svc, res, f"pharmacies ({res['count']})", "#e11d48", label_property="name")]
        png, info = render_png(layers, title="Oracle Spatial: pharmacies within 500 m of Kizilay", basemap=basemap, cache_dir=cache)
        (OUT / "oracle_pharmacies_near_kizilay.png").write_bytes(png)
        (OUT / "oracle_pharmacies_near_kizilay.html").write_text(render_html(layers, "Pharmacies within 500 m of Kizilay (Oracle Spatial)"), encoding="utf-8")
        print("1:", info.basemap, res["count"], "pharmacies")

        # 2. Same question on the TUREF TM30 (EPSG:5254, metres) copy of the layer
        tm = svc.within_distance("ankara_pois_tm30", 500, lon=KIZILAY[0], lat=KIZILAY[1], filters=pharm, columns=["name", "kind"], limit=200)
        layers = [layer_from(svc, ring, "500 m radius", "#64748b"),
                  layer_from(svc, tm, f"pharmacies, TUREF TM30 layer ({tm['count']})", "#7c3aed", label_property="name")]
        png, info = render_png(layers, title="Projected layer (TUREF TM30, SRID 5254): pharmacies within 500 m", basemap=basemap, cache_dir=cache)
        (OUT / "oracle_tm30_pharmacies.png").write_bytes(png)
        print("2:", info.basemap, tm["count"], "pharmacies (TM30)")

        # 3. Choropleth: cafes per neighbourhood (spatial_join: set-based spatial join on both indexes)
        sj = svc.spatial_join("ankara_neighbourhoods", "ankara_pois", "contains", right_filters=[Filter(column="kind", value="cafe")],
                              limit=200, geometry="none")
        layers = [layer_from(svc, sj, "cafes per neighbourhood", "#f59e0b", value_property="right_count", label_property="name")]
        png, info = render_png(layers, title="Oracle Spatial: cafes per neighbourhood (spatial_join + choropleth)", basemap=basemap, cache_dir=cache)
        (OUT / "oracle_cafes_per_neighbourhood.png").write_bytes(png)
        (OUT / "oracle_cafes_per_neighbourhood.html").write_text(render_html(layers, "Cafes per neighbourhood (Oracle Spatial)"), encoding="utf-8")
        print("3:", info.basemap, sum(f["properties"]["right_count"] for f in svc.store.get(sj["result_id"])["fc"]["features"]), "cafes")

        # 4. Five nearest hospitals (SDO_NN)
        near = svc.nearest("ankara_pois", 5, lon=KIZILAY[0], lat=KIZILAY[1], filters=[Filter(column="kind", value="hospital")], columns=["name", "kind"])
        origin = svc.buffer(40, lon=KIZILAY[0], lat=KIZILAY[1])
        layers = [layer_from(svc, origin, "Kizilay", "#111827"), layer_from(svc, near, "5 nearest hospitals", "#16a34a", label_property="name")]
        png, info = render_png(layers, title="Oracle Spatial: 5 nearest hospitals to Kizilay (SDO_NN)", basemap=basemap, cache_dir=cache)
        (OUT / "oracle_nearest_hospitals.png").write_bytes(png)
        print("4:", info.basemap, [f["properties"].get("distance_m") for f in near["features"]])

        # 5. Big cities of Turkey (world layers, geodetic polygons simplified for the response)
        tr = svc.find_features("world_countries", [Filter(column="name", value="Turkey")], columns=["name"])
        cities = svc.find_features("world_cities", [Filter(column="country", value="Turkey"), Filter(column="pop_max", op=">", value=500000)],
                                   columns=["name", "pop_max"], order_by="pop_max", descending=True)
        layers = [layer_from(svc, tr, "Turkey", "#64748b"), layer_from(svc, cities, f"cities > 500k ({cities['count']})", "#2563eb", label_property="name")]
        png, info = render_png(layers, title="Oracle Spatial: cities over 500,000 inhabitants in Turkey", basemap=basemap, cache_dir=cache)
        (OUT / "oracle_turkey_big_cities.png").write_bytes(png)
        print("5:", info.basemap, cities["count"])
    finally:
        svc.close()


if __name__ == "__main__":
    main()
