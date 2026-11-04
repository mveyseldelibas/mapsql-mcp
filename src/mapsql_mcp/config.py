"""Runtime configuration (environment variables + CLI overrides)."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .errors import MapsqlError, register_secret


def _csv(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    items = tuple(x.strip() for x in value.split(",") if x.strip())
    return items or None


def _int(env: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(env)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise MapsqlError(f"{env} must be an integer, got {raw!r}") from e
    return max(lo, min(hi, v))


def _bool(env: str, default: bool = False) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ORACLE_SCHEMES = ("oracle", "oracle+oracledb", "oracledb")
POSTGRES_SCHEMES = ("postgres", "postgresql", "postgis")


@dataclass
class Settings:
    #: ``postgresql://...`` (PostGIS) or ``oracle://USER:PASS@host:port/service_name`` (Oracle Spatial)
    database_url: str = field(default="", repr=False)  # contains the password: never in repr/logs
    #: force a backend ("postgis" | "oracle"); empty = detect from DATABASE_URL / MAPSQL_ORACLE_DSN
    backend: str = ""
    #: Oracle only: connect descriptor (TNS descriptor, Easy Connect string or tnsnames alias); overrides the host part
    #: of an oracle:// DATABASE_URL. User and password come from the URL or from the two settings below.
    oracle_dsn: str = ""
    oracle_user: str = ""
    oracle_password: str = field(default="", repr=False)
    #: Oracle only: optional mTLS wallet directory (cwallet.sso / ewallet.pem) and its password
    oracle_wallet_dir: str = ""
    oracle_wallet_password: str = field(default="", repr=False)
    allow_write: bool = False
    #: only these schemas are visible (None = every schema the DB role can read)
    schemas: tuple[str, ...] | None = None
    #: only layers/tables matching these fnmatch patterns ("schema.table", "schema.*", "table")
    layers: tuple[str, ...] | None = None
    default_limit: int = 100
    max_rows: int = 5000
    statement_timeout_ms: int = 15000
    basemap: str = "osm"  # "osm" | "none" | custom tile URL template with {z}/{x}/{y}
    output_dir: Path = field(default_factory=lambda: Path.home() / "mapsql-mcp-output")
    #: open each rendered map in the desktop's default viewer (local stdio use; for clients that do not show tool images)
    open_maps: bool = False
    pool_size: int = 4

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            database_url=os.environ.get("DATABASE_URL", "").strip(),
            backend=os.environ.get("MAPSQL_BACKEND", "").strip().lower(),
            oracle_dsn=os.environ.get("MAPSQL_ORACLE_DSN", "").strip(),
            oracle_user=os.environ.get("MAPSQL_ORACLE_USER", "").strip(),
            oracle_password=os.environ.get("MAPSQL_ORACLE_PASSWORD", ""),
            oracle_wallet_dir=os.environ.get("MAPSQL_ORACLE_WALLET_DIR", "").strip(),
            oracle_wallet_password=os.environ.get("MAPSQL_ORACLE_WALLET_PASSWORD", ""),
            allow_write=_bool("MAPSQL_ALLOW_WRITE"),
            schemas=_csv(os.environ.get("MAPSQL_SCHEMAS")),
            layers=_csv(os.environ.get("MAPSQL_LAYERS")),
            default_limit=_int("MAPSQL_DEFAULT_LIMIT", 100, 1, 10000),
            max_rows=_int("MAPSQL_MAX_ROWS", 5000, 1, 100000),
            statement_timeout_ms=_int("MAPSQL_STATEMENT_TIMEOUT_MS", 15000, 100, 600000),
            basemap=os.environ.get("MAPSQL_BASEMAP", "osm").strip() or "osm",
            output_dir=Path(os.environ.get("MAPSQL_OUTPUT_DIR") or (Path.home() / "mapsql-mcp-output")),
            open_maps=_bool("MAPSQL_OPEN_MAPS"),
        )
        s.default_limit = min(s.default_limit, s.max_rows)
        s.register_secrets()
        return s

    def register_secrets(self) -> None:
        """Teach the redactor about the passwords embedded in the DSN / given through the environment."""
        url = self.database_url
        if "://" in url and "@" in url:
            userinfo = url.split("://", 1)[1].rsplit("@", 1)[0]
            if ":" in userinfo:
                pw = userinfo.split(":", 1)[1]
                register_secret(pw)
                register_secret(unquote(pw))
        register_secret(url)
        register_secret(self.oracle_password)
        register_secret(self.oracle_wallet_password)

    # ---- backend ----------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        """``"postgis"`` or ``"oracle"``: explicit ``MAPSQL_BACKEND``, else the URL scheme, else Oracle when only an
        Oracle DSN is configured, else PostGIS."""
        if self.backend in ("postgis", "oracle"):
            return self.backend
        if self.backend:
            raise MapsqlError(f"MAPSQL_BACKEND must be 'postgis' or 'oracle', got {self.backend!r}")
        scheme = self.database_url.split("://", 1)[0].lower() if "://" in self.database_url else ""
        if scheme in ORACLE_SCHEMES:
            return "oracle"
        if scheme in POSTGRES_SCHEMES:
            return "postgis"
        if not self.database_url and self.oracle_dsn:
            return "oracle"
        return "postgis"

    # ---- allowlist helpers -------------------------------------------------

    def relation_allowed(self, schema: str, table: str) -> bool:
        if self.schemas is not None and schema.lower() not in {s.lower() for s in self.schemas}:
            return False
        if self.layers is not None:
            qual = f"{schema}.{table}".lower()
            for pat in self.layers:
                p = pat.lower()
                if "." not in p:
                    p = "*." + p
                if fnmatch.fnmatchcase(qual, p):
                    return True
            return False
        return True

    @property
    def restricted(self) -> bool:
        return self.schemas is not None or self.layers is not None

    def require_database_url(self) -> str:
        if self.backend_name == "oracle":
            self.require_oracle_connection()
            return self.database_url
        if not self.database_url:
            raise MapsqlError(
                "DATABASE_URL is not set. Example: DATABASE_URL=postgresql://user:pass@host:5432/dbname "
                "(Oracle Spatial: DATABASE_URL=oracle://user:pass@host:1521/service_name)"
            )
        return self.database_url

    def require_oracle_connection(self) -> None:
        has_url = self.database_url.split("://", 1)[0].lower() in ORACLE_SCHEMES
        if has_url or (self.oracle_dsn and self.oracle_user and self.oracle_password):
            return
        raise MapsqlError(
            "Oracle connection is not configured. Set DATABASE_URL=oracle://USER:PASS@host:1521/service_name, or "
            "MAPSQL_ORACLE_DSN + MAPSQL_ORACLE_USER + MAPSQL_ORACLE_PASSWORD (DATABASE_URL is not set)."
        )
