"""Command line entry point: ``mapsql-mcp`` (stdio by default)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .config import Settings
from .errors import MapsqlError, redact


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mapsql-mcp",
        description="Safe, spatial-aware MCP server for PostGIS and Oracle Spatial. Configure the database with the "
        "DATABASE_URL environment variable (postgresql://... or oracle://USER:PASS@host:1521/service_name; Oracle can also "
        "use MAPSQL_ORACLE_DSN + MAPSQL_ORACLE_USER + MAPSQL_ORACLE_PASSWORD). Environment variables are preferred over "
        "CLI flags, which can leak into process listings.",
    )
    p.add_argument("--version", action="version", version=f"mapsql-mcp {__version__}")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1; there is NO authentication).")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--backend", choices=["postgis", "oracle"], help="Force the database backend (default: detected from DATABASE_URL; env MAPSQL_BACKEND).")
    p.add_argument("--allow-write", action="store_true", help="PostGIS only: enable the execute_sql tool (INSERT/UPDATE/DELETE/CREATE). Off by default.")
    p.add_argument("--schemas", help="Comma separated allowlist of schemas (env MAPSQL_SCHEMAS).")
    p.add_argument("--layers", help="Comma separated allowlist of layers/tables, fnmatch patterns (env MAPSQL_LAYERS).")
    p.add_argument("--statement-timeout-ms", type=int, help="Per-query timeout (env MAPSQL_STATEMENT_TIMEOUT_MS, default 15000).")
    p.add_argument("--max-rows", type=int, help="Hard cap on rows per call (env MAPSQL_MAX_ROWS, default 5000).")
    p.add_argument("--no-basemap", action="store_true", help="Never download OSM tiles in render_map (offline mode).")
    p.add_argument("--output-dir", help="Where render_map writes PNG/HTML (env MAPSQL_OUTPUT_DIR).")
    p.add_argument("--open-maps", action="store_true", help="Open every rendered map in the default viewer, for clients that do not display tool images (env MAPSQL_OPEN_MAPS).")
    p.add_argument("--database-url", help="Connection string (prefer the DATABASE_URL env var).")
    p.add_argument("--check", action="store_true", help="Test the connection, print server info as JSON and exit.")
    return p


def settings_from_args(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    if args.database_url:
        s.database_url = args.database_url
    if args.backend:
        s.backend = args.backend
    if args.allow_write:
        s.allow_write = True
    if args.schemas:
        s.schemas = tuple(x.strip() for x in args.schemas.split(",") if x.strip()) or None
    if args.layers:
        s.layers = tuple(x.strip() for x in args.layers.split(",") if x.strip()) or None
    if args.statement_timeout_ms:
        s.statement_timeout_ms = max(100, args.statement_timeout_ms)
    if args.max_rows:
        s.max_rows = max(1, args.max_rows)
        s.default_limit = min(s.default_limit, s.max_rows)
    if args.no_basemap:
        s.basemap = "none"
    if args.output_dir:
        from pathlib import Path

        s.output_dir = Path(args.output_dir)
    if args.open_maps:
        s.open_maps = True
    s.register_secrets()
    return s


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # stdout belongs to the MCP protocol on stdio: log to stderr only
    logging.basicConfig(level=os.environ.get("MAPSQL_LOG_LEVEL", "WARNING").upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = settings_from_args(args)
        settings.require_database_url()
    except MapsqlError as e:
        print(f"mapsql-mcp: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    from .backends import create_service

    if settings.allow_write and settings.backend_name == "oracle":
        print("mapsql-mcp: --allow-write is not supported for the Oracle backend (read-only in this version).", file=sys.stderr)
        raise SystemExit(2)
    try:
        svc = create_service(settings)
    except MapsqlError as e:
        print(f"mapsql-mcp: {redact(e)}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.check:
        try:
            print(json.dumps(svc.server_info(), indent=2, default=str))
        except MapsqlError as e:
            print(f"mapsql-mcp: {redact(e)}", file=sys.stderr)
            raise SystemExit(1) from None
        finally:
            svc.close()
        return

    from .server import create_server

    server = create_server(settings, svc)
    if settings.allow_write:
        print("mapsql-mcp: WRITE MODE is enabled (execute_sql). Use a role with minimal privileges.", file=sys.stderr)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                print("mapsql-mcp: WARNING - binding to a non-loopback address; the HTTP transport has no authentication.",
                      file=sys.stderr)
            server.run("streamable-http", host=args.host, port=args.port)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        svc.close()


if __name__ == "__main__":  # pragma: no cover
    main()
