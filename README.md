# mapsql-mcp

<!-- mcp-name: io.github.mveyseldelibas/mapsql-mcp -->

**Ask your PostGIS or Oracle Spatial database questions in plain English and get the answer back as a map.**

`mapsql-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI assistants
(Claude Desktop, Claude Code, Cursor, and any other MCP client) explore, query and analyse a **PostGIS** or an
**Oracle Spatial** (`SDO_GEOMETRY`) database *safely* and answer with **maps rendered right in the chat**.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-server-purple)
![status](https://img.shields.io/badge/status-v0.2%20alpha-orange)
![PostGIS](https://img.shields.io/badge/PostGIS-supported-336791)
![Oracle Spatial](https://img.shields.io/badge/Oracle%20Spatial-supported-c74634)

![Pharmacies within 500 m of Kizilay](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/pharmacies_near_kizilay.png)

> *"Which pharmacies are within 500 m of Kızılay square?"* - the model discovers the layers, calls
> `within_distance` (geodesic metres, whatever the table's SRID), then `render_map`. The picture above is the
> literal output of those two tool calls on the bundled demo database.

![mapsql-mcp demo: question, tool calls, map](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/demo.gif)

*The GIF is built by `scripts/make_demo_video.py` from real tool outputs on the demo databases (PostGIS and Oracle).*

**Türkçe:** [kullanım kılavuzu aşağıda](#türkçe) · **Who is it for / step-by-step:** [Usage](#usage-step-by-step)

## Why another PostGIS / Postgres / Oracle MCP server?

Generic Postgres or Oracle MCP servers are not spatial-aware: the model has to hand-write PostGIS / `SDO_*` SQL and gets
SRIDs, `geography` casts, units and `ST_DWithin` / `SDO_WITHIN_DISTANCE` arguments wrong in subtle ways, then cannot show
the result. `mapsql-mcp` is built
around three ideas:

1. **Safe by construction.** Every query is parsed (PostgreSQL: the real PostgreSQL parser; Oracle: `sqlglot` in
   Oracle mode), runs in a `READ ONLY` transaction with a timeout and a row cap, and secrets never reach the model. Writes
   are off unless you ask for them (PostGIS only).
2. **Spatial tools instead of SQL guessing.** `within_distance`, `nearest`, `buffer`, `spatial_filter`, `spatial_join`,
   `aggregate`, `bbox_of` ... take care of SRIDs, metric units and indexes - the same 14 tools on both databases. Plain
   `run_sql` is still there.
3. **Answers are maps.** `render_map` returns a PNG (OpenStreetMap basemap, choropleths, labels) as MCP image
   content and can also write an interactive Leaflet HTML file.

And the model is told to **ask instead of guess**: the server instructions, a prompt and the tools themselves refuse to
proceed on "pharmacies nearby" (*nearby what?*) or on a place name that matches ten features.

## Quickstart

Requires Python 3.10+ and a PostGIS **or** Oracle Spatial database. **Use a read-only database role / user** (see
[SECURITY.md](SECURITY.md)). The backend is chosen from the connection string.

```bash
pip install mapsql-mcp            # or: pipx install mapsql-mcp   |   uvx mapsql-mcp
export DATABASE_URL=postgresql://readonly:secret@localhost:5432/gis
mapsql-mcp --check                # prints PostgreSQL/PostGIS versions and exits

# Oracle Spatial instead (python-oracledb thin mode: no Oracle Client install needed) - see "Oracle Spatial" below
pip install "mapsql-mcp[oracle]"
export DATABASE_URL="oracle://MAPSQL_RO:secret@dbhost:1521/ORCLPDB1"
mapsql-mcp --check
```

### Claude Desktop

Edit `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "postgis": {
      "command": "uvx",
      "args": ["mapsql-mcp"],
      "env": { "DATABASE_URL": "postgresql://readonly:secret@localhost:5432/gis" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add postgis -e DATABASE_URL=postgresql://readonly:secret@localhost:5432/gis -- uvx mapsql-mcp
```

### Cursor (`.cursor/mcp.json`) / Windsurf

```json
{
  "mcpServers": {
    "postgis": {
      "command": "uvx",
      "args": ["mapsql-mcp", "--schemas", "public,osm"],
      "env": { "DATABASE_URL": "postgresql://readonly:secret@localhost:5432/gis" }
    }
  }
}
```

### VS Code (`.vscode/mcp.json`, Copilot Chat in Agent mode)

VS Code uses a `servers` key and can prompt for the password instead of storing it in the file:

```json
{
  "inputs": [
    { "id": "pg-url", "type": "promptString", "description": "PostGIS connection URL", "password": true }
  ],
  "servers": {
    "postgis": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mapsql-mcp", "--schemas", "public,osm"],
      "env": { "DATABASE_URL": "${input:pg-url}" }
    }
  }
}
```

### Other MCP clients (ChatGPT-compatible, remote)

Run the streamable-HTTP transport and point the client at `http://127.0.0.1:8000/mcp`:

```bash
mapsql-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

There is **no authentication** on the HTTP transport: keep it on loopback or put it behind your own authenticating
reverse proxy / tunnel. If you installed from a clone use `"command": "python", "args": ["-m", "mapsql_mcp"]`.

## Usage, step by step

**Who it is for.** GIS developers, database people, analysts and researchers who already use an AI assistant (Claude Code,
Claude Desktop, Cursor, VS Code + Copilot) and have spatial data in PostGIS or Oracle Spatial. It is a building block for
an assistant, not an end-user app: there is no web UI of its own, the chat window *is* the UI.

**1. Install:**

```bash
pipx install "mapsql-mcp[oracle]"     # or: pip install "mapsql-mcp[oracle]"
```

**2. Point it at a database with a read-only user** and check the connection:

```bash
export DATABASE_URL=postgresql://readonly:secret@localhost:5432/gis      # or oracle://USER:PASS@host:1521/service
mapsql-mcp --check
```

No database at hand? Build the bundled demo (Natural Earth + OpenStreetMap Ankara) in two minutes, see the next section.

**3. Register it with your assistant.** Put one of the JSON blocks from *Quickstart* into the client's config
(`.mcp.json` for Claude Code, `.vscode/mcp.json` for VS Code, `.cursor/mcp.json` for Cursor, `claude_desktop_config.json`
for Claude Desktop). In VS Code click **Start** above the server entry; in Claude Code run `/mcp` and check it says
*connected*. You should see 14 tools.

**4. Ask in plain language.** For example:

* *"What layers do you have? Summarise them."*
* *"Which pharmacies are within 500 m of Kızılay (39.9208, 32.8541)? Show them on a map."*
* *"Which neighbourhood has the most cafes? Colour the neighbourhoods by count."*
* *"The 5 hospitals nearest to this point, with distances."*
* *"Find the pharmacies nearby."* - a good assistant asks *nearby what?* instead of guessing.

**5. See the map.** `render_map` returns the PNG to the assistant and saves it (and optionally an interactive Leaflet HTML)
under `~/mapsql-mcp-output`. Some clients (e.g. the Claude Code panel in VS Code) do not show tool images to you; add
`MAPSQL_OPEN_MAPS=1` (or `--open-maps`) to the server's `env` and every map opens in your default viewer.

**Good to know.** The assistant's provider (Anthropic, OpenAI, ...) receives the rows the tools return, so do not connect
databases whose content must not leave your organisation; use `--schemas` / `--layers` to expose only what is needed, and
keep the database user read-only.

## Try it with the demo database (2 minutes)

The demo data is Natural Earth (world countries, provinces, cities, rivers, lakes - public domain) plus an OpenStreetMap
extract of central Ankara (POIs, roads, parks, 105 neighbourhoods - ODbL). A pinned snapshot ships in `demo/data` (2.8 MB),
so building it works offline.

```bash
docker run -d --name pg -p 55432:5432 -e POSTGRES_PASSWORD=pw postgis/postgis:16-3.4
pip install -e .
python demo/build_demo_db.py --database-url postgresql://postgres:pw@localhost:55432/postgres
export DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres
```

Then ask your assistant, for example:

* *"What layers do you have? Summarise the Ankara data."*
* *"Which neighbourhood has the most pharmacies? Show it as a map."*
* *"Cafes within 300 m of Kızılay (lat 39.9208, lon 32.8541), on a map."*
* *"Find the pharmacies nearby."* → it should **ask** where.

| Cafes per neighbourhood (`spatial_join` + choropleth) | 5 nearest hospitals (`nearest`) |
|---|---|
| ![cafes](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/cafes_per_neighbourhood.png) | ![hospitals](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/nearest_hospitals.png) |

*Map data © OpenStreetMap contributors (ODbL).* Interactive versions of two of them: `docs/images/*.html` (open in a browser).

## Oracle Spatial

New in v0.2: the same tools, the same safety model and the same maps on **Oracle Database with Spatial** (19c, 21c, 23ai /
26ai, Autonomous Database, ...), through `python-oracledb` in *thin* mode (no Oracle Client libraries needed).

![Pharmacies within 500 m of Kizilay, Oracle Spatial](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/oracle_pharmacies_near_kizilay.png)

*Same question, same answer (20 pharmacies) as on PostGIS - this time from `SDO_WITHIN_DISTANCE(... 'distance=500 unit=M')`
on an Autonomous Database. Map data (c) OpenStreetMap contributors (ODbL).*

### Install and connect

```bash
pip install "mapsql-mcp[oracle]"        # adds oracledb + sqlglot
```

| Variable | Meaning |
|---|---|
| `DATABASE_URL=oracle://USER:PASS@host:1521/service_name` | Easy form. Percent-encode special characters in the password (`@` -> `%40`). Add `?protocol=tcps` (or `?tls=true`) for TLS; other `?key=value` pairs are passed on as Easy Connect parameters (e.g. `ssl_server_dn_match=yes`). |
| `MAPSQL_ORACLE_DSN` + `MAPSQL_ORACLE_USER` + `MAPSQL_ORACLE_PASSWORD` | For **long TNS descriptors**, `tcps://host:1522/service` strings or `tnsnames.ora` aliases (with `TNS_ADMIN`). The DSN overrides the host part of an `oracle://` URL; user/password given here override those in the URL. Keeping the password out of the URL avoids quoting problems. |
| `MAPSQL_ORACLE_WALLET_DIR`, `MAPSQL_ORACLE_WALLET_PASSWORD` | Only for mutual-TLS (wallet) connections, e.g. Autonomous Database with "mTLS required". |
| `MAPSQL_BACKEND=postgis\|oracle` / `--backend` | Force the backend. Default: detected from the `DATABASE_URL` scheme, or Oracle when only `MAPSQL_ORACLE_DSN` is set. |

All other settings (`MAPSQL_SCHEMAS`, `MAPSQL_LAYERS`, `MAPSQL_STATEMENT_TIMEOUT_MS`, `MAPSQL_MAX_ROWS`, ...) work unchanged;
the timeout is applied as the connection `call_timeout`, schemas are Oracle *owners*.

**Autonomous Database (TLS, no wallet).** In the console open *Database connection*, choose *TLS authentication: TLS* (instead
of mTLS; requires an access control list / allowed IPs for your address) and copy the `Low`/`Medium` connection string. The
descriptor looks like `(description=(address=(protocol=tcps)(port=1522)(host=...))(connect_data=(service_name=...))(security=(ssl_server_dn_match=yes)))`;
put it in `MAPSQL_ORACLE_DSN` (it is long and contains parentheses, so it does not fit well in a URL). The server certificate is
verified against the operating system's trust store. Use a dedicated read-only user, never `ADMIN`.

```bash
export MAPSQL_ORACLE_DSN='(description=(retry_count=3)(address=(protocol=tcps)(port=1522)(host=adb.<region>.oraclecloud.com))(connect_data=(service_name=xxxx_mydb_low.adb.oraclecloud.com))(security=(ssl_server_dn_match=yes)))'
export MAPSQL_ORACLE_USER=MAPSQL_RO MAPSQL_ORACLE_PASSWORD='...'
mapsql-mcp --check
```

### Read-only user

[`scripts/oracle_readonly_user.sql`](scripts/oracle_readonly_user.sql) is the Oracle counterpart of `readonly_role.sql`: a user with
only `CREATE SESSION` plus `SELECT` on the tables the assistant may see. The spatial metadata views, `MDSYS.CS_SRS` and the
`SDO_*` operators are available to every user, so nothing else is needed.

### Claude Desktop / Claude Code / Cursor / VS Code

```json
{
  "mcpServers": {
    "oracle-spatial": {
      "command": "uvx",
      "args": ["--from", "mapsql-mcp[oracle]", "mapsql-mcp", "--schemas", "GIS"],
      "env": { "DATABASE_URL": "oracle://MAPSQL_RO:secret@dbhost:1521/ORCLPDB1" }
    }
  }
}
```

```bash
claude mcp add oracle-spatial -e DATABASE_URL=oracle://MAPSQL_RO:secret@dbhost:1521/ORCLPDB1 -- uvx --from "mapsql-mcp[oracle]" mapsql-mcp --schemas GIS
```

VS Code (`.vscode/mcp.json`) - the password is prompted for and never stored in the file; a TNS descriptor goes into `MAPSQL_ORACLE_DSN`:

```json
{
  "inputs": [
    { "id": "ora-pw", "type": "promptString", "description": "Oracle password for MAPSQL_RO", "password": true }
  ],
  "servers": {
    "oracle-spatial": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "mapsql-mcp[oracle]", "mapsql-mcp", "--schemas", "GIS", "--no-basemap"],
      "env": {
        "MAPSQL_ORACLE_DSN": "dbhost:1521/ORCLPDB1",
        "MAPSQL_ORACLE_USER": "MAPSQL_RO",
        "MAPSQL_ORACLE_PASSWORD": "${input:ora-pw}"
      }
    }
  }
}
```

### Oracle demo schema (3 minutes)

`scripts/oracle_demo_db.py` loads the same Natural Earth + OpenStreetMap-Ankara data as the PostGIS demo into the schema
`MAPSQL_DEMO` (`WORLD_COUNTRIES`, `WORLD_CITIES`, `ANKARA_POIS`, `ANKARA_NEIGHBOURHOODS`, ... - Oracle has no schemas-within-a-database
convention like `world.countries`, so the PostGIS schema becomes a table-name prefix), registers `USER_SDO_GEOM_METADATA`, builds spatial indexes,
adds **TUREF TM30 (EPSG:5254, metres)** copies of two Ankara layers to exercise projected coordinate systems, and creates the
read-only user `MAPSQL_RO`. It needs an administrator login in a git-ignored ini file:

```ini
; oracle-test.ini
[oracle]
user = ADMIN
password = ...
dsn = (description=...)    ; or host:1521/service
```

```bash
python scripts/oracle_demo_db.py          # creates MAPSQL_DEMO + MAPSQL_RO (random passwords are written to the same ini file)
python scripts/make_oracle_demo_maps.py --ini oracle-test.ini     # the maps in docs/images/oracle_*.png
```

| Cafes per neighbourhood (`spatial_join` + choropleth) | Projected layer: TUREF TM30, SRID 5254 |
|---|---|
| ![cafes](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/oracle_cafes_per_neighbourhood.png) | ![tm30](https://raw.githubusercontent.com/mveyseldelibas/mapsql-mcp/main/docs/images/oracle_tm30_pharmacies.png) |

### How the tools map to Oracle Spatial

| Tool | Oracle implementation |
|---|---|
| `list_layers` / `describe_layer` | `ALL_SDO_GEOM_METADATA` (+ unregistered `SDO_GEOMETRY` columns of accessible tables/views), SRID, tolerance, geometry type from `SDO_GTYPE`, spatial-index presence/validity from `ALL_INDEXES`, CRS kind/name from `MDSYS.SDO_COORD_REF_SYS` / `CS_SRS` |
| `within_distance` | `SDO_WITHIN_DISTANCE(geom, ref, 'distance=N unit=M')`, distance via `SDO_GEOM.SDO_DISTANCE(..., 'unit=M')` |
| `nearest` | `SDO_NN` + `SDO_NN_DISTANCE` (`sdo_batch_size` when attribute filters are present, so the filter is applied while neighbours stream out) |
| `buffer` | `SDO_GEOM.SDO_BUFFER(..., 'unit=M arc_tolerance=...')`, dissolve with `SDO_AGGR_UNION`, area with `SDO_AREA(..., 'unit=SQ_KM')` |
| `spatial_filter` / `spatial_join` | `SDO_RELATE` masks (`ANYINTERACT`, `INSIDE+COVEREDBY+EQUAL`, `CONTAINS+COVERS+EQUAL`, `TOUCH`, `OVERLAPBDYDISJOINT+OVERLAPBDYINTERSECT`), `SDO_WITHIN_DISTANCE` for `dwithin`; joins run as a set-based spatial join grouped by `ROWID` |
| `bbox_of` | `SDO_AGGR_MBR` over `SDO_CS.TRANSFORM(geom, 4326)` |
| `aggregate` | `SDO_GEOM.SDO_AREA(... 'unit=SQ_KM')` / `SDO_LENGTH(... 'unit=KM')` |
| GeoJSON | `SDO_UTIL.TO_GEOJSON` on geometries transformed with `SDO_CS.TRANSFORM(geom, 4326)`; simplified/rounded in Python |
| `run_sql` | guarded Oracle SQL; every `SDO_GEOMETRY` column is returned as GeoJSON (WGS84) |

Metres are right on geodetic layers (EPSG:4326 and Oracle's legacy 8307: geodesic on the ellipsoid) **and** projected layers
(planar in the projection, converted with `unit=M`). Values are always bind variables; identifiers come from the catalog.

### Differences from PostGIS to know about

* **Spatial index required for `SDO_*` operators.** Layers without a valid spatial index are queried with index-free `SDO_GEOM.*`
  functions (flagged in the response, and `describe_layer` warns). Create indexes (`CREATE INDEX ... INDEXTYPE IS MDSYS.SPATIAL_INDEX_V2`) and
  register the layer in `USER_SDO_GEOM_METADATA` for speed.
* **`crosses`** has no faithful Oracle mask: `spatial_filter` refuses it with an explanation instead of returning a possibly wrong
  answer (use `intersects`/`touches`/`overlaps`, or `run_sql` with your own `SDO_RELATE` mask). `within`/`contains` include boundary
  contact (`COVEREDBY`/`COVERS`) and `EQUAL`; `overlaps` is `OVERLAPBDYDISJOINT+OVERLAPBDYINTERSECT`.
* **Write mode is not available** on Oracle in this version (`--allow-write` is refused at start-up).
* Identifiers are shown in lower case (`mapsql_demo.ankara_pois`, column `name`) when they are plain upper-case Oracle names;
  they remain valid unquoted in SQL. Quoted mixed-case names are shown as they are and must be quoted in `run_sql`.
* Oracle stores the empty string as `NULL`. `EQ ''` filters are translated to `IS NULL`.
* Small numeric differences to PostGIS (checked on the demo data): distances within 0.1 m for geodetic layers; areas/lengths within
  about 0.01 %; `dwithin` joins on polygons can differ by a feature at the boundary; a buffer's area differs by its polygon approximation.
  On a projected layer distances are planar in that projection (TM30 is within about 1 m per kilometre of geodesic distances near the central meridian).
* GeoJSON conversion does not support circular arcs / compound curves (buffers of projected data are densified first); a
  geometry that Oracle cannot serialise makes the tool fail with an explicit database error.
* `list_layers` reports extents with `SDO_AGGR_MBR` (skipped for layers over 500 000 rows); `SDO_TUNE.EXTENT_OF` cannot run inside a `READ ONLY` transaction.

## Tools

| Tool | What it does |
|---|---|
| `server_info` | Database / PostGIS or Oracle Spatial versions, backend, mode, limits, allowlists |
| `list_layers` | Spatial layers with geometry type, SRID, row estimate, extent, columns (searchable) |
| `describe_layer` | Columns/types, exact count and extent, top values of text columns, numeric ranges |
| `sample_features` | A few real features |
| `find_features` | Attribute filters (`= != < > like ilike contains in between is_null` ...), ordering, column selection |
| `within_distance` | Features within *N metres* of a lon/lat **or** of other features; geodesic for lon/lat data, planar for metric CRSs, converted for feet etc.; adds `distance_m` |
| `nearest` | k nearest neighbours with `distance_m` (GiST KNN pre-selection on large tables) |
| `buffer` | Geodesic buffer of a point or a (filtered) layer, optionally dissolved, with `area_km2` |
| `spatial_filter` | intersects / within / contains / touches / crosses / overlaps / disjoint vs another layer, a bbox or a GeoJSON geometry |
| `aggregate` | count, count_distinct, sum, avg, min, max, geodesic `area_km2` / `length_km`, grouped |
| `spatial_join` | Per left feature: count / sum / avg / attach of related right features (`intersects`, `within`, `contains`, `dwithin`) |
| `bbox_of` | Bounding box and centre of a layer or subset |
| `run_sql` | Guarded **read-only** SQL (PostgreSQL or Oracle dialect); geometry columns come back as GeoJSON |
| `render_map` | PNG map as MCP image content (+ saved file), optional Leaflet HTML; layers from `result_id`s, GeoJSON or SQL; labels and choropleth (`value_property`) |
| `execute_sql` | *Only with `--allow-write`, PostGIS only*: one INSERT / UPDATE / DELETE (WHERE required) / CREATE TABLE\|INDEX\|VIEW / ALTER TABLE ADD COLUMN |

All coordinates are WGS84 lon/lat; distances are metres; areas km². Every geometry-returning tool stores the full
(simplified) result server-side and returns a `result_id`, so `render_map(result_ids=[...])` never needs the model to copy
big GeoJSON around. Responses stay small: geometries above a vertex budget are returned as representative points
(with a note) unless you ask for `geometry="full"`.

Prompts: `gis_assistant`, `spatial_question`, `explore_database`. Resources: `mapsql://guide`, `mapsql://layers`.

### Ask, don't guess

`within_distance` with no point and no reference feature does not guess a location; it fails with
*"Missing reference: near WHAT? ... ask them"*. A reference that matches several features returns the candidates and asks
which one is meant. Likewise a missing distance, an ambiguous layer name, a numeric filter given text, swapped lon/lat ...
The server instructions (sent on connect) tell the model to ask one short clarifying question first. This was motivated by
the author's TR-CBS-Komut benchmark, where models differed from 0% to 98% in recognising missing information.

## Safety model

Defence in depth - no single layer is trusted ([SECURITY.md](SECURITY.md) has the full threat model):

| Layer | What it does |
|---|---|
| **SQL parsing** | PostgreSQL: `pglast` (the actual PostgreSQL parser). Oracle: `sqlglot` (Oracle dialect) plus an independent token pass - exactly one statement, plain `SELECT`/`WITH`/set operations only; rejects DML/DDL/`MERGE`, PL/SQL blocks, `EXECUTE IMMEDIATE`, `FOR UPDATE`, database links (`@`), `DBMS_*`/`UTL_*`/`CTX_*` packages, `SYS`/`DBA_*`/`V$*` objects, sequences, URL/XML functions; anything unparseable is refused (see [SECURITY.md](SECURITY.md)). PostgreSQL details: Exactly one statement, and it must be a plain `SELECT` (CTEs/UNION/VALUES fine). Rejects DDL/DML/COPY/DO/SET/transaction control, `SELECT INTO`, `FOR UPDATE`, data-modifying CTEs, `pg_read_file`, `lo_*`, `dblink*`, `pg_sleep`, `set_config`, `nextval`, `query_to_xml`, all `pg_*` functions except a small harmless list, and credential/activity relations (`pg_authid`, `pg_shadow`, `pg_stat_activity`, `pg_settings` ...). 540+ test cases. |
| **READ ONLY transaction** | PostgreSQL: every query runs in `BEGIN READ ONLY` with `statement_timeout`, `lock_timeout`, always rolled back; a `SELECT my_volatile_function()` that tries to `INSERT` is stopped by PostgreSQL (tested). Oracle: `SET TRANSACTION READ ONLY` plus `call_timeout`, always rolled back (ORA-01456 for writes, tested with a write-capable user). |
| **Row caps** | Server-side cursor / `FETCH FIRST` + hard cap (`MAPSQL_MAX_ROWS`, default 5000); the server never materialises a huge result. |
| **Allowlists** | `--schemas public,osm` and/or `--layers 'osm.*,public.cities'`: hidden everywhere (catalog tools, SQL guard, `search_path`). |
| **No secrets in output** | Errors are rewritten to the primary message; DSNs, passwords and hosts are scrubbed; the connection string is never returned. |
| **Writes off by default** | `--allow-write` adds one separate tool with a much narrower guard (no `DROP`/`TRUNCATE`/`GRANT`, no `UPDATE`/`DELETE` without `WHERE`). |
| **Your database role** | The strongest layer: connect with a role/user that only has `SELECT` on what the assistant may see ([`scripts/readonly_role.sql`](scripts/readonly_role.sql), [`scripts/oracle_readonly_user.sql`](scripts/oracle_readonly_user.sql)). |

## Configuration

| Env var | CLI flag | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | (`--database-url`, avoid) | required | `postgresql://...` or `oracle://USER:PASS@host:1521/service_name` |
| `MAPSQL_ORACLE_DSN`, `MAPSQL_ORACLE_USER`, `MAPSQL_ORACLE_PASSWORD` | | | Oracle: DSN / TNS descriptor and credentials (alternative to the URL) |
| `MAPSQL_ORACLE_WALLET_DIR`, `MAPSQL_ORACLE_WALLET_PASSWORD` | | | Oracle: mTLS wallet (optional) |
| `MAPSQL_BACKEND` | `--backend` | auto | `postgis` or `oracle` |
| `MAPSQL_SCHEMAS` | `--schemas` | all | Comma-separated schema allowlist (Oracle: owners) |
| `MAPSQL_LAYERS` | `--layers` | all | Comma-separated table/layer patterns (`schema.table`, `schema.*`, `table`) |
| `MAPSQL_STATEMENT_TIMEOUT_MS` | `--statement-timeout-ms` | 15000 | Per-query timeout |
| `MAPSQL_MAX_ROWS` / `MAPSQL_DEFAULT_LIMIT` | `--max-rows` | 5000 / 100 | Hard row cap / default `limit` |
| `MAPSQL_ALLOW_WRITE` | `--allow-write` | off | Enable `execute_sql` (PostGIS only) |
| `MAPSQL_BASEMAP` | `--no-basemap` | `osm` | `osm`, `none`, or a tile URL template with `{z}/{x}/{y}` |
| `MAPSQL_OUTPUT_DIR` | `--output-dir` | `~/mapsql-mcp-output` | Where `render_map` saves PNG/HTML (+ tile cache) |
| `MAPSQL_OPEN_MAPS` | `--open-maps` | off | Open each rendered map in the default viewer (local use; for clients such as the Claude Code VS Code panel that do not display tool images) |
| `MAPSQL_LOG_LEVEL` | | `WARNING` | Logs go to stderr only |

Basemap tiles come from `tile.openstreetmap.org` with attribution, a descriptive User-Agent, a ≤ 16-tile cap per map and
an on-disk cache. Please respect the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/); for
heavy use point `MAPSQL_BASEMAP` at your own tile server, or use `none` (works fully offline).

## Benchmark: mapsql-bench

[`bench/`](bench) ships **60 English GIS questions** over the demo database (52 answerable + 8 that *must* be
answered with a clarifying question), in 8 categories (attribute, distance, spatial, aggregate, join, buffer, bbox,
ambiguous), with gold answers computed by hand-written SQL, and a runner that evaluates **any OpenAI-compatible model**
(OpenAI, OpenRouter, vLLM, Ollama, LM Studio ...) in an agent loop that calls this server's tools over stdio:

```bash
python bench/run_bench.py --oracle                  # no LLM: replays one reference tool call per question
python bench/run_bench.py --validate-gold           # re-checks gold answers against the database
python bench/run_bench.py --model gpt-4o-mini --api-key-env OPENAI_API_KEY
python bench/run_bench.py --model qwen2.5:7b --base-url http://localhost:11434/v1
```

Scoring: numbers within a tolerance, names as case/accent-insensitive sets, and for ambiguous questions the model must
end with `CLARIFY:` instead of `ANSWER:`. What is verified today (v0.1): the gold answers match the database (52/52) and the
toolset can express every answerable question (**oracle 52/52**). **No LLM results are published yet** - the harness is
ready; PRs adding model results to `bench/RESULTS.md` are very welcome.

## Limitations (v0.2)

* PostGIS or Oracle Spatial (one database per server); `geometry`/`geography` columns (PostGIS) or `SDO_GEOMETRY` columns (Oracle) of tables, views, materialised views, foreign tables. Oracle specifics: see [Differences from PostGIS](#differences-from-postgis-to-know-about) (read-only only, no `crosses`, index-free fallback).
* No raster, topology or 3D support in the structured tools (`run_sql` can still query them).
* The basemap needs internet (graceful fallback to a plain background) and supports OSM-style raster tiles only.
* `render_map` draws one flat colour per layer, plus a single-variable choropleth; no custom styling yet.
* The HTTP transport has no auth; `execute_sql` is a deliberately small write surface.
* Cross-CRS joins transform the left geometry; very large cross-SRID `dwithin` joins can be slow.
* Tested against PostgreSQL 18 / PostGIS 3.6 and Oracle Database 26ai (23.26, Spatial 23.7, Autonomous Database Always Free) with `mcp` 1.30 and 2.3 (the code supports both SDK generations); CI runs PostGIS 3.4 / PostgreSQL 16 and the Oracle guard tests (the database-backed Oracle tests skip without credentials).

## Roadmap

* v0.3: Oracle write mode (narrow guard), Oracle `SDO_JOIN`/Spatial Studio extras, auth for HTTP (bearer tokens), per-tool allowlists, vector-tile/PMTiles basemaps, raster/H3 summaries, an `explain`
  tool, routing helpers (pgRouting), result export (GeoJSON/GeoPackage), richer map styling
* v0.4: multi-database profiles, MCP elicitation for clarifying questions (instead of plain-text asks), catalog caching knobs
* Published benchmark results for popular models + a public leaderboard

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
# a throwaway PostGIS (never point the tests at a database you care about):
docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=mapsql_test postgis/postgis:16-3.4
export MAPSQL_TEST_DATABASE_URL=postgresql://postgres:pw@localhost:55432/mapsql_test
pytest                      # guard tests need no database; the rest recreate schemas `mt` and `mt_other`
```

Oracle tests (`pytest -m oracle`) run when `oracle-test.ini` (`[oracle_ro]`, see above) or `MAPSQL_TEST_ORACLE_URL` /
`MAPSQL_TEST_ORACLE_DSN`+`_USER`+`_PASSWORD` is available and `scripts/oracle_demo_db.py` has been run; otherwise they are skipped. The
Oracle SQL guard tests need no database.

See [CONTRIBUTING.md](CONTRIBUTING.md). Windows helper for a local cluster: `scripts/dev_cluster.ps1`.

## Türkçe

**mapsql-mcp**, yapay zekâ asistanlarının (Claude Desktop/Code, Cursor, ChatGPT uyumlu MCP istemcileri) bir **PostGIS** veya
**Oracle Spatial** (v0.2) veritabanını **güvenli biçimde** sorgulamasını, mekânsal analiz yapmasını ve cevabı **harita olarak** sohbette
göstermesini sağlayan açık kaynaklı bir MCP sunucusudur.

* **Güvenlik önce:** her sorgu gerçek PostgreSQL ayrıştırıcısı (`pglast`) ile doğrulanır; tek `SELECT`, `READ ONLY` işlem,
  zaman aşımı ve satır sınırı. `pg_read_file`, `COPY`, `dblink`, `lo_*` vb. engellidir. Yazma varsayılan olarak kapalıdır
  (`--allow-write`). Şema/katman izin listesi (`--schemas`, `--layers`) desteklenir; parola ve bağlantı dizesi çıktıya sızmaz.
* **Mekânsal araçlar:** `list_layers`, `describe_layer`, `within_distance` (metre cinsinden, SRID'den bağımsız), `nearest`,
  `buffer`, `spatial_filter`, `aggregate`, `spatial_join`, `bbox_of`, `run_sql` ve **`render_map`** (OSM altlıklı PNG +
  isteğe bağlı etkileşimli Leaflet HTML, kloropet harita).
* **Belirsizse sorar:** "yakındaki eczaneler" gibi eksik bilgili sorularda model tahmin yürütmek yerine "neye yakın?" diye
  sorar (araçlar da eksik girdiyi reddeder). Bu, yazarın TR-CBS-Komut kıyaslamasından çıkan bir derstir.
* **Kıyaslama:** `bench/` altında demo veritabanı üzerinde 60 İngilizce CBS sorusu (8'i "sormalı" tipi), altın cevaplar ve
  herhangi bir OpenAI uyumlu modeli deneyen çalıştırıcı bulunur.

**Oracle Spatial:** `pip install "mapsql-mcp[oracle]"` ve `DATABASE_URL=oracle://KULLANICI:SIFRE@sunucu:1521/servis_adi`
(uzun TNS tanımlayıcıları için `MAPSQL_ORACLE_DSN` + `MAPSQL_ORACLE_USER` + `MAPSQL_ORACLE_PASSWORD`). Aynı 14 araç; mesafeler
`unit=M` ile metre cinsinden, coğrafi (4326) ve projeksiyonlu (ör. TUREF TM30, EPSG:5254) katmanlarda doğrudur. Sorgular `sqlglot`
(Oracle lehçesi) ile doğrulanır, `SET TRANSACTION READ ONLY` ve salt okunur kullanıcı (`scripts/oracle_readonly_user.sql`) önerilir.
Ayrıntı: yukarıdaki *Oracle Spatial* bölümü.

**Kimin için?** Yapay zekâ asistanı (Claude Code, Claude Desktop, Cursor, VS Code + Copilot) kullanan ve mekânsal verisi
PostGIS ya da Oracle Spatial'da duran **CBS geliştiricileri, veritabanı uzmanları, analistler ve araştırmacılar** için.
Son kullanıcı uygulaması değildir; kendi ekranı yoktur, arayüzü sohbet penceresinin kendisidir.

### Adım adım kullanım

**1. Kurulum:**

```bash
pipx install "mapsql-mcp[oracle]"     # ya da: pip install "mapsql-mcp[oracle]"
```

**2. Veritabanını salt okunur bir kullanıcıyla bağlayın** ve bağlantıyı deneyin:

```bash
export DATABASE_URL=postgresql://salt_okunur:sifre@localhost:5432/cbs     # ya da oracle://KULLANICI:SIFRE@sunucu:1521/servis
mapsql-mcp --check
```

Salt okunur kullanıcı betikleri: `scripts/readonly_role.sql` (PostGIS), `scripts/oracle_readonly_user.sql` (Oracle).
Elinizde veritabanı yoksa yukarıdaki *Try it with the demo database* bölümüyle iki dakikada demo veritabanı kurulur
(Natural Earth + OpenStreetMap Ankara).

**3. Asistana tanıtın.** *Quickstart* bölümündeki JSON örneklerinden birini istemcinin ayar dosyasına koyun: Claude Code
için `.mcp.json`, VS Code için `.vscode/mcp.json`, Cursor için `.cursor/mcp.json`, Claude Desktop için
`claude_desktop_config.json`. VS Code'da sunucunun üstündeki **Start**'a basın; Claude Code'da `/mcp` yazıp
*connected* olduğunu görün. 14 araç listelenmelidir.

**4. Türkçe sorun.** Örnekler:

* *"Hangi katmanlar var? Özetle."*
* *"Kızılay'a (39.9208, 32.8541) 500 m içindeki eczaneleri haritada göster."*
* *"En çok kafesi olan mahalle hangisi? Mahalleleri kafe sayısına göre renklendir."*
* *"Bu noktaya en yakın 5 hastaneyi mesafeleriyle listele."*
* *"Yakındaki eczaneleri bul."* — iyi bir asistan tahmin etmez, *"neye yakın?"* diye sorar.

**5. Haritayı görün.** `render_map` haritayı asistana PNG olarak döner ve `~/mapsql-mcp-output` klasörüne kaydeder
(istenirse etkileşimli Leaflet HTML de). Bazı istemciler (ör. VS Code'daki Claude Code paneli) aracın resmini size
göstermez; sunucunun `env` kısmına `MAPSQL_OPEN_MAPS=1` (ya da `--open-maps`) ekleyin, her harita varsayılan
görüntüleyicide açılır.

**Bilmeniz gereken.** Asistanın sağlayıcısı (Anthropic, OpenAI vb.) araçların döndürdüğü satırları görür. Kurum dışına
çıkmaması gereken verinin bulunduğu veritabanlarını bağlamayın; `--schemas` / `--layers` ile yalnız gereken katmanları açın
ve veritabanı kullanıcısını salt okunur tutun. Demo veri: Natural Earth (kamu malı) ve OpenStreetMap (ODbL), ayrıntı:
`demo/ATTRIBUTION.md`.

Yazar: Mehmet Veysel Delibaş, CBS yazılım mühendisi (Türkiye).

## License and attribution

Code: [MIT](LICENSE) © 2026 Mehmet Veysel Delibaş. Demo data: Natural Earth (public domain) and OpenStreetMap
(© OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright)); see [`demo/ATTRIBUTION.md`](demo/ATTRIBUTION.md).
Maps rendered with the OSM basemap carry the required attribution.
