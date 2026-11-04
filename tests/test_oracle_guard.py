"""The Oracle SQL guard: everything dangerous must be rejected, everything useful accepted. No database needed."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from mapsql_mcp.backends.oracle_guard import OracleKnown, validate_oracle_sql  # noqa: E402
from mapsql_mcp.config import Settings  # noqa: E402
from mapsql_mcp.errors import GuardError  # noqa: E402

OK = [
    "SELECT 1 FROM dual",
    "select 1 from dual;",
    "SELECT * FROM mapsql_demo.ankara_pois",
    "SELECT name, kind FROM ankara_pois WHERE kind = 'pharmacy' FETCH FIRST 5 ROWS ONLY",
    "SELECT * FROM t WHERE ROWNUM <= 5",
    "WITH a AS (SELECT 1 x FROM dual) SELECT * FROM a",
    "SELECT * FROM a UNION ALL SELECT * FROM b",
    "SELECT * FROM a MINUS SELECT * FROM b",
    "(SELECT 1 FROM dual)",
    "SELECT /*+ INDEX(t i) */ * FROM t",
    "SELECT t.geom.SDO_SRID FROM t t",
    "SELECT SDO_GEOM.SDO_AREA(geom, 0.05, 'unit=SQ_KM') FROM mapsql_demo.ankara_parks",
    "SELECT MDSYS.SDO_GEOM.SDO_AREA(geom, 0.05) FROM mapsql_demo.ankara_parks",
    "SELECT name FROM mapsql_demo.ankara_pois p WHERE SDO_WITHIN_DISTANCE(p.geom, SDO_GEOMETRY(2001, 4326, "
    "SDO_POINT_TYPE(32.85, 39.92, NULL), NULL, NULL), 'distance=500 unit=M') = 'TRUE'",
    "SELECT name FROM mapsql_demo.ankara_pois p WHERE SDO_NN(p.geom, SDO_GEOMETRY(2001, 4326, "
    "SDO_POINT_TYPE(32.85, 39.92, NULL), NULL, NULL), 'sdo_num_res=5 unit=M', 1) = 'TRUE'",
    "SELECT SDO_UTIL.TO_GEOJSON(geom) FROM mapsql_demo.ankara_pois",
    "SELECT * FROM t1, t2 WHERE t1.a = t2.a(+)",
    "SELECT * FROM t START WITH a = 1 CONNECT BY PRIOR a = b",
    "SELECT * FROM t AS OF TIMESTAMP SYSTIMESTAMP",
    "SELECT * FROM (SELECT 1 a FROM dual) CROSS JOIN LATERAL (SELECT 2 b FROM dual)",
    "SELECT 'a;b' FROM dual",
    "SELECT 'DROP TABLE x' FROM dual",
    "SELECT 'x@y.com' FROM dual",
    "SELECT 'DBMS_LOCK.SLEEP(5)' AS s FROM dual",
    "SELECT q'[it's; DROP TABLE x]' FROM dual",
    "SELECT 1 FROM dual -- harmless trailing comment",
    "SELECT /* a ; DROP TABLE x */ 1 FROM dual",
    "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER') FROM dual",
    "SELECT name FROM ankara_pois WHERE name = :name",
    "SELECT * FROM sys.dual",
    "SELECT srid, cs_name FROM mdsys.cs_srs WHERE srid = 5254",
    "SELECT * FROM all_tab_columns WHERE table_name = 'ANKARA_POIS'",
    'SELECT "NAME" FROM "MAPSQL_DEMO"."ANKARA_POIS"',
]


@pytest.mark.parametrize("sql", OK)
def test_accepts_plain_selects(sql: str) -> None:
    assert validate_oracle_sql(sql).kind == "select"


DENIED = {
    # statement classes
    "insert": "INSERT INTO t VALUES (1)",
    "insert select": "INSERT INTO t SELECT * FROM s",
    "update": "UPDATE t SET a = 1",
    "delete": "DELETE FROM t",
    "merge": "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET a = 1",
    "create table": "CREATE TABLE x (a NUMBER)",
    "create table as": "CREATE TABLE x AS SELECT * FROM t",
    "create view": "CREATE VIEW v AS SELECT 1 a FROM dual",
    "drop": "DROP TABLE t",
    "truncate": "TRUNCATE TABLE t",
    "alter table": "ALTER TABLE t ADD (b NUMBER)",
    "alter session": "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY'",
    "alter system": "ALTER SYSTEM KILL SESSION '1,2'",
    "grant": "GRANT SELECT ON t TO PUBLIC",
    "revoke": "REVOKE SELECT ON t FROM u",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
    "set transaction": "SET TRANSACTION READ WRITE",
    "lock table": "LOCK TABLE t IN EXCLUSIVE MODE",
    "flashback": "FLASHBACK TABLE t TO BEFORE DROP",
    "call": "CALL my_proc()",
    "explain": "EXPLAIN PLAN FOR SELECT 1 FROM dual",
    # PL/SQL and dynamic SQL
    "anonymous block": "BEGIN NULL; END;",
    "declare block": "DECLARE x NUMBER; BEGIN x := 1; END;",
    "execute immediate": "EXECUTE IMMEDIATE 'DROP TABLE t'",
    "exec": "EXEC dbms_output.put_line('x')",
    "block after select": "SELECT 1 FROM dual; BEGIN NULL; END;",
    "with function": "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM dual",
    "with procedure": "WITH PROCEDURE p IS BEGIN NULL; END; SELECT 1 FROM dual",
    # multiple statements / comment tricks
    "two selects": "SELECT 1 FROM dual; SELECT 2 FROM dual",
    "select then drop": "SELECT 1 FROM dual; DROP TABLE t",
    "select then drop newline": "SELECT 1 FROM dual;\nDROP TABLE t",
    "drop after comment": "SELECT 1 FROM dual; -- hi\nDROP TABLE t",
    "drop after block comment": "SELECT 1 FROM dual;/* x */DROP TABLE t",
    "sqlplus slash": "SELECT 1 FROM dual\n/\nDROP TABLE t",
    "sqlplus slash only": "/",
    "backslash tail": "SELECT 'a\\' FROM dual; DROP TABLE t --'",
    "select into": "SELECT * INTO v FROM t",
    "bulk collect": "SELECT a BULK COLLECT INTO v FROM t",
    # locking
    "for update": "SELECT * FROM t FOR UPDATE",
    "for update nowait": "SELECT * FROM t FOR UPDATE NOWAIT",
    "for update skip locked": "SELECT * FROM t FOR UPDATE OF a SKIP LOCKED",
    "for update in subquery": "SELECT * FROM (SELECT * FROM t FOR UPDATE)",
    # database links
    "dblink table": "SELECT * FROM t@remote",
    "dblink with schema": "SELECT * FROM scott.t@remote",
    "dblink domain": "SELECT * FROM t@remote.example.com",
    "dblink spaced": "SELECT * FROM t @ remote",
    "dblink in subquery": "SELECT 1 FROM dual WHERE x IN (SELECT y FROM t@lnk)",
    "dblink in cte": "WITH a AS (SELECT * FROM t@lnk) SELECT * FROM a",
    "dblink quoted": 'SELECT * FROM "T"@"LNK"',
    "sqlplus at": "@script.sql",
    # dangerous packages
    "dbms_lock": "SELECT DBMS_LOCK.SLEEP(5) FROM dual",
    "dbms_lock sys": "SELECT SYS.DBMS_LOCK.SLEEP(5) FROM dual",
    "dbms_lock lower": "select dbms_lock.sleep(5) from dual",
    "dbms_lock quoted": 'SELECT "DBMS_LOCK".SLEEP(5) FROM dual',
    "dbms_lock spaces": "SELECT DBMS_LOCK . SLEEP ( 5 ) FROM dual",
    "dbms_lock comment": "SELECT DBMS_LOCK/**/.SLEEP(5) FROM dual",
    "dbms_xplan table": "SELECT * FROM TABLE(dbms_xplan.display)",
    "dbms_random": "SELECT DBMS_RANDOM.VALUE FROM dual",
    "dbms_xmlgen": "SELECT DBMS_XMLGEN.GETXML('select * from t') FROM dual",
    "dbms_scheduler": "SELECT DBMS_SCHEDULER.CREATE_JOB('x') FROM dual",
    "dbms_java": "SELECT DBMS_JAVA.RUNJAVA('x') FROM dual",
    "dbms_sql": "SELECT DBMS_SQL.OPEN_CURSOR FROM dual",
    "utl_http": "SELECT UTL_HTTP.REQUEST('http://evil') FROM dual",
    "utl_inaddr": "SELECT UTL_INADDR.GET_HOST_ADDRESS('evil.example.com') FROM dual",
    "utl_file": "SELECT UTL_FILE.FOPEN('D','f','r') FROM dual",
    "utl_tcp": "SELECT UTL_TCP.OPEN_CONNECTION('h', 1) FROM dual",
    "utl_smtp": "SELECT UTL_SMTP.OPEN_CONNECTION('h') FROM dual",
    "ctx_doc": "SELECT CTX_DOC.FILTER('a') FROM dual",
    "ctx_ddl": "SELECT CTX_DDL.CREATE_PREFERENCE('a','b') FROM dual",
    "dbms in where": "SELECT 1 FROM dual WHERE DBMS_LOB.GETLENGTH(NULL) IS NULL",
    "dbms in subquery": "SELECT * FROM (SELECT UTL_HTTP.REQUEST('x') r FROM dual)",
    "dbms in column alias": 'SELECT 1 AS "DBMS_X" FROM dual',
    "apex": "SELECT APEX_WEB_SERVICE.MAKE_REST_REQUEST('x') FROM dual",
    # url / xml functions
    "httpuritype": "SELECT HTTPURITYPE('http://evil').getclob() FROM dual",
    "dburitype": "SELECT DBURITYPE('/PUBLIC/T').getclob() FROM dual",
    "xmltype": "SELECT XMLTYPE('<a/>').getclobval() FROM dual",
    "xmlquery": "SELECT XMLQUERY('1' RETURNING CONTENT) FROM dual",
    "extractvalue": "SELECT EXTRACTVALUE(x, '/a') FROM t",
    "sys_dburigen": "SELECT SYS_DBURIGEN(a) FROM t",
    "sys_xmlgen": "SELECT SYS_XMLGEN(a) FROM t",
    # sequences
    "nextval": "SELECT my_seq.NEXTVAL FROM dual",
    "currval": "SELECT s.CURRVAL FROM dual",
    "nextval schema": "SELECT scott.s.nextval FROM dual",
    # system objects
    "sys.user$": "SELECT * FROM sys.user$",
    "sys.link$": "SELECT * FROM SYS.LINK$",
    "user$ unqualified": "SELECT * FROM user$",
    "dba_users": "SELECT * FROM dba_users",
    "dba_db_links": "SELECT * FROM dba_db_links",
    "user_db_links": "SELECT * FROM user_db_links",
    "all_source": "SELECT * FROM all_source",
    "v$session": "SELECT * FROM v$session",
    "v$ qualified": "SELECT * FROM sys.v_$session",
    "gv$": "SELECT * FROM gv$sql",
    "x$": "SELECT * FROM x$ksppi",
    "system schema": "SELECT * FROM system.help",
    "xdb schema": "SELECT * FROM xdb.xdb$resource",
    "ctxsys": "SELECT * FROM ctxsys.ctx_user_indexes",
    "mdsys internals": "SELECT * FROM mdsys.wfs_featuretype",
    "sys in subquery": "SELECT * FROM (SELECT * FROM sys.obj$)",
    "sys quoted": 'SELECT * FROM "SYS"."USER$"',
    # parse failures are rejections
    "garbage": "SELEC FRM",
    "unterminated string": "SELECT 'abc FROM dual",
    "empty": "",
    "whitespace": "   ",
    "comment only": "-- nothing",
    "nul byte": "SELECT 1 FROM dual\x00",
    "too long": "SELECT '" + "a" * 21000 + "' FROM dual",
}


@pytest.mark.parametrize("sql", list(DENIED.values()), ids=list(DENIED))
def test_rejects_dangerous_sql(sql: str) -> None:
    with pytest.raises(GuardError):
        validate_oracle_sql(sql)


def test_error_messages_are_helpful() -> None:
    with pytest.raises(GuardError, match="Only SELECT"):
        validate_oracle_sql("DELETE FROM t")
    with pytest.raises(GuardError, match="Multiple statements"):
        validate_oracle_sql("SELECT 1 FROM dual; SELECT 2 FROM dual")
    with pytest.raises(GuardError, match="Database links"):
        validate_oracle_sql("SELECT * FROM t@x")
    with pytest.raises(GuardError, match="FOR UPDATE"):
        validate_oracle_sql("SELECT * FROM t FOR UPDATE")
    with pytest.raises(GuardError, match="DBMS"):
        validate_oracle_sql("SELECT DBMS_LOCK.SLEEP(1) FROM dual")


# ---------------------------------------------------------------------------- allowlists

KNOWN = OracleKnown(
    current_schema="MAPSQL_RO",
    objects={"mapsql_ro": {"mine", "secret_mine"}, "mapsql_demo": {"ankara_pois", "world_cities", "other"}},
    synonyms={"pois": ("mapsql_demo", "ankara_pois"), "evil": ("mapsql_demo", "other")},
)


def test_schema_allowlist() -> None:
    s = Settings(schemas=("MAPSQL_DEMO",))
    validate_oracle_sql("SELECT * FROM mapsql_demo.ankara_pois", s, known=KNOWN)
    validate_oracle_sql('SELECT * FROM "MAPSQL_DEMO"."ANKARA_POIS"', s, known=KNOWN)
    with pytest.raises(GuardError, match="allowlist"):
        validate_oracle_sql("SELECT * FROM other_schema.t", s, known=KNOWN)
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM mapsql_demo.ankara_pois p JOIN hr.employees e ON 1=1", s, known=KNOWN)
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM (SELECT * FROM hr.employees)", s, known=KNOWN)


def test_layer_allowlist() -> None:
    s = Settings(layers=("mapsql_demo.ankara_*", "mapsql_demo.world_cities"))
    validate_oracle_sql("SELECT * FROM mapsql_demo.ankara_pois", s, known=KNOWN)
    validate_oracle_sql("SELECT * FROM mapsql_demo.world_cities", s, known=KNOWN)
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM mapsql_demo.other", s, known=KNOWN)


def test_unqualified_names_are_resolved_against_the_account() -> None:
    s = Settings(schemas=("MAPSQL_RO",))
    validate_oracle_sql("SELECT * FROM mine", s, known=KNOWN)  # own table, schema allowed
    s2 = Settings(layers=("mapsql_ro.mine",))
    validate_oracle_sql("SELECT * FROM mine", s2, known=KNOWN)
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM secret_mine", s2, known=KNOWN)
    # a private synonym is followed to its target
    s3 = Settings(layers=("mapsql_demo.ankara_pois",))
    validate_oracle_sql("SELECT * FROM pois", s3, known=KNOWN)
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM evil", s3, known=KNOWN)


def test_unknown_unqualified_names_are_refused_when_restricted() -> None:
    s = Settings(schemas=("MAPSQL_DEMO",))
    with pytest.raises(GuardError, match="qualify"):
        validate_oracle_sql("SELECT * FROM all_tab_columns", s, known=KNOWN)  # PUBLIC synonym of a dictionary view
    with pytest.raises(GuardError, match="qualify"):
        validate_oracle_sql("SELECT * FROM something", s)  # no account knowledge at all
    validate_oracle_sql("SELECT 1 FROM dual", s, known=KNOWN)
    validate_oracle_sql("WITH x AS (SELECT 1 a FROM dual) SELECT * FROM x", s, known=KNOWN)


def test_cte_cannot_hide_a_forbidden_table() -> None:
    s = Settings(layers=("mapsql_ro.mine",))
    with pytest.raises(GuardError):
        validate_oracle_sql("WITH secret_mine AS (SELECT 1 a FROM dual) SELECT * FROM secret_mine", s, known=KNOWN)


def test_unrestricted_default_allows_dictionary_views_but_not_dba() -> None:
    validate_oracle_sql("SELECT * FROM all_tables")
    with pytest.raises(GuardError):
        validate_oracle_sql("SELECT * FROM dba_tables")
