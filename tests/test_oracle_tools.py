"""Oracle Spatial backend against the demo schema (scripts/oracle_demo_db.py), as the read-only user.

Skipped automatically without Oracle credentials (see tests/conftest.py). Expected numbers were cross-checked against the
PostGIS backend on the same pinned Natural Earth / OpenStreetMap snapshot.
"""

from __future__ import annotations

import math

import pytest

from mapsql_mcp.errors import GuardError, LayerNotFound, MapsqlError, NeedsClarification
from mapsql_mcp.layers import Filter
from mapsql_mcp.service import Metric

from .conftest import oracle_credentials, oracle_settings_from

pytestmark = pytest.mark.oracle

KIZILAY = {"lon": 32.8541, "lat": 39.9208}
PHARMACY = [Filter(column="kind", value="pharmacy")]
HOSPITAL = [Filter(column="kind", value="hospital")]
CAFE = [Filter(column="kind", value="cafe")]


def names(res: dict, key: str = "name") -> list[str]:
    return [f["properties"][key] for f in res["features"]]


def props(res: dict, key: str) -> list:
    return [f["properties"][key] for f in res["features"]]


# ---------------------------------------------------------------------------- discovery


def test_server_info(oracle_svc) -> None:
    info = oracle_svc.server_info()
    assert info["backend"] == "oracle" and info["mode"] == "read-only"
    assert info["oracle"] and info["oracle_spatial"] and info["layers"] >= 9
    assert "password" not in str(info).lower()


def test_list_layers_discovers_metadata(oracle_svc) -> None:
    res = oracle_svc.list_layers()
    by = {l["layer"]: l for l in res["layers"]}
    pois = by["mapsql_demo.ankara_pois"]
    assert pois["geometry_type"] == "POINT" and pois["srid"] == 4326 and pois["spatial_index"] is True
    assert pois["row_estimate"] == 2297 and "kind:varchar2(80)" in pois["columns"]
    assert by["mapsql_demo.ankara_neighbourhoods"]["geometry_type"] == "POLYGON"
    assert by["mapsql_demo.ankara_roads"]["geometry_type"] == "LINESTRING"
    assert by["mapsql_demo.world_rivers"]["geometry_type"].endswith("LINESTRING")
    tm30 = by["mapsql_demo.ankara_pois_tm30"]
    assert tm30["srid"] == 5254 and "TM30" in tm30["crs"]
    ext = pois["extent_wgs84"]
    assert 32.7 < ext[0] < 32.9 and 39.8 < ext[1] < 40.0
    assert [l["layer"] for l in oracle_svc.list_layers(search="neighbourhoods")["layers"]][0].endswith("neighbourhoods")


def test_describe_layer(oracle_svc) -> None:
    d = oracle_svc.describe_layer("ankara_pois")
    assert d["row_count"] == 2297 and d["geometry_types_in_sample"] == {"POINT": 2000}  # sample of 2000 rows
    assert d["top_values"]["kind"][0] == {"value": "pharmacy", "count": 329}
    assert d["numeric_stats"]["osm_id"]["min"] > 0
    t = oracle_svc.describe_layer("mapsql_demo.ankara_pois_tm30", include_value_samples=False)
    assert t["srid"] == 5254 and t["crs_kind"] == "projected (metres)"
    assert 32.7 < t["extent_wgs84"][0] < 32.9  # reported in WGS84 although the layer is in TM30 metres


def test_layer_resolution(oracle_svc) -> None:
    assert oracle_svc.catalog.resolve("ANKARA_POIS").table == "ANKARA_POIS"
    assert oracle_svc.catalog.resolve("mapsql_demo.ankara_pois").qualified == "mapsql_demo.ankara_pois"
    assert oracle_svc.catalog.resolve('"MAPSQL_DEMO"."ANKARA_POIS"').table == "ANKARA_POIS"
    with pytest.raises(LayerNotFound, match="Did you mean"):
        oracle_svc.catalog.resolve("ankara_poi")


# ---------------------------------------------------------------------------- retrieval


def test_find_features_filters(oracle_svc) -> None:
    r = oracle_svc.find_features("ankara_pois", PHARMACY, columns=["name", "kind"], limit=5)
    assert r["count"] == 5 and r["truncated"] and r["total_matches"] == 329
    assert set(props(r, "kind")) == {"pharmacy"} and r["features"][0]["geometry"]["type"] == "Point"
    r = oracle_svc.find_features("ankara_pois", [Filter(column="name", op="contains", value="ECZANE")], limit=3)
    assert r["total_matches"] >= 100  # case-insensitive substring
    r = oracle_svc.find_features("ankara_pois", [Filter(column="kind", op="in", value=["bar", "pub"])], columns=["kind"], limit=100)
    assert set(props(r, "kind")) <= {"bar", "pub"}
    r = oracle_svc.find_features("world_cities", order_by="pop_max", descending=True, columns=["name", "pop_max"], limit=2)
    assert props(r, "pop_max") == sorted(props(r, "pop_max"), reverse=True)
    r = oracle_svc.find_features("ankara_pois", [Filter(column="osm_id", op="between", value=[1, 10**12]), Filter(column="cuisine", op="not_null")], limit=2)
    assert r["count"] == 2


def test_filter_values_are_bound_not_spliced(oracle_svc) -> None:
    for hostile in ("O'Brien; DROP TABLE ankara_pois", "x' OR '1'='1", "\" OR 1=1 --", "%' ESCAPE '\\"):
        r = oracle_svc.find_features("ankara_pois", [Filter(column="name", value=hostile)], limit=5)
        assert r["count"] == 0
    assert oracle_svc.find_features("ankara_pois", limit=1)["count"] == 1  # still there


def test_filter_validation(oracle_svc) -> None:
    with pytest.raises(MapsqlError, match="numeric"):
        oracle_svc.find_features("ankara_pois", [Filter(column="osm_id", value="abc")])
    with pytest.raises(MapsqlError, match="geometry column"):
        oracle_svc.find_features("ankara_pois", [Filter(column="geom", value="x")])
    with pytest.raises(MapsqlError, match="no column"):
        oracle_svc.find_features("ankara_pois", [Filter(column="nope", value="x")])


def test_sample_features(oracle_svc) -> None:
    r = oracle_svc.sample_features("world_countries", 3)
    assert r["count"] == 3 and all(f["geometry"] for f in r["features"])


# ---------------------------------------------------------------------------- distance / neighbours


def test_within_distance_matches_postgis(oracle_svc) -> None:
    r = oracle_svc.within_distance("ankara_pois", 500, filters=PHARMACY, columns=["name"], limit=200, **KIZILAY)
    assert r["count"] == 20  # PostGIS (geography) finds the same 20
    d = props(r, "distance_m")
    assert d == sorted(d) and max(d) <= 500
    assert names(r)[0] == "Botanica" and abs(d[0] - 109.6) <= 0.5
    assert r["reference"].startswith("point") and r["distance_m"] == 500.0


def test_within_distance_projected_layer_is_metre_correct(oracle_svc) -> None:
    geo = oracle_svc.within_distance("ankara_pois", 500, filters=PHARMACY, columns=["name"], limit=200, **KIZILAY)
    prj = oracle_svc.within_distance("ankara_pois_tm30", 500, filters=PHARMACY, columns=["name"], limit=200, **KIZILAY)
    assert set(names(prj)) == set(names(geo)) and prj["count"] == 20
    # TM30 distances are planar in the projection: scale distortion near the central meridian is ~1e-3 at most
    for a, b in zip(props(geo, "distance_m"), props(prj, "distance_m")):
        assert abs(a - b) < 1.5


def test_within_distance_reference_feature_and_asks(oracle_svc) -> None:
    park = oracle_svc.find_features("ankara_parks", [Filter(column="name", op="ilike", value="%park%")], limit=1, columns=["name"])
    pname = names(park)[0]
    r = oracle_svc.within_distance("ankara_pois", 200, reference_layer="ankara_parks",
                                   reference_filters=[Filter(column="name", value=pname)], columns=["name"], limit=500)
    assert r["count"] > 0 and r["reference"].endswith("of mapsql_demo.ankara_parks")
    with pytest.raises(NeedsClarification, match="near WHAT"):
        oracle_svc.within_distance("ankara_pois", 200)
    with pytest.raises(NeedsClarification, match="which one is meant"):
        oracle_svc.within_distance("ankara_pois", 200, reference_layer="ankara_parks",
                                   reference_filters=[Filter(column="name", op="contains", value="park")])
    with pytest.raises(NeedsClarification, match="both"):
        oracle_svc.within_distance("ankara_pois", 200, reference_layer="ankara_parks", reference_filters=PHARMACY, **KIZILAY)
    with pytest.raises(MapsqlError, match="swapped"):
        oracle_svc.within_distance("ankara_pois", 200, lon=32.0, lat=120.0)


def test_nearest(oracle_svc) -> None:
    r = oracle_svc.nearest("ankara_pois", 5, filters=HOSPITAL, columns=["name"], **KIZILAY)
    assert r["count"] == 5
    d = props(r, "distance_m")
    assert d == sorted(d) and abs(d[0] - 795.3) < 1
    assert names(r)[0].startswith("Onkoloji")
    r = oracle_svc.nearest("ankara_pois", 3, columns=["name"], **KIZILAY)  # plain SDO_NN
    assert r["count"] == 3 and abs(props(r, "distance_m")[0] - 31.9) < 0.5
    r = oracle_svc.nearest("ankara_pois", 50, max_distance_m=100, columns=["name"], **KIZILAY)
    assert 0 < r["count"] < 50 and max(props(r, "distance_m")) <= 100
    r = oracle_svc.nearest("ankara_pois_tm30", 5, filters=HOSPITAL, columns=["name"], **KIZILAY)
    assert abs(props(r, "distance_m")[0] - 795.9) < 1.5


# ---------------------------------------------------------------------------- geometry tools


def test_buffer_area_is_geodesic(oracle_svc) -> None:
    r = oracle_svc.buffer(500, **KIZILAY)
    area = r["features"][0]["properties"]["area_km2"]
    assert abs(area - math.pi * 0.25) / (math.pi * 0.25) < 0.01
    assert r["features"][0]["geometry"]["type"] == "Polygon"
    r = oracle_svc.buffer(300, "ankara_pois", HOSPITAL)
    assert r["features"][0]["properties"]["n_source_features"] == 31 and r["features"][0]["properties"]["area_km2"] > 0
    r = oracle_svc.buffer(100, "ankara_pois_tm30", HOSPITAL, dissolve=False, limit=2)  # projected: arcs are densified
    assert r["count"] == 2 and abs(props(r, "area_km2")[0] - math.pi * 0.01) < 0.002
    with pytest.raises(NeedsClarification):
        oracle_svc.buffer(100)


def test_bbox_of(oracle_svc) -> None:
    b = oracle_svc.bbox_of("ankara_pois")
    assert b["count"] == 2297 and b["crs"] == "EPSG:4326"
    assert abs(b["bbox"][0] - 32.809842) < 1e-4 and abs(b["bbox"][3] - 39.961093) < 1e-4
    t = oracle_svc.bbox_of("ankara_neighbourhoods_tm30")  # TM30 metres in, WGS84 out
    assert 32.7 < t["bbox"][0] < 32.8 and 39.8 < t["bbox"][1] < 39.9
    assert oracle_svc.bbox_of("ankara_pois", [Filter(column="kind", value="nothing")])["bbox"] is None


def test_spatial_filter(oracle_svc) -> None:
    r = oracle_svc.spatial_filter("ankara_pois", "intersects", bbox=[32.85, 39.92, 32.86, 39.93], columns=["name"], limit=1000)
    assert r["count"] == 196
    poly = {"type": "Polygon", "coordinates": [[[32.85, 39.92], [32.85, 39.93], [32.86, 39.93], [32.86, 39.92], [32.85, 39.92]]]}  # clockwise
    r2 = oracle_svc.spatial_filter("ankara_pois", "within", geojson_geometry=poly, columns=["name"], limit=1000)
    assert r2["count"] == 196
    kiz = [Filter(column="name", value="Kızılay Mahallesi")]
    r = oracle_svc.spatial_filter("ankara_pois", "within", "ankara_neighbourhoods", kiz, filters=PHARMACY, columns=["name"], limit=100)
    assert r["count"] == 10
    dis = oracle_svc.spatial_filter("ankara_pois", "disjoint", "ankara_neighbourhoods", kiz, columns=["name"], limit=5000)
    assert dis["count"] + oracle_svc.spatial_filter("ankara_pois", "intersects", "ankara_neighbourhoods", kiz, limit=5000)["count"] == 2297
    with pytest.raises(NeedsClarification, match="WITH WHAT"):
        oracle_svc.spatial_filter("ankara_pois", "within")
    with pytest.raises(NeedsClarification, match="exactly one"):
        oracle_svc.spatial_filter("ankara_pois", "within", bbox=[0, 0, 1, 1], geojson_geometry=poly)
    with pytest.raises(MapsqlError, match="crosses"):
        oracle_svc.spatial_filter("ankara_roads", "crosses", "ankara_parks", [Filter(column="name", value="x")])


# ---------------------------------------------------------------------------- analysis


def test_aggregate(oracle_svc) -> None:
    r = oracle_svc.aggregate("ankara_pois", group_by=["kind"], filters=[Filter(column="category", value="amenity")], limit=3)
    assert [(x["kind"], x["count"]) for x in r["rows"]] == [("pharmacy", 329), ("cafe", 276), ("restaurant", 238)]
    r = oracle_svc.aggregate("ankara_parks", metrics=[Metric(fn="area_km2"), Metric(fn="count")])
    assert r["rows"][0]["count"] == 231 and abs(r["rows"][0]["area_km2"] - 3.64) < 0.02
    r = oracle_svc.aggregate("world_countries", metrics=[Metric(fn="area_km2"), Metric(fn="sum", column="pop_est")],
                             filters=[Filter(column="name", value="Turkey")])
    assert abs(r["rows"][0]["area_km2"] - 780476) < 200  # geodesic km2 (PostGIS: 780476.04)
    r = oracle_svc.aggregate("ankara_roads", metrics=[Metric(fn="length_km")], group_by=["highway"], limit=1)
    assert abs(r["rows"][0]["length_km"] - 471.7) < 0.5
    with pytest.raises(MapsqlError, match="polygon"):
        oracle_svc.aggregate("ankara_roads", metrics=[Metric(fn="area_km2")])
    with pytest.raises(MapsqlError, match="output columns"):
        oracle_svc.aggregate("ankara_pois", group_by=["kind"], order_by="zzz")


def test_spatial_join_choropleth_counts(oracle_svc) -> None:
    r = oracle_svc.spatial_join("ankara_neighbourhoods", "ankara_pois", "contains", right_filters=CAFE, limit=200, geometry="none")
    counts = {f["properties"]["name"]: f["properties"]["right_count"] for f in r["features"]}
    assert len(counts) == 105 and sum(counts.values()) == 276  # same as PostGIS, hood by hood
    assert counts["Yukarı Bahçelievler Mahallesi"] == 45
    assert props(r, "right_count") == sorted(props(r, "right_count"), reverse=True)
    # projected left layer x geographic right layer and vice versa: coordinates are transformed
    for left, right in (("ankara_neighbourhoods_tm30", "ankara_pois"), ("ankara_neighbourhoods", "ankara_pois_tm30")):
        r2 = oracle_svc.spatial_join(left, right, "contains" if right == "ankara_pois" else "intersects", right_filters=CAFE,
                                     limit=200, geometry="none")
        assert sum(props(r2, "right_count")) == 276
    r = oracle_svc.spatial_join("ankara_neighbourhoods", "ankara_pois", "contains", right_filters=CAFE, min_count=20,
                                order_desc=False, geometry="full", limit=10)
    assert props(r, "right_count") == sorted(props(r, "right_count")) and min(props(r, "right_count")) >= 20
    assert r["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_spatial_join_modes(oracle_svc) -> None:
    r = oracle_svc.spatial_join("world_countries", "world_cities", "contains", mode="sum", right_column="pop_max",
                                left_filters=[Filter(column="name", value="Turkey")], geometry="none")
    assert r["count"] == 1 and props(r, "right_sum_pop_max")[0] > 5_000_000
    r = oracle_svc.spatial_join("ankara_neighbourhoods", "ankara_pois", "contains", mode="attach", right_columns=["name"],
                                right_filters=HOSPITAL, limit=500, geometry="none")
    assert r["count"] > 0 and all("right_name" in f["properties"] for f in r["features"])
    r = oracle_svc.spatial_join("ankara_parks", "ankara_pois", "dwithin", 300, right_filters=PHARMACY, limit=300, geometry="none")
    assert r["count"] == 231 and abs(sum(props(r, "right_count")) - 756) <= 10  # PostGIS: 756 +- boundary cases
    with pytest.raises(NeedsClarification, match="distance_m"):
        oracle_svc.spatial_join("ankara_parks", "ankara_pois", "dwithin")
    with pytest.raises(NeedsClarification, match="right_column"):
        oracle_svc.spatial_join("ankara_parks", "ankara_pois", "contains", mode="sum")


# ---------------------------------------------------------------------------- run_sql and safety


def test_run_sql_returns_geojson_in_wgs84(oracle_svc) -> None:
    r = oracle_svc.run_sql("SELECT name, geom FROM mapsql_demo.ankara_pois_tm30 WHERE kind = 'hospital' FETCH FIRST 2 ROWS ONLY")
    assert r["row_count"] == 2 and r["geometry_column"] == "geom"
    lon, lat = r["features"][0]["geometry"]["coordinates"]
    assert 32.7 < lon < 32.95 and 39.8 < lat < 40.0  # TM30 metres were transformed to lon/lat
    r = oracle_svc.run_sql("SELECT kind, COUNT(*) AS n FROM mapsql_demo.ankara_pois GROUP BY kind ORDER BY n DESC FETCH FIRST 2 ROWS ONLY")
    assert r["rows"][0] == {"kind": "pharmacy", "n": 329}
    r = oracle_svc.run_sql("SELECT name, SDO_GEOM.SDO_DISTANCE(geom, SDO_GEOMETRY(2001, 4326, SDO_POINT_TYPE(32.8541, 39.9208, NULL), NULL, NULL), "
                           "0.05, 'unit=M') AS d FROM mapsql_demo.ankara_pois WHERE ROWNUM <= 3", geometry="none")
    assert r["row_count"] == 3
    r = oracle_svc.run_sql("SELECT name FROM mapsql_demo.ankara_pois", limit=7)
    assert r["row_count"] == 7 and r["truncated"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM mapsql_demo.ankara_pois",
        "INSERT INTO mapsql_demo.ankara_pois (name) VALUES ('x')",
        "UPDATE mapsql_demo.ankara_pois SET name = 'x'",
        "DROP TABLE mapsql_demo.ankara_pois",
        "SELECT * FROM mapsql_demo.ankara_pois@some_link",
        "SELECT DBMS_RANDOM.VALUE FROM dual",
        "SELECT UTL_HTTP.REQUEST('http://example.invalid') FROM dual",
        "SELECT * FROM mapsql_demo.ankara_pois FOR UPDATE",
        "SELECT 1 FROM dual; DROP TABLE mapsql_demo.ankara_pois",
        "BEGIN NULL; END;",
        "SELECT * FROM dba_users",
        "SELECT * FROM sys.user$",
    ],
)
def test_run_sql_refuses(oracle_svc, sql) -> None:
    with pytest.raises(GuardError):
        oracle_svc.run_sql(sql)


def test_run_sql_database_errors_are_friendly(oracle_svc) -> None:
    with pytest.raises(MapsqlError, match="does not exist"):
        oracle_svc.run_sql("SELECT * FROM mapsql_demo.no_such_table")
    with pytest.raises(MapsqlError, match="Database error: ORA-"):
        oracle_svc.run_sql("SELECT no_such_column FROM mapsql_demo.ankara_pois")


def test_write_mode_is_not_available(oracle_svc) -> None:
    with pytest.raises(MapsqlError, match="not available"):
        oracle_svc.execute_sql("DELETE FROM mapsql_demo.ankara_pois WHERE 1=0")


def test_statement_timeout(oracle_creds, tmp_path) -> None:
    from mapsql_mcp.backends.oracle import OracleService

    svc = OracleService(oracle_settings_from(oracle_creds, tmp_path, statement_timeout_ms=1500))
    try:
        with pytest.raises(MapsqlError, match="time limit"):
            svc.run_sql("SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c")
    finally:
        svc.close()


def test_row_cap(oracle_creds, tmp_path) -> None:
    from mapsql_mcp.backends.oracle import OracleService

    svc = OracleService(oracle_settings_from(oracle_creds, tmp_path, max_rows=25, default_limit=25))
    try:
        r = svc.find_features("ankara_pois", limit=10_000)
        assert r["count"] == 25 and r["truncated"] and r["total_matches"] == 2297
    finally:
        svc.close()


def test_transaction_is_read_only_even_for_a_user_who_can_write(oracle_creds, tmp_path) -> None:
    """Defence in depth: the demo OWNER may write, but the server's transactions are READ ONLY (ORA-01456)."""
    import oracledb

    owner = oracle_credentials("oracle_demo")
    if owner is None:
        pytest.skip("no write-capable demo user configured ([oracle_demo] in oracle-test.ini)")
    from mapsql_mcp.backends.oracle_db import OracleDatabase

    db = OracleDatabase(oracle_settings_from(owner, tmp_path))
    try:
        with pytest.raises(MapsqlError, match="read-only"):
            with db.read_tx() as conn:
                conn.cursor().execute("INSERT INTO ankara_pois (name) VALUES ('should not be written')")
        assert db.query("SELECT COUNT(*) FROM ankara_pois WHERE name = 'should not be written'").rows[0][0] == 0
    finally:
        db.close()
    assert oracledb  # imported for the skip-on-missing-driver side effect


def test_read_only_user_has_no_write_privileges(oracle_creds, tmp_path) -> None:
    import oracledb

    from mapsql_mcp.backends.oracle_db import OracleDatabase, resolve_connection

    p = resolve_connection(oracle_settings_from(oracle_creds, tmp_path))
    conn = oracledb.connect(user=p.user, password=p.password, dsn=p.dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT privilege FROM session_privs")
        assert {r[0] for r in cur.fetchall()} == {"CREATE SESSION"}
        with pytest.raises(oracledb.DatabaseError):
            cur.execute("DELETE FROM mapsql_demo.ankara_pois")
    finally:
        conn.close()
    assert OracleDatabase  # imported above for type reference only


# ---------------------------------------------------------------------------- allowlists


def test_schema_and_layer_allowlists(oracle_creds, tmp_path) -> None:
    from mapsql_mcp.backends.oracle import OracleService

    svc = OracleService(oracle_settings_from(oracle_creds, tmp_path, schemas=("MAPSQL_DEMO",), layers=("mapsql_demo.ankara_*",)))
    try:
        listed = {l["layer"] for l in svc.list_layers()["layers"]}
        assert listed and all(n.startswith("mapsql_demo.ankara_") for n in listed)
        assert svc.server_info()["allowed_layers"] == ["mapsql_demo.ankara_*"]
        with pytest.raises(LayerNotFound):
            svc.find_features("world_cities")
        with pytest.raises(GuardError, match="allowlist"):
            svc.run_sql("SELECT * FROM mapsql_demo.world_cities")
        with pytest.raises(GuardError, match="qualify|allowlist"):
            svc.run_sql("SELECT * FROM all_tab_columns")
        assert svc.run_sql("SELECT COUNT(*) AS n FROM mapsql_demo.ankara_pois")["rows"][0]["n"] == 2297
        assert svc.run_sql("SELECT 1 AS one FROM dual")["rows"][0]["one"] == 1
    finally:
        svc.close()


def test_unqualified_names_resolve_for_the_owner_schema(oracle_creds, tmp_path) -> None:
    owner = oracle_credentials("oracle_demo")
    if owner is None:
        pytest.skip("no [oracle_demo] credentials")
    from mapsql_mcp.backends.oracle import OracleService

    svc = OracleService(oracle_settings_from(owner, tmp_path, layers=("mapsql_demo.ankara_pois",)))
    try:
        assert svc.run_sql("SELECT COUNT(*) AS n FROM ankara_pois")["rows"][0]["n"] == 2297
        with pytest.raises(GuardError):
            svc.run_sql("SELECT COUNT(*) AS n FROM world_cities")
    finally:
        svc.close()


def test_connection_errors_do_not_leak_credentials(oracle_creds, tmp_path) -> None:
    from mapsql_mcp.backends.oracle import OracleService

    secret = "Wr0ngPassw0rd-Leak-Check"
    bad = dict(oracle_creds)
    if "url" in bad:
        pytest.skip("only checked for the DSN form")
    bad.update(user="NO_SUCH_USER_FOR_MAPSQL_TEST", password=secret)
    svc = OracleService(oracle_settings_from(bad, tmp_path))
    try:
        with pytest.raises(MapsqlError) as e:
            svc.server_info()
        msg = str(e.value)
        assert secret not in msg and "oraclecloud" not in msg.lower() and "(DESCRIPTION" not in msg.upper()
    finally:
        svc.close()
