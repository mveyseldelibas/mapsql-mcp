-- A least-privilege Oracle user for mapsql-mcp: the Oracle counterpart of scripts/readonly_role.sql.
-- Run as an administrator (e.g. ADMIN on Autonomous Database, or a DBA on 19c/21c/23ai). Adapt names and tables.
--   sqlplus admin@yourdb @scripts/oracle_readonly_user.sql
-- Then:  DATABASE_URL=oracle://MAPSQL_RO:<password>@host:1522/service_name?protocol=tcps
--
-- Why this is enough: python-oracledb connects with CREATE SESSION only; the spatial metadata views
-- (ALL_SDO_GEOM_METADATA, ALL_SDO_INDEX_INFO), the data dictionary (ALL_TAB_COLUMNS, ALL_INDEXES ...), MDSYS.CS_SRS and
-- the SDO_* operators/functions are readable/executable through PUBLIC. Object privileges are the ONLY thing that
-- decides what the assistant can see, so grant SELECT table by table (never SELECT ANY TABLE, never a role such as DBA).

-- 1. the user (choose a strong password; quote it)
CREATE USER mapsql_ro IDENTIFIED BY "change-me-Str0ng-passw0rd";   -- no QUOTA: the user cannot store anything
GRANT CREATE SESSION TO mapsql_ro;

-- 2. optional hard limits that hold even if the application guard were bypassed (not available on every edition /
--    Autonomous profile; ask your DBA). They are the Oracle equivalent of ALTER ROLE ... SET statement_timeout.
-- CREATE PROFILE mapsql_ro_profile LIMIT
--   SESSIONS_PER_USER 5 CPU_PER_CALL 3000 LOGICAL_READS_PER_CALL 10000000 IDLE_TIME 10 CONNECT_TIME 60;
-- ALTER USER mapsql_ro PROFILE mapsql_ro_profile;
-- On Autonomous Database use the service level (LOW/MEDIUM) and a Resource Manager plan instead.

-- 3. expose only what the assistant may see: one GRANT per table or view (repeat, or generate them)
GRANT SELECT ON gis.parcels        TO mapsql_ro;
GRANT SELECT ON gis.roads          TO mapsql_ro;
GRANT SELECT ON gis.neighbourhoods TO mapsql_ro;

-- Generate the grants for every table of a schema:
-- BEGIN
--   FOR t IN (SELECT table_name FROM all_tables WHERE owner = 'GIS') LOOP
--     EXECUTE IMMEDIATE 'GRANT SELECT ON gis.' || t.table_name || ' TO mapsql_ro';
--   END LOOP;
-- END;
-- /

-- 4. check what the user really has (connect as mapsql_ro):
--    SELECT * FROM session_privs;          -- expect exactly: CREATE SESSION
--    SELECT * FROM session_roles;          -- expect: no rows
--    SELECT owner, table_name FROM all_tab_privs WHERE grantee = 'MAPSQL_RO';

-- The demo schema built by scripts/oracle_demo_db.py does exactly this for the tables of MAPSQL_DEMO.
