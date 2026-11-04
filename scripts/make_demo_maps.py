#!/usr/bin/env python
"""Generate the showcase maps used in the README by calling the same service layer the MCP tools use.

    python scripts/make_demo_maps.py [--no-basemap]

Needs the demo database (see demo/build_demo_db.py) and DATABASE_URL (default: dev cluster).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mapsql_mcp.config import Settings
from mapsql_mcp.layers import Filter
from mapsql_mcp.render import MapLayer, render_html, render_png
from mapsql_mcp.service import GeoService

OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
KIZILAY = (32.8541, 39.9208)  # Kizilay square, Ankara


def layer_from(svc: GeoService, res: dict, name: str, color: str | None = None, **kw) -> MapLayer:
    item = svc.store.get(res["result_id"])
    return MapLayer(name=name, features=item["fc"]["features"], color=color, **kw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-basemap", action="store_true")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/mapsql_demo"))
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / ".tiles"
    basemap = "none" if args.no_basemap else "osm"
    svc = GeoService(Settings(database_url=args.database_url))
    try:
        # 1. "Pharmacies within 500 m of Kizilay"
        ring = svc.buffer(500, lon=KIZILAY[0], lat=KIZILAY[1])
        pharm = svc.within_distance(
            "ankara.pois", 500, lon=KIZILAY[0], lat=KIZILAY[1], filters=[Filter(column="kind", value="pharmacy")],
            columns=["name", "kind"], limit=200,
        )
        layers = [
            layer_from(svc, ring, "500 m radius", "#64748b"),
            layer_from(svc, pharm, f"pharmacies ({pharm['count']})", "#e11d48", label_property="name"),
        ]
        png, info = render_png(layers, title="Pharmacies within 500 m of Kizilay", basemap=basemap, cache_dir=cache)
        (OUT / "pharmacies_near_kizilay.png").write_bytes(png)
        (OUT / "pharmacies_near_kizilay.html").write_text(render_html(layers, "Pharmacies within 500 m of Kizilay"), encoding="utf-8")
        print("1:", info.basemap, info.features, "features;", pharm["count"], "pharmacies")

        # 2. Choropleth: cafes per neighbourhood (spatial join)
        sj = svc.spatial_join(
            "ankara.neighbourhoods", "ankara.pois", "contains", right_filters=[Filter(column="kind", value="cafe")],
            limit=200, geometry="none",
        )
        layers = [layer_from(svc, sj, "cafes per neighbourhood", "#f59e0b", value_property="right_count", label_property="name")]
        png, info = render_png(layers, title="Cafes per neighbourhood (spatial_join + choropleth)", basemap=basemap, cache_dir=cache)
        (OUT / "cafes_per_neighbourhood.png").write_bytes(png)
        (OUT / "cafes_per_neighbourhood.html").write_text(render_html(layers, "Cafes per neighbourhood"), encoding="utf-8")
        print("2:", info.basemap, info.features)

        # 3. Five nearest hospitals + clinics... to Kizilay
        near = svc.nearest("ankara.pois", 5, lon=KIZILAY[0], lat=KIZILAY[1], filters=[Filter(column="kind", value="hospital")], columns=["name", "kind"])
        origin = svc.buffer(40, lon=KIZILAY[0], lat=KIZILAY[1])
        layers = [layer_from(svc, origin, "Kizilay", "#111827"), layer_from(svc, near, "5 nearest hospitals", "#16a34a", label_property="name")]
        png, info = render_png(layers, title="5 nearest hospitals to Kizilay", basemap=basemap, cache_dir=cache)
        (OUT / "nearest_hospitals.png").write_bytes(png)
        print("3:", info.basemap, [f["properties"].get("distance_m") for f in near["features"]])

        # 4. Big cities of Turkey
        tr = svc.find_features("world.countries", [Filter(column="name", value="Turkey")], columns=["name"])
        cities = svc.find_features(
            "world.cities", [Filter(column="country", value="Turkey"), Filter(column="pop_max", op=">", value=500000)],
            columns=["name", "pop_max"], order_by="pop_max", descending=True,
        )
        layers = [layer_from(svc, tr, "Turkey", "#64748b"), layer_from(svc, cities, f"cities > 500k ({cities['count']})", "#2563eb", label_property="name")]
        png, info = render_png(layers, title="Cities over 500,000 inhabitants in Turkey", basemap=basemap, cache_dir=cache)
        (OUT / "turkey_big_cities.png").write_bytes(png)
        print("4:", info.basemap, cities["count"])
    finally:
        svc.close()


if __name__ == "__main__":
    main()
