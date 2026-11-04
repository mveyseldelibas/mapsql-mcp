"""Texts of the Oracle backend (no heavy imports, so ``server.py`` can read them without ``oracledb``)."""

from __future__ import annotations

from .base import BackendSpec

ORACLE_NOTES = """
Oracle Spatial notes
- This server talks to Oracle Database (Spatial). Layers are tables/views with an SDO_GEOMETRY column; names look like
  schema.table (all lower-case in results; unquoted Oracle identifiers are case-insensitive).
- run_sql takes Oracle SQL: no LIMIT (use FETCH FIRST n ROWS ONLY), no trailing semicolon, no database links, no
  DBMS_/UTL_ packages. Spatial operators (SDO_WITHIN_DISTANCE, SDO_NN, SDO_RELATE, SDO_ANYINTERACT) need a spatial index on
  their first argument and return 'TRUE'; pass 'unit=M' for metres: SDO_WITHIN_DISTANCE(g, ref, 'distance=500 unit=M') = 'TRUE'.
  Geometry columns in the result are converted to GeoJSON (EPSG:4326) automatically.
- Oracle treats the empty string as NULL.
"""

SPEC = BackendSpec(
    name="oracle",
    label="Oracle Spatial",
    sql_dialect="Oracle SQL / Oracle Spatial",
    server_info_hint="Oracle Database and Oracle Spatial versions",
    run_sql_hint="single SELECT, Oracle SQL: FETCH FIRST n ROWS ONLY instead of LIMIT, SDO_* functions, no trailing semicolon",
    extra_instructions=ORACLE_NOTES,
    supports_write=False,
)
