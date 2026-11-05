# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.2.0] - 2026-11-05

Oracle Spatial support.

### Added
- **Oracle Spatial backend** (`pip install mapsql-mcp[oracle]`, `python-oracledb` thin mode - no Oracle Client): all 14 tools
  (`list_layers` ... `render_map`) on `SDO_GEOMETRY` data. Metre-correct distances/areas on geodetic (EPSG:4326 / 8307) and projected
  layers (verified with TUREF TM30, EPSG:5254) through `unit=M` / `SQ_KM`; `SDO_WITHIN_DISTANCE`, `SDO_NN`, `SDO_RELATE`
  masks, `SDO_GEOM.SDO_BUFFER`, `SDO_AGGR_MBR`, `SDO_AGGR_UNION`, `SDO_CS.TRANSFORM`, `SDO_UTIL.TO_GEOJSON`; index-free
  `SDO_GEOM.*` fallback for layers without a spatial index (flagged in responses).
- Layer discovery from `ALL_SDO_GEOM_METADATA` plus unregistered `SDO_GEOMETRY` columns, SRID, `SDO_GTYPE` sampling, spatial
  index validity, CRS kind/unit from `MDSYS`; `--schemas` / `--layers` allowlists work with Oracle owners.
- **Oracle SQL guard** (`sqlglot` Oracle dialect + independent token pass): single `SELECT`/`WITH` only; blocks DML/DDL/`MERGE`,
  PL/SQL, `EXECUTE IMMEDIATE`, `FOR UPDATE`, database links, `DBMS_*`/`UTL_*`/`CTX_*`, dictionary secrets, sequences, URL/XML
  functions; allowlist enforcement incl. unqualified names and synonyms; 150+ test cases.
- Oracle execution safety: `SET TRANSACTION READ ONLY` on every transaction, `call_timeout`, row caps, bind variables everywhere,
  redaction of Oracle DSNs, TNS descriptors and passwords.
- Connection: `DATABASE_URL=oracle://USER:PASS@host:port/service?protocol=tcps` or `MAPSQL_ORACLE_DSN` (+ `_USER`, `_PASSWORD`,
  optional wallet); `MAPSQL_BACKEND` / `--backend`.
- Backend layer (`mapsql_mcp.backends`): `BackendSpec`/`SpatialService` contract, `postgis` and `oracle` implementations; server
  instructions and tool descriptions adapt to the dialect.
- `scripts/oracle_demo_db.py` (re-runnable demo schema `MAPSQL_DEMO` + read-only `MAPSQL_RO`, TM30 copies),
  `scripts/oracle_readonly_user.sql`, `scripts/make_oracle_demo_maps.py`, `docs/images/oracle_*.png`.
- Tests: Oracle guard (148 cases, no database), config/redaction, database-backed tool tests and an MCP stdio end-to-end test
  (skipped without credentials).

### Changed
- `Settings` keeps secrets out of `repr`; `server_info` reports the `backend`; the `Database`/`GeoService` PostGIS code is unchanged.

- `--open-maps` / `MAPSQL_OPEN_MAPS=1`: open every rendered map in the desktop's default viewer, for MCP clients that do not
  display tool images to the user.

### Fixed
- Unknown tool arguments are now rejected with the list of allowed parameters instead of being silently dropped. A model
  that invented `where=` on `nearest` used to get an unfiltered (plausible-looking but wrong) answer.

### Known limits (Oracle)
- Read-only only (`--allow-write` is refused); `crosses` is not offered; GeoJSON conversion cannot handle circular arcs;
  extents are skipped for layers over 500 000 rows; see the README section *Differences from PostGIS*.

## [0.1.0] - 2026-11-02 (unreleased)

First public version.

### Added
- MCP server (stdio and streamable HTTP) built on the official `mcp` Python SDK.
- Safety layers: `pglast`-based SQL guard (single `SELECT` only, function/relation deny-lists), `READ ONLY` transactions
  with `statement_timeout`, server-side cursors with row caps, schema/layer allowlists, secret redaction, opt-in write
  mode (`--allow-write`).
- Spatial tools: `list_layers`, `describe_layer`, `sample_features`, `find_features`, `within_distance`, `nearest`,
  `buffer`, `spatial_filter`, `aggregate`, `spatial_join`, `bbox_of`, `run_sql`, `server_info`, `render_map`.
- Metric-correct distances/areas for geographic, metre-projected and non-metre-projected CRSs and `geography` columns.
- `render_map`: PNG (matplotlib + OpenStreetMap basemap with attribution and tile cache), labels, choropleth, and
  standalone Leaflet HTML export.
- "Ask, don't guess": server instructions, prompts (`gis_assistant`, `spatial_question`, `explore_database`) and tool-level
  refusals with candidate lists for missing or ambiguous inputs.
- Demo database builder (Natural Earth + OpenStreetMap central Ankara, pinned offline snapshot).
- `mapsql-bench`: 60 questions with SQL-derived gold answers, an oracle replay and an OpenAI-compatible agent runner.
- Test-suite (guard: 540+ cases; tools against a PostGIS fixture; rendering; MCP stdio end-to-end).
