# Demo data attribution

The demo database (`demo/build_demo_db.py`) is built from two openly licensed sources. The pinned snapshots in
`demo/data/` are re-encoded (minified JSON, gzip) copies of the original downloads.

## Natural Earth (public domain)

Countries, admin-1 provinces, populated places, rivers and lakes at 1:50 million scale.
Made with Natural Earth. Free vector and raster map data @ https://www.naturalearthdata.com
(public domain, no attribution required; thanks to Natural Earth and the `nvkelso/natural-earth-vector` mirror).

## OpenStreetMap (ODbL 1.0)

Points of interest, roads, parks and neighbourhood (admin_level=8) boundaries of central Ankara, Turkey,
fetched on 2026-11-02 through the Overpass API.

(c) OpenStreetMap contributors. The data is available under the Open Database License (ODbL):
https://www.openstreetmap.org/copyright

The ODbL "share-alike" clause applies to any database you derive from this extract; keep this attribution
if you redistribute it. The code of this repository is MIT licensed; the data files are not.
