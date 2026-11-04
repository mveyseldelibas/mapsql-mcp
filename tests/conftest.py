"""Shared fixtures.

DB tests need a PostGIS database. They create (and recreate) their own schemas `mt`/`mt_other`
inside the database given by MAPSQL_TEST_DATABASE_URL (default: the local dev cluster,
postgresql://postgres@127.0.0.1:55432/mapsql_test) and never touch anything else.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from mapsql_mcp.config import Settings
from mapsql_mcp.service import GeoService

TEST_URL = os.environ.get("MAPSQL_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/mapsql_test")

FIXTURE_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;
DROP SCHEMA IF EXISTS mt CASCADE;
DROP SCHEMA IF EXISTS mt_other CASCADE;
CREATE SCHEMA mt;
CREATE SCHEMA mt_other;

-- lon/lat points on the equator: 0.01 deg of longitude ~ 1113.2 m
CREATE TABLE mt.cities (id int PRIMARY KEY, name text, country text, pop int, capital boolean, geom geometry(Point, 4326));
INSERT INTO mt.cities VALUES
 (1, 'Alpha',   'Aland', 1000, true,  ST_SetSRID(ST_MakePoint(0.00, 0.0), 4326)),
 (2, 'Bravo',   'Aland', 2000, false, ST_SetSRID(ST_MakePoint(0.01, 0.0), 4326)),
 (3, 'Charlie', 'Bland', 3000, false, ST_SetSRID(ST_MakePoint(0.02, 0.0), 4326)),
 (4, 'Delta',   'Bland', 4000, true,  ST_SetSRID(ST_MakePoint(1.0, 1.0), 4326)),
 (5, 'Echo',    'Cland', 500,  false, ST_SetSRID(ST_MakePoint(10.0, 10.0), 4326)),
 (6, 'O''Brien; DROP TABLE x', 'Cland', NULL, false, ST_SetSRID(ST_MakePoint(10.1, 10.1), 4326));
CREATE INDEX ON mt.cities USING gist (geom);

CREATE TABLE mt.districts (id int PRIMARY KEY, name text, geom geometry(Polygon, 4326));
INSERT INTO mt.districts VALUES
 (1, 'West', ST_MakeEnvelope(-0.005, -0.005, 0.015, 0.005, 4326)),
 (2, 'East', ST_MakeEnvelope(0.015, -0.005, 0.03, 0.005, 4326)),
 (3, 'Far',  ST_MakeEnvelope(9.9, 9.9, 10.2, 10.2, 4326));

CREATE TABLE mt.roads (id int PRIMARY KEY, name text, kind text, geom geometry(LineString, 4326));
INSERT INTO mt.roads VALUES
 (1, 'Main St', 'primary', ST_GeomFromText('LINESTRING(0 0, 0.02 0)', 4326)),
 (2, 'Side St', 'residential', ST_GeomFromText('LINESTRING(0.01 0, 0.01 0.01)', 4326));

-- projected, metre-based CRS (UTM 36N): coordinates are metres
CREATE TABLE mt.sensors (id int PRIMARY KEY, label text, reading numeric, geom geometry(Point, 32636));
INSERT INTO mt.sensors VALUES
 (1, 's1', 1.5, ST_SetSRID(ST_MakePoint(500000, 0), 32636)),
 (2, 's2', 2.5, ST_SetSRID(ST_MakePoint(500500, 0), 32636)),
 (3, 's3', 3.5, ST_SetSRID(ST_MakePoint(501000, 0), 32636)),
 (4, 's4', 4.5, ST_SetSRID(ST_MakePoint(503000, 0), 32636));

-- projected CRS in US survey feet (EPSG:2263 NY Long Island)
CREATE TABLE mt.parcels_ft (id int PRIMARY KEY, geom geometry(Point, 2263));
INSERT INTO mt.parcels_ft
 SELECT i, ST_Transform(ST_SetSRID(ST_MakePoint(-74.0 + i * 0.001, 40.7), 4326), 2263) FROM generate_series(0, 5) i;

CREATE TABLE mt.geog_pts (id int PRIMARY KEY, name text, geog geography(Point, 4326));
INSERT INTO mt.geog_pts VALUES
 (1, 'g1', ST_SetSRID(ST_MakePoint(0.0, 0.0), 4326)::geography),
 (2, 'g2', ST_SetSRID(ST_MakePoint(0.005, 0.0), 4326)::geography),
 (3, 'g3', ST_SetSRID(ST_MakePoint(0.5, 0.0), 4326)::geography);

-- a view with an untyped geometry column (SRID unknown to geometry_columns)
CREATE VIEW mt.v_untyped AS SELECT id, name, ST_Buffer(geom, 0.001) AS geom FROM mt.cities;

-- side-effect function: the READ ONLY transaction must stop it even though it passes the SQL guard
CREATE TABLE mt.log (n int);
CREATE FUNCTION mt.sneaky() RETURNS int LANGUAGE sql VOLATILE AS $$ INSERT INTO mt.log VALUES (1) RETURNING n $$;

CREATE TABLE mt_other.secret (id int, token text, geom geometry(Point, 4326));
INSERT INTO mt_other.secret VALUES (1, 'top-secret', ST_SetSRID(ST_MakePoint(0, 0), 4326));

ANALYZE;
"""


@pytest.fixture(scope="session")
def db_url() -> str:
    try:
        with psycopg.connect(TEST_URL, connect_timeout=3, autocommit=True) as conn:
            conn.execute(FIXTURE_SQL)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostGIS test database not available ({type(e).__name__}); set MAPSQL_TEST_DATABASE_URL")
    return TEST_URL


@pytest.fixture(scope="session")
def settings(db_url, tmp_path_factory) -> Settings:
    return Settings(database_url=db_url, output_dir=tmp_path_factory.mktemp("maps"), basemap="none")


@pytest.fixture(scope="session")
def svc(settings):
    s = GeoService(settings)
    yield s
    s.close()


@pytest.fixture()
def restricted_svc(db_url, tmp_path):
    s = GeoService(Settings(database_url=db_url, schemas=("mt",), output_dir=tmp_path, basemap="none"))
    yield s
    s.close()


def names(result: dict) -> list[str]:
    return [f["properties"]["name"] for f in result["features"]]


# ---------------------------------------------------------------------------- Oracle (optional)
#
# Oracle tests run against the demo schema built by scripts/oracle_demo_db.py, as the read-only user. Credentials:
#   * env MAPSQL_TEST_ORACLE_URL=oracle://MAPSQL_RO:pw@host:1522/service?protocol=tcps   (or
#     MAPSQL_TEST_ORACLE_DSN + MAPSQL_TEST_ORACLE_USER + MAPSQL_TEST_ORACLE_PASSWORD), or
#   * the git-ignored oracle-test.ini ([oracle_ro]; [oracle_demo] enables the "READ ONLY transaction" test).
# Without them every Oracle test is skipped.

import configparser  # noqa: E402
from pathlib import Path  # noqa: E402

ORACLE_INI = Path(os.environ.get("MAPSQL_TEST_ORACLE_INI") or Path(__file__).resolve().parent.parent / "oracle-test.ini")


def oracle_credentials(section: str = "oracle_ro") -> dict | None:
    env = os.environ
    if section == "oracle_ro":
        if env.get("MAPSQL_TEST_ORACLE_URL"):
            return {"url": env["MAPSQL_TEST_ORACLE_URL"]}
        if env.get("MAPSQL_TEST_ORACLE_DSN") and env.get("MAPSQL_TEST_ORACLE_USER") and env.get("MAPSQL_TEST_ORACLE_PASSWORD"):
            return {"dsn": env["MAPSQL_TEST_ORACLE_DSN"], "user": env["MAPSQL_TEST_ORACLE_USER"],
                    "password": env["MAPSQL_TEST_ORACLE_PASSWORD"]}
    elif env.get("MAPSQL_TEST_ORACLE_RW_URL"):
        return {"url": env["MAPSQL_TEST_ORACLE_RW_URL"]}
    if ORACLE_INI.exists():
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(ORACLE_INI, encoding="utf-8")
        if section in cp and all(k in cp[section] for k in ("user", "password", "dsn")):
            s = cp[section]
            return {"dsn": s["dsn"], "user": s["user"], "password": s["password"]}
    return None


def oracle_settings_from(creds: dict, tmp_path, **kw) -> Settings:
    base = dict(output_dir=tmp_path, basemap="none", backend="oracle")
    if "url" in creds:
        base["database_url"] = creds["url"]
    else:
        base.update(oracle_dsn=creds["dsn"], oracle_user=creds["user"], oracle_password=creds["password"])
    base.update(kw)
    s = Settings(**base)
    s.register_secrets()
    return s


def oracle_env(creds: dict) -> dict[str, str]:
    """Environment for a spawned server process (password only goes through the environment)."""
    if "url" in creds:
        return {"DATABASE_URL": creds["url"]}
    return {"MAPSQL_ORACLE_DSN": creds["dsn"], "MAPSQL_ORACLE_USER": creds["user"], "MAPSQL_ORACLE_PASSWORD": creds["password"]}


@pytest.fixture(scope="session")
def oracle_creds() -> dict:
    pytest.importorskip("oracledb")
    pytest.importorskip("sqlglot")
    c = oracle_credentials()
    if c is None:
        pytest.skip("no Oracle test credentials (oracle-test.ini [oracle_ro] or MAPSQL_TEST_ORACLE_URL)")
    return c


@pytest.fixture(scope="session")
def oracle_svc(oracle_creds, tmp_path_factory):
    from mapsql_mcp.backends.oracle import OracleService

    s = OracleService(oracle_settings_from(oracle_creds, tmp_path_factory.mktemp("ora_maps")))
    try:
        layers = {l.qualified for l in s.catalog.layers()}
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Oracle test database not reachable ({type(e).__name__})")
    if "mapsql_demo.ankara_pois" not in layers:
        pytest.skip("Oracle demo schema missing: run python scripts/oracle_demo_db.py")
    yield s
    s.close()
