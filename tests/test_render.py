"""Rendering: PNG validity, HTML safety, choropleth, offline behaviour. No network, no database."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from mapsql_mcp.errors import MapsqlError
from mapsql_mcp.geo import ResultStore, count_coords, shape_response
from mapsql_mcp.render import MapLayer, render_html, render_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def feat(geom, **props):
    return {"type": "Feature", "properties": props, "geometry": geom}


POINT = {"type": "Point", "coordinates": [32.85, 39.92]}
POLY = {"type": "Polygon", "coordinates": [[[32.80, 39.90], [32.90, 39.90], [32.90, 39.95], [32.80, 39.95], [32.80, 39.90]]]}
POLY_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[3, 3], [3, 6], [6, 6], [6, 3], [3, 3]],
    ],
}
LINE = {"type": "LineString", "coordinates": [[32.80, 39.90], [32.90, 39.95]]}


def test_png_is_valid_and_has_requested_size():
    png, info = render_png(
        [MapLayer("pts", [feat(POINT, name="Kizilay")]), MapLayer("area", [feat(POLY, name="A")]), MapLayer("road", [feat(LINE)])],
        title="Test map", width=800, height=500, basemap="none",
    )
    assert png.startswith(PNG_MAGIC)
    img = Image.open(io.BytesIO(png))
    assert img.size == (800, 500) and img.format == "PNG"
    assert info.features == 3 and info.layers == 3 and info.basemap == "none"
    assert info.bbox == [32.8, 39.9, 32.9, 39.95]
    # something was actually drawn: the image is not a single flat colour
    assert len(img.convert("RGB").resize((80, 50)).getcolors(maxcolors=100000)) > 5


def test_single_point_and_polygon_with_hole_and_multi_geometries():
    png, _ = render_png([MapLayer("p", [feat(POINT)])], basemap="none")
    assert png.startswith(PNG_MAGIC)
    png, _ = render_png([MapLayer("h", [feat(POLY_HOLE)])], basemap="none")
    assert png.startswith(PNG_MAGIC)
    multi = {"type": "GeometryCollection", "geometries": [POINT, LINE, POLY]}
    png, _ = render_png([MapLayer("m", [feat(multi)])], basemap="none")
    assert png.startswith(PNG_MAGIC)
    mp = {"type": "MultiPoint", "coordinates": [[0, 0], [1, 1]]}
    png, _ = render_png([MapLayer("mp", [feat(mp)])], basemap="none")
    assert png.startswith(PNG_MAGIC)


def test_choropleth_and_labels():
    feats = [
        feat({"type": "Polygon", "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]]}, name=f"P{i}", n=i * 3)
        for i in range(8)
    ]
    layer = MapLayer("choro", feats, value_property="n", label_property="name")
    png, info = render_png([layer], title="choropleth", basemap="none")
    assert png.startswith(PNG_MAGIC) and not info.notes
    # non-numeric value property degrades gracefully
    png, info = render_png([MapLayer("x", feats, value_property="name")], basemap="none")
    assert png.startswith(PNG_MAGIC) and info.notes


def test_nothing_to_draw_is_an_error():
    with pytest.raises(MapsqlError, match="Nothing to draw"):
        render_png([MapLayer("empty", [feat(None, a=1)])], basemap="none")
    with pytest.raises(MapsqlError, match="Nothing to draw"):
        render_png([], basemap="none")


def test_offline_basemap_failure_falls_back(monkeypatch, tmp_path):
    import mapsql_mcp.render as r

    monkeypatch.setattr(r, "_fetch_tile", lambda *a, **k: None)
    png, info = render_png([MapLayer("p", [feat(POINT)])], basemap="osm", cache_dir=tmp_path)
    assert png.startswith(PNG_MAGIC) and info.basemap == "none"
    assert any("Basemap unavailable" in n for n in info.notes)


def test_basemap_tiles_are_used_and_attributed(monkeypatch, tmp_path):
    import mapsql_mcp.render as r

    calls = []

    def fake_fetch(url, cache_file, timeout=6.0):
        calls.append(url)
        return Image.new("RGB", (256, 256), (200, 220, 200))

    monkeypatch.setattr(r, "_fetch_tile", fake_fetch)
    png, info = render_png([MapLayer("p", [feat(POLY)])], basemap="osm", cache_dir=tmp_path)
    assert info.basemap == "osm" and 1 <= len(calls) <= 16
    assert all(u.startswith("https://tile.openstreetmap.org/") for u in calls)


def test_html_is_standalone_and_xss_safe():
    evil = {"name": "</script><script>alert(1)</script>", "html": "<img src=x onerror=alert(1)>"}
    html = render_html([MapLayer("L", [feat(POINT, **evil)], color="#ff0000")], title="<b>T</b> & co")
    assert "leaflet" in html.lower() and "OpenStreetMap" in html
    assert "</script><script>alert(1)" not in html  # payload cannot terminate the script block
    assert "&lt;b&gt;T&lt;/b&gt; &amp; co" in html
    assert "textContent" in html  # popups are built with textContent, never innerHTML
    start = html.index("const LAYERS = ") + len("const LAYERS = ")
    data = json.loads(html[start: html.index(";\nconst map")])
    assert data[0]["features"][0]["properties"]["name"] == evil["name"]  # data survives round-trip


def test_count_coords_and_shape_response_budget():
    big = {"type": "Polygon", "coordinates": [[[i / 100, (i % 7) / 100] for i in range(2000)] + [[0, 0]]]}
    assert count_coords(POINT) == 1 and count_coords(POLY) == 5 and count_coords(None) == 0
    fc = {"type": "FeatureCollection", "features": [feat(big, name="big"), feat(POINT, name="p")]}
    auto, note = shape_response(fc, geometry="auto", budget=100)
    assert note and auto["features"][0]["geometry"]["type"] == "Point" and auto["features"][1]["geometry"] == POINT
    full, note = shape_response(fc, geometry="full")
    assert note is None and full["features"][0]["geometry"]["type"] == "Polygon"
    none, _ = shape_response(fc, geometry="none")
    assert all(f["geometry"] is None for f in none["features"])
    assert fc["features"][0]["geometry"]["type"] == "Polygon"  # input not mutated


def test_result_store_lru_and_unknown_id():
    st = ResultStore(max_items=2)
    ids = [st.put({"features": []}, f"t{i}") for i in range(3)]
    with pytest.raises(MapsqlError, match="Unknown result_id"):
        st.get(ids[0])
    assert st.get(ids[2])["title"] == "t2"
