"""Oracle access (python-oracledb, thin mode): pooled connections, READ ONLY transactions, call timeouts, row caps."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

import oracledb

from ..config import ORACLE_SCHEMES, Settings
from ..db import QueryResult
from ..errors import MapsqlError, redact, register_secret

_PLAIN_ID = re.compile(r"^[A-Z][A-Z0-9_$#]*$")


def display_name(name: str) -> str:
    """Oracle folds unquoted identifiers to upper case; show those in lower case (they stay valid unquoted)."""
    return name.lower() if _PLAIN_ID.match(name) else name


def quote_ident(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or '"' in name or len(name) > 128:
        raise MapsqlError(f"Invalid identifier {name!r}.")
    return '"' + name + '"'


_STR_LITERAL = re.compile(r"'(?:[^']|'')*'")
_BIND_REF = re.compile(r":([A-Za-z][A-Za-z0-9_]*)")


def used_binds(sql: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Only the binds that occur in ``sql`` (python-oracledb refuses unused ones)."""
    if not params:
        return {}
    names = set(_BIND_REF.findall(_STR_LITERAL.sub("''", sql)))
    return {k: v for k, v in params.items() if k in names}



# --------------------------------------------------------------------------- connection settings


@dataclass
class OracleConnectionParams:
    user: str
    password: str
    dsn: str
    wallet_dir: str = ""
    wallet_password: str = ""


def resolve_connection(settings: Settings) -> OracleConnectionParams:
    """Combine ``DATABASE_URL`` (oracle://USER:PASS@host:port/service?protocol=tcps) and the
    ``MAPSQL_ORACLE_*`` settings. ``MAPSQL_ORACLE_DSN`` wins over the host part of the URL; user/password from the
    environment win over the URL's."""
    user = settings.oracle_user
    password = settings.oracle_password
    dsn = settings.oracle_dsn
    url = settings.database_url
    if "://" in url and url.split("://", 1)[0].lower() in ORACLE_SCHEMES:
        parts = urlsplit(url)
        user = user or unquote(parts.username or "")
        password = password or unquote(parts.password or "")
        if not dsn:
            if not parts.hostname:
                raise MapsqlError("The Oracle URL has no host. Use oracle://USER:PASS@host:1521/service_name "
                                  "or set MAPSQL_ORACLE_DSN.")
            service = unquote(parts.path.lstrip("/"))
            if not service:
                raise MapsqlError("The Oracle URL needs a service name: oracle://USER:PASS@host:1521/service_name")
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            proto = "tcp"
            rest = []
            for k, v in query:
                if k.lower() == "protocol":
                    proto = v.lower()
                elif k.lower() in ("ssl", "tls") and v.lower() in ("1", "true", "yes", "on"):
                    proto = "tcps"
                else:
                    rest.append((k, v))
            if proto not in ("tcp", "tcps"):
                raise MapsqlError("protocol must be tcp or tcps.")
            host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
            dsn = f"{proto}://{host}:{parts.port or 1521}/{service}"
            if rest:
                dsn += "?" + "&".join(f"{k}={v}" for k, v in rest)
    if not (user and password and dsn):
        raise MapsqlError(
            "Oracle connection is incomplete: need a user, a password and a DSN. Use "
            "DATABASE_URL=oracle://USER:PASS@host:1521/service_name or MAPSQL_ORACLE_DSN + MAPSQL_ORACLE_USER + "
            "MAPSQL_ORACLE_PASSWORD."
        )
    register_secret(password)
    register_secret(dsn)
    return OracleConnectionParams(user, password, dsn, settings.oracle_wallet_dir, settings.oracle_wallet_password)


# --------------------------------------------------------------------------- errors

_TIMEOUT_CODES = {"DPY-4024", "ORA-01013", "ORA-03156", "ORA-00040", "ORA-02393"}
_CONNECT_CODES = {"DPY-6005", "DPY-6001", "DPY-6000", "DPY-3001", "ORA-01017", "ORA-12154", "ORA-12514", "ORA-12541",
                  "ORA-12170", "ORA-28000", "ORA-28001", "ORA-03113", "ORA-03114", "ORA-03135", "ORA-12545",
                  "ORA-12537", "ORA-12547"}


def _code_and_message(e: BaseException) -> tuple[str, str]:
    err = e.args[0] if getattr(e, "args", None) else None
    code = getattr(err, "full_code", "") or ""
    msg = getattr(err, "message", None) or str(e)
    return code, msg


def friendly_oracle_error(e: BaseException, timeout_ms: int | None = None) -> MapsqlError:
    """Translate an oracledb error into a short, redacted, model-friendly message."""
    if isinstance(e, MapsqlError):
        return e
    if isinstance(e, oracledb.Error):
        code, msg = _code_and_message(e)
        lines = [ln.strip() for ln in msg.splitlines() if ln.strip() and not ln.strip().startswith(("Help:", "ORA-06512"))]
        short = redact(" ".join(lines))[:500]
        if code in _TIMEOUT_CODES:
            t = f" ({timeout_ms} ms)" if timeout_ms else ""
            return MapsqlError(
                f"The query exceeded the time limit{t} and was cancelled. Add filters, a smaller area or a lower limit."
            )
        if code in ("ORA-01456",):
            return MapsqlError("The database is in read-only mode for this tool; write statements are not permitted.")
        if code in ("ORA-01031", "ORA-00990"):
            return MapsqlError("The database user does not have permission for that operation.")
        if code == "ORA-00942":
            return MapsqlError("Table or view does not exist, or is not accessible to this database user (ORA-00942).")
        if code in ("ORA-13226", "ORA-13249") and "index" in msg.lower():
            return MapsqlError(
                f"Oracle Spatial operator error: {short} (SDO_* operators need a valid spatial index on their first "
                "argument; use the structured tools, which fall back to index-free functions, or SDO_GEOM.* functions.)"
            )
        if code in _CONNECT_CODES or isinstance(e, (oracledb.OperationalError, oracledb.InterfaceError)) and not code.startswith("ORA-"):
            return MapsqlError(f"Could not talk to the database: {short[:200]}")
        return MapsqlError(f"Database error: {short}")
    if isinstance(e, OSError):
        # thin mode can surface DNS / socket failures as plain OSError (e.g. socket.gaierror on Linux)
        return MapsqlError(redact(f"Could not talk to the database: {type(e).__name__}: {e.strerror or e}")[:300])
    return MapsqlError(redact(f"{type(e).__name__}: {e}")[:400])


# --------------------------------------------------------------------------- database


def _output_type_handler(cursor: oracledb.Cursor, metadata: Any):
    """Fetch (N)CLOB/BLOB as plain str/bytes instead of LOB locators (GeoJSON comes back as CLOB)."""
    if metadata.type_code is oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_NCLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_NVARCHAR, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


@dataclass
class ColumnInfo:
    name: str
    type_name: str  # oracledb DbType name, or "OWNER.OBJECT_TYPE" for object columns
    is_geometry: bool


class OracleDatabase:
    """Thread-safe wrapper around a python-oracledb pool. Every query runs in ``SET TRANSACTION READ ONLY``
    with ``call_timeout`` and is always rolled back."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool: oracledb.ConnectionPool | None = None
        self._lock = threading.Lock()
        self._local = threading.local()  # active batch() connection of the current thread
        self._version: str | None = None

    # ---- lifecycle ----------------------------------------------------------------

    def _get_pool(self) -> oracledb.ConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    p = resolve_connection(self.settings)
                    kw: dict[str, Any] = {}
                    if p.wallet_dir:
                        kw.update(config_dir=p.wallet_dir, wallet_location=p.wallet_dir)
                        if p.wallet_password:
                            kw["wallet_password"] = p.wallet_password
                    try:
                        self._pool = oracledb.create_pool(
                            user=p.user, password=p.password, dsn=p.dsn,
                            min=0, max=self.settings.pool_size, increment=1,
                            getmode=oracledb.POOL_GETMODE_TIMEDWAIT, wait_timeout=15000,
                            timeout=120, tcp_connect_timeout=10.0, **kw,
                        )
                    except (oracledb.Error, OSError) as e:
                        raise friendly_oracle_error(e) from None
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close(force=True)
            except Exception:  # pragma: no cover
                pass
            self._pool = None

    # ---- transactions -------------------------------------------------------------

    @contextmanager
    def read_tx(self, timeout_ms: int | None = None) -> Iterator[oracledb.Connection]:
        """A READ ONLY transaction with a call timeout. Always rolled back. Inside :meth:`batch` the batch's
        transaction is reused (one connection, one ``SET TRANSACTION READ ONLY`` for many queries)."""
        timeout = int(timeout_ms or self.settings.statement_timeout_ms)
        active = getattr(self._local, "conn", None)
        if active is not None:
            try:
                active.call_timeout = timeout
                yield active
            except oracledb.Error as e:
                raise friendly_oracle_error(e, timeout) from None
            return
        pool = self._get_pool()
        try:
            conn = pool.acquire()
        except (oracledb.Error, OSError) as e:
            raise friendly_oracle_error(e, timeout) from None
        try:
            conn.call_timeout = timeout
            conn.outputtypehandler = _output_type_handler
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            yield conn
        except MapsqlError:
            raise
        except oracledb.Error as e:
            raise friendly_oracle_error(e, timeout) from None
        finally:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - broken connection
                pass
            try:
                pool.release(conn)
            except Exception:  # pragma: no cover
                try:
                    pool.drop(conn)
                except Exception:
                    pass

    @contextmanager
    def batch(self, timeout_ms: int | None = None) -> Iterator[oracledb.Connection]:
        """Run several queries on one connection / one read-only transaction (saves network round trips)."""
        if getattr(self._local, "conn", None) is not None:  # nested
            yield self._local.conn
            return
        with self.read_tx(timeout_ms) as conn:
            self._local.conn = conn
            try:
                yield conn
            finally:
                self._local.conn = None

    # ---- queries ------------------------------------------------------------------

    @staticmethod
    def _bind_values(conn: oracledb.Connection, params: dict[str, Any] | Sequence[Any] | None):
        """Long strings are bound as temporary CLOBs (portable across MAX_STRING_SIZE settings)."""
        if not params:
            return {}
        if isinstance(params, dict):
            out = {}
            for k, v in params.items():
                if isinstance(v, str) and len(v.encode("utf-8", "ignore")) > 3500:
                    v = conn.createlob(oracledb.DB_TYPE_CLOB, v)
                out[k] = v
            return out
        return list(params)

    def query(
        self,
        sql: str,
        params: dict[str, Any] | Sequence[Any] | None = None,
        *,
        max_rows: int | None = None,
        timeout_ms: int | None = None,
        catalog: bool = False,
    ) -> QueryResult:
        """Run a read-only query, returning at most ``max_rows`` rows (``truncated`` flag set if more).
        ``catalog=True`` (internal metadata queries) lifts the user-facing ``MAPSQL_MAX_ROWS`` cap."""
        cap = int(max_rows or self.settings.default_limit)
        cap = max(1, min(cap, 200000 if catalog else self.settings.max_rows))
        timeout = int(timeout_ms or self.settings.statement_timeout_ms)
        with self.read_tx(timeout) as conn:
            try:
                with conn.cursor() as cur:
                    cur.arraysize = min(cap + 1, 500)
                    cur.prefetchrows = min(cap + 1, 500)
                    cur.execute(sql, self._bind_values(conn, used_binds(sql, params) if isinstance(params, dict) else params))
                    desc = cur.description or []
                    rows = cur.fetchmany(cap + 1)
            except oracledb.Error as e:
                raise friendly_oracle_error(e, timeout) from None
        truncated = len(rows) > cap
        return QueryResult(
            columns=[display_name(d.name) for d in desc],
            type_oids=[],
            rows=[tuple(r) for r in rows[:cap]],
            truncated=truncated,
        )

    def describe(self, sql: str, params: dict[str, Any] | None = None, timeout_ms: int | None = None) -> list[ColumnInfo]:
        """Column names and types of a query without fetching rows (parse only)."""
        timeout = int(timeout_ms or self.settings.statement_timeout_ms)
        with self.read_tx(timeout) as conn:
            try:
                with conn.cursor() as cur:
                    cur.parse(sql)
                    desc = cur.description or []
                    out = []
                    for d in desc:
                        t = getattr(d, "type", None)
                        if t is not None:
                            tname = f"{getattr(t, 'schema', '')}.{getattr(t, 'name', '')}".strip(".")
                            is_geom = tname.upper().endswith("SDO_GEOMETRY")
                        else:
                            tname = str(d.type_code.name) if hasattr(d.type_code, "name") else str(d.type_code)
                            is_geom = False
                        out.append(ColumnInfo(d.name, tname, is_geom))
                    return out
            except oracledb.Error as e:
                raise friendly_oracle_error(e, timeout) from None

    # ---- facts --------------------------------------------------------------------

    def server_versions(self) -> dict[str, str | None]:
        with self.read_tx() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT SDO_VERSION FROM DUAL")
                    spatial = cur.fetchone()[0]
                    cur.execute(
                        "SELECT SYS_CONTEXT('USERENV','DB_NAME'), SYS_CONTEXT('USERENV','SESSION_USER'), "
                        "SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL"
                    )
                    dbname, user, schema = cur.fetchone()
                version = conn.version
            except oracledb.Error as e:
                raise friendly_oracle_error(e) from None
        return {"oracle": version, "oracle_spatial": spatial, "database": dbname, "user": user, "schema": schema}
