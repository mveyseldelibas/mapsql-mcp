"""Each tool against a deterministic PostGIS fixture (see conftest.FIXTURE_SQL)."""

from __future__ import annotations

import json

import psycopg
import pytest

from mapsql_mcp.errors import GuardError, LayerNotFound, MapsqlError, NeedsClarification
from mapsql_mcp.layers import Filter
from mapsql_mcp.service import Metric

from .conftest import TEST_URL, names

pytestmark = pytest.mark.db


def F(column, value=None, op="="):
    return Filter(column=column, op=op, value=value)


# ------------------------------------------------------------------------ catalog


def test_catalog_discovers_layers_and_crs(svc):
    by = {l.qualified: l for l in svc.catalog.layers()}
    assert {"mt.cities", "mt.districts", "mt.roads", "mt.sensors", "mt.parcels_ft", "mt.geog_pts", "mt.v_untyped"} <= set(by)
    assert by["mt.cities"].srid == 4326 and by["mt.cities"].crs.kind == "geographic"
    assert by["mt.sensors"].srid == 32636 and by["mt.sensors"].crs.kind == "projected" and by["mt.sensors"].crs.metre
    assert by["mt.parcels_ft"].crs.kind == "projected" and not by["mt.parcels_ft"].crs.metre
    assert by["mt.geog_pts"].is_geography and by["mt.geog_pts"].geom_column == "geog"
    # untyped view column: SRID is discovered by sampling
    assert by["mt.v_untyped"].srid == 4326
    assert by["mt.v_untyped"].kind == "view"
    assert "mt.log" not in by  # no geometry -> not a layer


def test_list_layers(svc):
    res = svc.list_layers(schema="mt")
    got = {l["layer"]: l for l in res["layers"]}
    assert got["mt.cities"]["geometry_type"] == "POINT"
    assert got["mt.cities"]["srid"] == 4326
    assert "pop:integer" in got["mt.cities"]["columns"]
    assert got["mt.cities"]["extent_wgs84"] is not None
    assert svc.list_layers(search="roads")["count"] == 1
    assert svc.list_layers(search="pop")["count"] >= 1  # matches column names too


def test_describe_layer(svc):
    d = svc.describe_layer("mt.cities")
    assert d["row_count"] == 6
    assert d["extent_wgs84"][0] == pytest.approx(0.0) and d["extent_wgs84"][2] == pytest.approx(10.1)
    assert d["geometry_types_in_sample"] == {"POINT": 6}
    assert {c["name"] for c in d["columns"]} >= {"id", "name", "pop", "geom"}
    assert d["numeric_stats"]["pop"]["max"] == 4000
    top = {v["value"] for v in d["top_values"]["country"]}
    assert top == {"Aland", "Bland", "Cland"}


def test_layer_resolution_and_suggestions(svc):
    assert svc.catalog.resolve("cities").qualified == "mt.cities"
    assert svc.catalog.resolve("MT.Cities").qualified == "mt.cities"
    with pytest.raises(LayerNotFound, match="Did you mean.*mt.cities"):
        svc.catalog.resolve("citie")
    with pytest.raises(NeedsClarification):
        svc.catalog.resolve("")


# ------------------------------------------------------------------------ retrieval


def test_sample_features(svc):
    r = svc.sample_features("mt.cities", 3)
    assert r["count"] == 3 and r["result_id"].startswith("res_")
    assert r["features"][0]["geometry"]["type"] == "Point"


def test_find_features_filters_and_ops(svc):
    assert names(svc.find_features("mt.cities", [F("country", "Aland")], order_by="pop")) == ["Alpha", "Bravo"]
    assert names(svc.find_features("mt.cities", [F("pop", 2000, ">=")], order_by="pop", descending=True))[:2] == ["Delta", "Charlie"]
    assert names(svc.find_features("mt.cities", [F("name", "ALP", "contains")])) == ["Alpha"]
    assert names(svc.find_features("mt.cities", [F("name", "a%", "ilike")], order_by="name")) == ["Alpha"]
    assert names(svc.find_features("mt.cities", [F("country", ["Aland", "Bland"], "in")], order_by="id")) == [
        "Alpha", "Bravo", "Charlie", "Delta"
    ]
    assert names(svc.find_features("mt.cities", [F("pop", [1000, 2000], "between")], order_by="id")) == ["Alpha", "Bravo"]
    assert svc.find_features("mt.cities", [F("pop", op="is_null")])["count"] == 1
    assert svc.find_features("mt.cities", [F("pop", op="not_null")])["count"] == 5
    assert svc.find_features("mt.cities", [F("capital", True)])["count"] == 2
    # numeric given as string is accepted for numeric columns
    assert svc.find_features("mt.cities", [F("pop", "1000")])["count"] == 1


def test_find_features_is_injection_safe(svc):
    evil = "x'; DROP TABLE mt.cities; --"
    assert svc.find_features("mt.cities", [F("name", evil)])["count"] == 0
    r = svc.find_features("mt.cities", [F("name", "O'Brien; DROP TABLE x")])
    assert r["count"] == 1  # the quote-laden name is matched literally
    assert svc.find_features("mt.cities")["count"] == 6  # table still there
    for bad_col in ['name"; DROP TABLE mt.cities; --', "nope", "geom"]:
        with pytest.raises(MapsqlError):
            svc.find_features("mt.cities", [F(bad_col, "x")])
    with pytest.raises(MapsqlError):
        svc.find_features("mt.cities; DROP TABLE mt.cities")
    with pytest.raises(MapsqlError):
        svc.find_features("mt.cities", order_by="id; DROP TABLE mt.cities")
    with pytest.raises(MapsqlError, match="numeric"):
        svc.find_features("mt.cities", [F("pop", "abc")])


def test_find_features_truncation_and_total(svc):
    r = svc.find_features("mt.cities", limit=2, columns=["name"])
    assert r["count"] == 2 and r["truncated"] is True and r["total_matches"] == 6
    assert "note" in r
    assert svc.find_features("mt.cities", columns=["id"])["truncated"] is False


def test_geometry_modes(svc):
    r = svc.find_features("mt.districts", geometry="none")
    assert all(f["geometry"] is None for f in r["features"])
    r = svc.find_features("mt.districts", geometry="centroid")
    assert all(f["geometry"]["type"] == "Point" for f in r["features"])
    r = svc.find_features("mt.districts", geometry="full")
    assert all(f["geometry"]["type"] == "Polygon" for f in r["features"])
    # the full geometry stays available server-side
    stored = svc.store.get(r["result_id"])["fc"]
    assert stored["features"][0]["geometry"]["type"] == "Polygon"


# ------------------------------------------------------------------------ distance


def independent_distance_m(url, lon1, lat1, lon2, lat2):
    with psycopg.connect(url) as c:
        return c.execute(
            "SELECT ST_Distance(ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography)",
            (lon1, lat1, lon2, lat2),
        ).fetchone()[0]


def test_within_distance_geographic_matches_independent_calc(svc, db_url):
    # Alpha(0,0) Bravo(0.01,0)=1113 m Charlie(0.02,0)=2226 m
    r = svc.within_distance("mt.cities", 1500, lon=0, lat=0, columns=["name"])
    assert names(r) == ["Alpha", "Bravo"]
    d = {f["properties"]["name"]: f["properties"]["distance_m"] for f in r["features"]}
    assert d["Alpha"] == 0
    assert d["Bravo"] == pytest.approx(independent_distance_m(db_url, 0, 0, 0.01, 0), abs=0.1)
    assert 1100 < d["Bravo"] < 1125
    # just below / above the true distance
    assert names(svc.within_distance("mt.cities", 1110, lon=0, lat=0, columns=["name"])) == ["Alpha"]
    assert names(svc.within_distance("mt.cities", 1115, lon=0, lat=0, columns=["name"])) == ["Alpha", "Bravo"]
    # ordered nearest first
    assert names(svc.within_distance("mt.cities", 5000, lon=0.02, lat=0, columns=["name"])) == ["Charlie", "Bravo", "Alpha"]


def test_within_distance_projected_metre_crs(svc):
    # sensors are in UTM 36N metres; reference given as lon/lat (33E, 0N == 500000, 0)
    r = svc.within_distance("mt.sensors", 600, lon=33.0, lat=0.0, columns=["label"])
    assert sorted(f["properties"]["label"] for f in r["features"]) == ["s1", "s2"]
    assert [f["properties"]["distance_m"] for f in r["features"]][0] == pytest.approx(0, abs=1)
    assert svc.within_distance("mt.sensors", 1200, lon=33.0, lat=0.0)["count"] == 3
    # geometry comes back in WGS84 whatever the native SRID
    g = r["features"][0]["geometry"]["coordinates"]
    assert g[0] == pytest.approx(33.0, abs=1e-4) and g[1] == pytest.approx(0.0, abs=1e-4)


def test_within_distance_feet_crs_is_converted_to_metres(svc, db_url):
    # parcels are in feet; the radius is still metres. i*0.001 deg lon at 40.7N ~ 84.4 m
    d1 = independent_distance_m(db_url, -74.0, 40.7, -73.999, 40.7)
    r = svc.within_distance("mt.parcels_ft", d1 + 1, lon=-74.0, lat=40.7)
    assert r["count"] == 2
    r = svc.within_distance("mt.parcels_ft", d1 - 1, lon=-74.0, lat=40.7)
    assert r["count"] == 1
    dists = sorted(f["properties"]["distance_m"] for f in svc.within_distance("mt.parcels_ft", 1000, lon=-74.0, lat=40.7)["features"])
    assert dists[1] == pytest.approx(d1, rel=0.01)


def test_within_distance_geography_column(svc):
    r = svc.within_distance("mt.geog_pts", 600, lon=0, lat=0, columns=["name"])
    assert names(r) == ["g1", "g2"]  # g2 is ~556 m away
    assert r["features"][1]["properties"]["distance_m"] == pytest.approx(556.6, abs=1)


def test_within_distance_reference_feature_and_clarification(svc):
    r = svc.within_distance(
        "mt.cities", 1500, reference_layer="mt.cities", reference_filters=[F("name", "Alpha")], columns=["name"]
    )
    assert names(r) == ["Alpha", "Bravo"]
    # two features match -> the tool must refuse to guess
    with pytest.raises(NeedsClarification, match="which one"):
        svc.within_distance("mt.cities", 1500, reference_layer="mt.cities", reference_filters=[F("country", "Aland")])
    # ...unless told to use them all
    r = svc.within_distance(
        "mt.cities", 100, reference_layer="mt.cities", reference_filters=[F("country", "Aland")],
        use_all_matches=True, columns=["name"],
    )
    assert names(r) == ["Alpha", "Bravo"]
    with pytest.raises(MapsqlError, match="No feature"):
        svc.within_distance("mt.cities", 10, reference_layer="mt.cities", reference_filters=[F("name", "Nope")])


def test_missing_reference_asks_instead_of_guessing(svc):
    with pytest.raises(NeedsClarification, match="near WHAT"):
        svc.within_distance("mt.cities", 100)
    with pytest.raises(NeedsClarification, match="near WHAT"):
        svc.nearest("mt.cities", 3)
    with pytest.raises(NeedsClarification):
        svc.within_distance("mt.cities", 100, lon=1.0)  # lat missing
    with pytest.raises(NeedsClarification):
        svc.within_distance("mt.cities", 100, lon=0, lat=0, reference_layer="mt.cities")
    with pytest.raises(NeedsClarification, match="reference_filters"):
        svc.within_distance("mt.cities", 100, reference_layer="mt.cities")


def test_coordinate_validation(svc):
    with pytest.raises(MapsqlError, match="swapped"):
        svc.within_distance("mt.cities", 100, lon=40.0, lat=100.0)
    with pytest.raises(MapsqlError):
        svc.within_distance("mt.cities", 100, lon=float("nan"), lat=0)
    with pytest.raises(MapsqlError):
        svc.within_distance("mt.cities", -5, lon=0, lat=0)


def test_nearest(svc):
    r = svc.nearest("mt.cities", 3, lon=0.012, lat=0, columns=["name"])
    assert names(r) == ["Bravo", "Charlie", "Alpha"]
    ds = [f["properties"]["distance_m"] for f in r["features"]]
    assert ds == sorted(ds)
    r = svc.nearest("mt.cities", 5, lon=0, lat=0, max_distance_m=2000, columns=["name"])
    assert names(r) == ["Alpha", "Bravo"]
    r = svc.nearest("mt.cities", 2, lon=0, lat=0, filters=[F("country", "Bland")], columns=["name"])
    assert names(r) == ["Charlie", "Delta"]
    r = svc.nearest("mt.sensors", 2, lon=33.0, lat=0.0, columns=["label"])
    assert [f["properties"]["label"] for f in r["features"]] == ["s1", "s2"]


# ------------------------------------------------------------------------ buffer


def test_buffer_point_area(svc):
    r = svc.buffer(1000, lon=0, lat=0)
    area = r["features"][0]["properties"]["area_km2"]
    assert area == pytest.approx(3.14159, rel=0.01)  # pi * 1 km^2
    assert r["features"][0]["geometry"]["type"] == "Polygon"


def test_buffer_layer_dissolve(svc):
    r = svc.buffer(100, layer="mt.cities", filters=[F("country", "Aland")])
    p = r["features"][0]["properties"]
    assert p["n_source_features"] == 2
    assert p["area_km2"] == pytest.approx(2 * 0.0314, rel=0.05)  # two separate 100 m discs
    r = svc.buffer(100, layer="mt.cities", filters=[F("country", "Aland")], dissolve=False)
    assert r["count"] == 2
    with pytest.raises(MapsqlError, match="No features matched"):
        svc.buffer(100, layer="mt.cities", filters=[F("name", "Nope")])
    with pytest.raises(NeedsClarification):
        svc.buffer(100)


# ------------------------------------------------------------------------ spatial_filter


def test_spatial_filter_other_layer(svc):
    r = svc.spatial_filter("mt.cities", "within", other_layer="mt.districts", other_filters=[F("name", "West")], columns=["name"])
    assert sorted(names(r)) == ["Alpha", "Bravo"]
    r = svc.spatial_filter("mt.cities", "within", other_layer="mt.districts", columns=["name"])
    assert sorted(names(r)) == ["Alpha", "Bravo", "Charlie", "Echo", "O'Brien; DROP TABLE x"]  # every city but Delta
    r = svc.spatial_filter("mt.districts", "contains", other_layer="mt.cities", other_filters=[F("name", "Charlie")], columns=["name"])
    assert names(r) == ["East"]
    r = svc.spatial_filter("mt.roads", "intersects", other_layer="mt.districts", other_filters=[F("name", "East")], columns=["name"])
    assert sorted(names(r)) == ["Main St"]
    r = svc.spatial_filter("mt.cities", "disjoint", other_layer="mt.districts", columns=["name"])
    assert names(r) == ["Delta"]


def test_spatial_filter_cross_srid(svc):
    # sensors (UTM) vs districts (4326): the sensor at (33E, 0N) is nowhere near those districts -> empty
    assert svc.spatial_filter("mt.sensors", "within", other_layer="mt.districts")["count"] == 0
    # same via bbox in WGS84
    assert svc.spatial_filter("mt.sensors", "intersects", bbox=[32.99, -0.01, 33.01, 0.01])["count"] == 3
    assert svc.spatial_filter("mt.parcels_ft", "intersects", bbox=[-74.01, 40.69, -73.99, 40.71])["count"] >= 2


def test_spatial_filter_bbox_and_geojson(svc):
    r = svc.spatial_filter("mt.cities", "intersects", bbox=[-1, -1, 0.5, 0.5], columns=["name"])
    assert sorted(names(r)) == ["Alpha", "Bravo", "Charlie"]
    poly = {"type": "Polygon", "coordinates": [[[0.9, 0.9], [1.1, 0.9], [1.1, 1.1], [0.9, 1.1], [0.9, 0.9]]]}
    assert names(svc.spatial_filter("mt.cities", "within", geojson_geometry=poly, columns=["name"])) == ["Delta"]
    with pytest.raises(MapsqlError):
        svc.spatial_filter("mt.cities", "intersects", bbox=[1, 1, 0, 0])
    with pytest.raises(NeedsClarification, match="WITH WHAT"):
        svc.spatial_filter("mt.cities", "intersects")
    with pytest.raises(NeedsClarification):
        svc.spatial_filter("mt.cities", "intersects", bbox=[0, 0, 1, 1], other_layer="mt.districts")
    with pytest.raises(MapsqlError):
        svc.spatial_filter("mt.cities", "intersects", geojson_geometry={"type": "Polygon", "coordinates": "x"})


def test_bbox_of(svc):
    r = svc.bbox_of("mt.cities", [F("country", "Aland")])
    assert r["bbox"] == [0.0, 0.0, 0.01, 0.0] and r["count"] == 2
    assert r["center"] == [0.005, 0.0]
    assert svc.bbox_of("mt.cities", [F("name", "Nope")])["bbox"] is None
    sens = svc.bbox_of("mt.sensors")
    assert sens["bbox"][0] == pytest.approx(33.0, abs=1e-3)  # reprojected to WGS84


# ------------------------------------------------------------------------ analysis


def test_aggregate(svc):
    r = svc.aggregate("mt.cities", [Metric(fn="count"), Metric(fn="sum", column="pop")], group_by=["country"], order_by="country", descending=False)
    assert [(x["country"], x["count"], x["sum_pop"]) for x in r["rows"]] == [("Aland", 2, 3000), ("Bland", 2, 7000), ("Cland", 2, 500)]
    r = svc.aggregate("mt.cities", [Metric(fn="avg", column="pop"), Metric(fn="max", column="pop"), Metric(fn="count_distinct", column="country")])
    assert r["rows"][0]["avg_pop"] == pytest.approx(2100) and r["rows"][0]["max_pop"] == 4000 and r["rows"][0]["distinct_country"] == 3
    r = svc.aggregate("mt.cities", filters=[F("pop", 1500, ">")])
    assert r["rows"][0]["count"] == 3
    # default ordering by first metric descending
    r = svc.aggregate("mt.cities", [Metric(fn="count")], group_by=["country"], filters=[F("pop", op="not_null")])
    assert r["rows"][0]["count"] == 2


def test_aggregate_area_and_length_are_geodesic(svc):
    r = svc.aggregate("mt.districts", [Metric(fn="area_km2")], filters=[F("name", "West")])
    # 0.02 x 0.01 degrees at the equator ~ 2226 m x 1113 m
    assert r["rows"][0]["area_km2"] == pytest.approx(2.478, rel=0.01)
    r = svc.aggregate("mt.roads", [Metric(fn="length_km")], filters=[F("name", "Main St")])
    assert r["rows"][0]["length_km"] == pytest.approx(2.226, rel=0.01)
    with pytest.raises(MapsqlError, match="polygon"):
        svc.aggregate("mt.cities", [Metric(fn="area_km2")])
    with pytest.raises(MapsqlError, match="numeric"):
        svc.aggregate("mt.cities", [Metric(fn="sum", column="name")])
    with pytest.raises(NeedsClarification):
        svc.aggregate("mt.cities", [Metric(fn="sum")])


def test_spatial_join_count_sum_attach(svc):
    r = svc.spatial_join("mt.districts", "mt.cities", "contains", geometry="none")
    got = {f["properties"]["name"]: f["properties"]["right_count"] for f in r["features"]}
    assert got == {"West": 2, "East": 1, "Far": 2}
    r = svc.spatial_join("mt.districts", "mt.cities", "contains", mode="sum", right_column="pop", geometry="none")
    got = {f["properties"]["name"]: f["properties"]["right_sum_pop"] for f in r["features"]}
    assert got["West"] == 3000 and got["East"] == 3000
    r = svc.spatial_join("mt.districts", "mt.cities", "contains", min_count=2, geometry="none")
    assert {f["properties"]["name"] for f in r["features"]} == {"West", "Far"}
    r = svc.spatial_join(
        "mt.districts", "mt.cities", "contains", mode="attach", right_columns=["name"],
        left_filters=[F("name", "West")], geometry="none",
    )
    assert sorted(f["properties"]["right_name"] for f in r["features"]) == ["Alpha", "Bravo"]
    r = svc.spatial_join("mt.districts", "mt.cities", "contains", right_filters=[F("pop", 1500, ">")], geometry="none")
    assert {f["properties"]["name"]: f["properties"]["right_count"] for f in r["features"]} == {"West": 1, "East": 1, "Far": 0}
    # left geometry is returned for mapping
    assert svc.spatial_join("mt.districts", "mt.cities", "contains")["features"][0]["geometry"]["type"] == "Polygon"


def test_spatial_join_dwithin_and_validation(svc):
    r = svc.spatial_join("mt.cities", "mt.cities", "dwithin", distance_m=1200, geometry="none", left_filters=[F("country", "Aland")])
    got = {f["properties"]["name"]: f["properties"]["right_count"] for f in r["features"]}
    assert got == {"Alpha": 2, "Bravo": 3}  # Bravo (1113 m from both neighbours) sees itself, Alpha and Charlie
    with pytest.raises(NeedsClarification, match="distance_m"):
        svc.spatial_join("mt.cities", "mt.cities", "dwithin")
    with pytest.raises(NeedsClarification, match="right_column"):
        svc.spatial_join("mt.districts", "mt.cities", "contains", mode="sum")
    with pytest.raises(MapsqlError, match="numeric"):
        svc.spatial_join("mt.districts", "mt.cities", "contains", mode="sum", right_column="name")
    # cross-SRID join: sensors (UTM) inside nothing; parcels (feet) vs cities
    assert svc.spatial_join("mt.districts", "mt.sensors", "contains", geometry="none")["count"] == 3


# ------------------------------------------------------------------------ run_sql


def test_run_sql_rows_and_geojson(svc):
    r = svc.run_sql("SELECT country, count(*) AS n FROM mt.cities GROUP BY country ORDER BY country")
    assert r["rows"] == [{"country": "Aland", "n": 2}, {"country": "Bland", "n": 2}, {"country": "Cland", "n": 2}]
    r = svc.run_sql("SELECT name, geom FROM mt.cities WHERE id = 2")
    assert r["geometry_column"] == "geom" and r["features"][0]["geometry"] == {"type": "Point", "coordinates": [0.01, 0]}
    # projected geometry is transformed to WGS84
    r = svc.run_sql("SELECT label, geom FROM mt.sensors WHERE id = 1")
    c = r["features"][0]["geometry"]["coordinates"]
    assert c[0] == pytest.approx(33.0, abs=1e-4)
    # geography column
    r = svc.run_sql("SELECT name, geog FROM mt.geog_pts WHERE id = 2")
    assert r["features"][0]["geometry"]["coordinates"][0] == pytest.approx(0.005)
    # computed geometry
    r = svc.run_sql("SELECT ST_Centroid(ST_Collect(geom)) AS c FROM mt.cities WHERE country = 'Aland'")
    assert r["features"][0]["geometry"]["coordinates"] == [0.005, 0]


def test_run_sql_limits(svc):
    r = svc.run_sql("SELECT * FROM generate_series(1, 1000) g", limit=7)
    assert r["row_count"] == 7 and r["truncated"] is True
    r = svc.run_sql("SELECT generate_series(1, 1000000000) AS g", limit=3)  # streamed with a cursor, not materialised
    assert r["row_count"] == 3


def test_run_sql_rejections(svc):
    for bad in ["DELETE FROM mt.cities", "SELECT pg_read_file('/etc/passwd')", "SELECT 1; SELECT 2", "SELECT * FROM pg_authid"]:
        with pytest.raises(GuardError):
            svc.run_sql(bad)
    with pytest.raises(MapsqlError, match="duplicate column"):
        svc.run_sql("SELECT geom, geom FROM mt.cities")


def test_run_sql_timeout_is_enforced(db_url, tmp_path):
    from mapsql_mcp.config import Settings
    from mapsql_mcp.service import GeoService

    s = GeoService(Settings(database_url=db_url, statement_timeout_ms=300, output_dir=tmp_path))
    try:
        with pytest.raises(MapsqlError, match="time limit"):
            s.run_sql("SELECT count(*) FROM generate_series(1, 100000000000)")
    finally:
        s.close()


def test_database_errors_are_friendly_and_redacted(svc):
    with pytest.raises(MapsqlError) as e:
        svc.run_sql("SELECT * FROM mt.no_such_table")
    assert "no_such_table" in str(e.value) and "postgresql://" not in str(e.value)
    with pytest.raises(MapsqlError, match="Database error"):
        svc.run_sql("SELECT 1/0")


def test_read_only_transaction_blocks_side_effects_that_pass_the_guard(svc):
    """`SELECT mt.sneaky()` is a plain SELECT for the parser; the READ ONLY transaction must stop the INSERT inside."""
    with pytest.raises(MapsqlError, match="read-only"):
        svc.run_sql("SELECT mt.sneaky() AS x")
    with psycopg.connect(TEST_URL) as c:
        assert c.execute("SELECT count(*) FROM mt.log").fetchone()[0] == 0


def test_session_state_does_not_leak_between_calls(svc):
    # statement_timeout is set with set_config(..., is_local=true): it must not stick to pooled connections
    for _ in range(6):
        svc.run_sql("SELECT 1 AS x")
    with svc.db.read_tx() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "15s"


# ------------------------------------------------------------------------ allowlists


def test_schema_allowlist_hides_other_schemas(restricted_svc):
    assert {l.schema for l in restricted_svc.catalog.layers()} == {"mt"}
    with pytest.raises(LayerNotFound):
        restricted_svc.find_features("mt_other.secret")
    with pytest.raises(LayerNotFound):
        restricted_svc.describe_layer("secret")
    for bad in [
        "SELECT * FROM mt_other.secret",
        "SELECT * FROM public.spatial_ref_sys",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_class",
        "SELECT * FROM secret",
    ]:
        with pytest.raises(GuardError):
            restricted_svc.run_sql(bad)
    # allowed schema still works, with and without qualification, PostGIS functions included
    assert restricted_svc.run_sql("SELECT count(*) AS n FROM cities")["rows"][0]["n"] == 6
    assert restricted_svc.run_sql("SELECT ST_AsText(geom) AS t FROM mt.cities WHERE id = 1")["rows"][0]["t"] == "POINT(0 0)"


def test_search_path_is_restricted_for_allowlisted_runs(restricted_svc):
    with restricted_svc.db.read_tx() as conn:
        sp = conn.execute("SHOW search_path").fetchone()[0]
    assert "mt_other" not in sp and "mt" in sp


def test_layer_allowlist(db_url, tmp_path):
    from mapsql_mcp.config import Settings
    from mapsql_mcp.service import GeoService

    s = GeoService(Settings(database_url=db_url, layers=("mt.cities", "mt.roads"), output_dir=tmp_path))
    try:
        assert {l.qualified for l in s.catalog.layers()} == {"mt.cities", "mt.roads"}
        with pytest.raises(LayerNotFound):
            s.find_features("mt.districts")
        with pytest.raises(GuardError):
            s.run_sql("SELECT * FROM mt.districts")
        assert s.run_sql("SELECT count(*) AS n FROM mt.roads")["rows"][0]["n"] == 2
        # cross-layer tools cannot reach hidden layers either
        with pytest.raises(LayerNotFound):
            s.spatial_filter("mt.cities", "within", other_layer="mt.districts")
    finally:
        s.close()


# ------------------------------------------------------------------------ server info / write mode


def test_server_info(svc):
    info = svc.server_info()
    assert info["mode"] == "read-only" and info["postgis"] and "password" not in json.dumps(info).lower()


def test_write_mode_is_off_by_default(svc):
    with pytest.raises(MapsqlError, match="disabled"):
        svc.execute_sql("INSERT INTO mt.log VALUES (1)")


def test_write_mode_when_enabled(db_url, tmp_path):
    from mapsql_mcp.config import Settings
    from mapsql_mcp.service import GeoService

    s = GeoService(Settings(database_url=db_url, allow_write=True, output_dir=tmp_path))
    try:
        assert s.execute_sql("INSERT INTO mt.log VALUES (42)")["rowcount"] == 1
        with psycopg.connect(TEST_URL) as c:
            assert c.execute("SELECT count(*) FROM mt.log WHERE n = 42").fetchone()[0] == 1
        with pytest.raises(GuardError):
            s.execute_sql("DELETE FROM mt.log")  # no WHERE
        with pytest.raises(GuardError):
            s.execute_sql("DROP TABLE mt.log")
        assert s.execute_sql("DELETE FROM mt.log WHERE n = 42")["rowcount"] == 1
        # read tools stay read-only even in write mode
        with pytest.raises(GuardError):
            s.run_sql("DELETE FROM mt.log WHERE n = 1")
    finally:
        s.close()


# ------------------------------------------------------------------------ large tables (index paths)


@pytest.fixture(scope="module")
def big_svc(db_url, tmp_path_factory):
    """60k random lon/lat points: exercises the GiST KNN pre-selection and the bbox prefilter."""
    from mapsql_mcp.config import Settings
    from mapsql_mcp.service import GeoService

    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS mt_big CASCADE")
        c.execute("CREATE SCHEMA mt_big")
        c.execute("SELECT setseed(0.42)")
        c.execute(
            "CREATE TABLE mt_big.pts AS SELECT i AS id, ST_SetSRID(ST_MakePoint(random()*2 - 1, random()*2 - 1), 4326)::geometry(Point,4326) AS geom "
            "FROM generate_series(1, 60000) i"
        )
        c.execute("CREATE INDEX ON mt_big.pts USING gist (geom)")
        c.execute("ANALYZE mt_big.pts")
    s = GeoService(Settings(database_url=db_url, schemas=("mt_big",), output_dir=tmp_path_factory.mktemp("big")))
    yield s
    s.close()
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS mt_big CASCADE")


def test_nearest_on_large_table_matches_exact_ranking(big_svc, db_url):
    layer = big_svc.catalog.resolve("mt_big.pts")
    assert (layer.row_estimate or 0) > 50_000
    r = big_svc.nearest("mt_big.pts", 5, lon=0.1, lat=0.2, columns=["id"])
    got = [f["properties"]["id"] for f in r["features"]]
    with psycopg.connect(db_url) as c:
        want = [
            row[0]
            for row in c.execute(
                "SELECT id FROM mt_big.pts ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(0.1, 0.2), 4326)::geography LIMIT 5"
            )
        ]
    assert got == want
    assert [f["properties"]["distance_m"] for f in r["features"]] == sorted(f["properties"]["distance_m"] for f in r["features"])


def test_within_distance_prefilter_on_large_table_is_exact(big_svc, db_url):
    r = big_svc.within_distance("mt_big.pts", 5000, lon=0.0, lat=0.0, columns=["id"], limit=5000)
    with psycopg.connect(db_url) as c:
        want = c.execute(
            "SELECT count(*) FROM mt_big.pts WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography, 5000)"
        ).fetchone()[0]
    assert r["count"] == want and want > 0
