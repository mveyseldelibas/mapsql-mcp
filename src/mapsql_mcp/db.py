"""PostgreSQL access: pooled connections, read-only transactions, row caps."""

from __future__ import annotations

import datetime as dt
import decimal
import ipaddress
import math
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import errors as pgerr
from psycopg_pool import ConnectionPool

from .config import Settings
from .errors import MapsqlError, redact


@dataclass
class QueryResult:
    columns: list[str]
    type_oids: list[int]
    rows: list[tuple]
    truncated: bool = False
    rowcount: int | None = None  # for write statements
    notices: list[str] = field(default_factory=list)


def json_safe(v: Any, max_str: int = 2000) -> Any:
    """Convert a database value to something ``json.dumps`` accepts."""
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        if not v.is_finite():
            return None
        f = float(v)
        return int(v) if v == v.to_integral_value() and abs(f) < 1e15 else f
    if isinstance(v, str):
        return v if len(v) <= max_str else v[:max_str] + f"... [{len(v) - max_str} more chars]"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(v))} bytes>"
    if isinstance(v, (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(v)
    if isinstance(v, dict):
        return {str(k): json_safe(x, max_str) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [json_safe(x, max_str) for x in v]
    if hasattr(v, "lower") and hasattr(v, "upper"):  # psycopg Range
        return str(v)
    return str(v)


def friendly_db_error(e: BaseException, timeout_ms: int | None = None) -> MapsqlError:
    """Translate a psycopg error into a short, redacted, model-friendly message."""
    if isinstance(e, pgerr.QueryCanceled):
        t = f" ({timeout_ms} ms)" if timeout_ms else ""
        return MapsqlError(
            f"The query exceeded the time limit{t} and was cancelled. Add filters, a smaller area or a lower limit."
        )
    if isinstance(e, pgerr.ReadOnlySqlTransaction):
        return MapsqlError("The database is in read-only mode for this tool; write statements are not permitted.")
    if isinstance(e, (pgerr.InsufficientPrivilege,)):
        return MapsqlError("The database role does not have permission for that operation.")
    if isinstance(e, psycopg.OperationalError) and not isinstance(e, pgerr.QueryCanceled):
        # connection problems; never echo the DSN
        diag = getattr(e, "diag", None)
        msg = (diag.message_primary if diag and diag.message_primary else "connection failed")
        return MapsqlError(f"Could not talk to the database: {redact(msg)[:200]}")
    if isinstance(e, psycopg.Error):
        diag = e.diag
        msg = diag.message_primary or str(e)
        parts = [redact(msg)]
        if diag.message_detail:
            parts.append(redact(diag.message_detail))
        if diag.message_hint:
            parts.append("Hint: " + redact(diag.message_hint))
        return MapsqlError("Database error: " + " ".join(parts)[:600])
    return MapsqlError(redact(f"{type(e).__name__}: {e}")[:400])


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()
        self._geom_oids: set[int] | None = None

    # ---- lifecycle -----------------------------------------------------------

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    url = self.settings.require_database_url()
                    pool = ConnectionPool(
                        url,
                        min_size=0,
                        max_size=self.settings.pool_size,
                        kwargs={"application_name": "mapsql-mcp", "connect_timeout": 10},
                        open=False,
                        timeout=15,
                        name="mapsql",
                    )
                    pool.open(wait=False)
                    self._pool = pool
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close(timeout=3)
            except Exception:  # pragma: no cover
                pass
            self._pool = None

    # ---- transactions --------------------------------------------------------

    @contextmanager
    def read_tx(self, timeout_ms: int | None = None) -> Iterator[psycopg.Connection]:
        """A READ ONLY transaction with a statement timeout. Always rolled back."""
        timeout = int(timeout_ms or self.settings.statement_timeout_ms)
        try:
            with self._get_pool().connection() as conn:
                conn.read_only = True
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT set_config('statement_timeout', %s, true),"
                            " set_config('lock_timeout', '2000', true),"
                            " set_config('idle_in_transaction_session_timeout', %s, true)",
                            (str(timeout), str(timeout * 2)),
                        )
                        sp = self.search_path_sql()
                        if sp:
                            cur.execute("SELECT set_config('search_path', %s, true)", (sp,))
                    yield conn
                finally:
                    try:
                        conn.rollback()
                    except Exception:  # pragma: no cover
                        pass
        except MapsqlError:
            raise
        except psycopg.Error as e:
            raise friendly_db_error(e, timeout) from None
        except Exception as e:
            # pool timeouts etc.
            from psycopg_pool import PoolTimeout

            if isinstance(e, PoolTimeout):
                raise MapsqlError("Could not obtain a database connection (pool timeout). Is the database reachable?") from None
            raise

    @contextmanager
    def write_tx(self, timeout_ms: int | None = None) -> Iterator[psycopg.Connection]:
        timeout = int(timeout_ms or self.settings.statement_timeout_ms)
        try:
            with self._get_pool().connection() as conn:
                conn.read_only = False
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout),))
                        sp = self.search_path_sql()
                        if sp:
                            cur.execute("SELECT set_config('search_path', %s, true)", (sp,))
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except psycopg.Error as e:
            raise friendly_db_error(e, timeout) from None

    def search_path_sql(self) -> str | None:
        """Restrict ``search_path`` to the allowed schemas (+ the PostGIS schema)."""
        if self.settings.schemas is None:
            return None
        names = list(self.settings.schemas)
        extra = self._postgis_schema
        if extra and extra not in [n.lower() for n in names]:
            names.append(extra)
        return ",".join('"' + n.replace('"', '""') + '"' for n in names)

    _postgis_schema: str | None = None

    # ---- queries -------------------------------------------------------------

    def query(
        self,
        sql: str,
        params: Sequence[Any] | dict | None = None,
        *,
        max_rows: int | None = None,
        timeout_ms: int | None = None,
    ) -> QueryResult:
        """Run a read-only query, returning at most ``max_rows`` rows (``truncated`` flag set if more)."""
        cap = int(max_rows or self.settings.default_limit)
        cap = max(1, min(cap, self.settings.max_rows))
        with self.read_tx(timeout_ms) as conn:
            try:
                with conn.cursor(name="mapsql_cur") as cur:
                    cur.itersize = min(cap + 1, 2000)
                    cur.execute(sql, params)
                    desc = cur.description or []
                    rows = cur.fetchmany(cap + 1)
            except psycopg.Error as e:
                raise friendly_db_error(e, timeout_ms or self.settings.statement_timeout_ms) from None
        truncated = len(rows) > cap
        return QueryResult(
            columns=[d.name for d in desc],
            type_oids=[d.type_code for d in desc],
            rows=[tuple(r) for r in rows[:cap]],
            truncated=truncated,
        )

    def describe(self, sql: str, timeout_ms: int | None = None) -> tuple[list[str], list[int]]:
        """Column names and type OIDs of a query without fetching rows."""
        with self.read_tx(timeout_ms) as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM ({sql}) AS _mapsql_q LIMIT 0")
                    desc = cur.description or []
            except psycopg.Error as e:
                raise friendly_db_error(e, timeout_ms or self.settings.statement_timeout_ms) from None
        return [d.name for d in desc], [d.type_code for d in desc]

    def execute_write(self, sql: str, timeout_ms: int | None = None) -> QueryResult:
        with self.write_tx(timeout_ms) as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        rows = cur.fetchmany(self.settings.default_limit)
                        return QueryResult(
                            [d.name for d in cur.description], [d.type_code for d in cur.description],
                            [tuple(r) for r in rows], rowcount=cur.rowcount,
                        )
                    return QueryResult([], [], [], rowcount=cur.rowcount)
            except psycopg.Error as e:
                raise friendly_db_error(e, timeout_ms or self.settings.statement_timeout_ms) from None

    # ---- PostGIS facts -------------------------------------------------------

    def geometry_oids(self) -> set[int]:
        if self._geom_oids is None:
            res = self.query(
                "SELECT t.oid::bigint, n.nspname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname IN ('geometry', 'geography')",
                max_rows=20,
            )
            self._geom_oids = {r[0] for r in res.rows}
            if res.rows:
                self._postgis_schema = res.rows[0][1].lower()
        return self._geom_oids

    def geography_oid(self) -> int | None:
        res = self.query("SELECT oid::bigint FROM pg_type WHERE typname = 'geography' LIMIT 1", max_rows=1)
        return res.rows[0][0] if res.rows else None

    def server_versions(self) -> dict[str, str | None]:
        res = self.query(
            "SELECT current_setting('server_version'), "
            "(SELECT extversion FROM pg_extension WHERE extname = 'postgis'), current_database()",
            max_rows=1,
        )
        pg, postgis, dbname = res.rows[0]
        return {"postgresql": pg, "postgis": postgis, "database": dbname}
