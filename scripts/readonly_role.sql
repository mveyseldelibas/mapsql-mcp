-- A least-privilege role for mapsql-mcp. Run as a superuser / database owner, adapt names and schemas.
--   psql -d gis -v pw="'change-me'" -f scripts/readonly_role.sql
-- Then:  DATABASE_URL=postgresql://mapsql_ro:change-me@host:5432/gis

CREATE ROLE mapsql_ro LOGIN PASSWORD :pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;

-- hard limits that hold even if the application guard were bypassed
ALTER ROLE mapsql_ro SET statement_timeout = '15s';
ALTER ROLE mapsql_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE mapsql_ro SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE gis TO mapsql_ro;

-- PostGIS functions and spatial_ref_sys / geometry_columns live here (usually public)
GRANT USAGE ON SCHEMA public TO mapsql_ro;
GRANT SELECT ON public.spatial_ref_sys, public.geometry_columns, public.geography_columns TO mapsql_ro;

-- expose only what the assistant may see (repeat per schema)
GRANT USAGE ON SCHEMA osm TO mapsql_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA osm TO mapsql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA osm GRANT SELECT ON TABLES TO mapsql_ro;
