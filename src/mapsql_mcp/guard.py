"""SQL safety guard.

Parses SQL with the *real* PostgreSQL parser (``pglast`` / libpg_query) and rejects
anything that is not a single, plain, side-effect-free ``SELECT``.

This is one of several layers of defence (see SECURITY.md):

1. this AST guard (statement type, dangerous functions/tables, schema allowlist)
2. every query runs inside ``BEGIN READ ONLY`` with ``statement_timeout``
3. row cap through a server-side cursor
4. (recommended) a dedicated read-only database role

The guard is intentionally *conservative*: when in doubt, reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.visitors import Visitor

from .config import Settings
from .errors import GuardError

# --------------------------------------------------------------------------
# deny lists
# --------------------------------------------------------------------------

#: exact function names that are never allowed (lower case)
DENY_FUNCTIONS = frozenset(
    {
        # server-side config / state mutation
        "set_config", "current_setting", "nextval", "setval", "currval", "lastval",
        # arbitrary SQL from strings (would bypass the AST check)
        "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
        "table_to_xml", "table_to_xml_and_xmlschema", "table_to_xmlschema",
        "schema_to_xml", "schema_to_xml_and_xmlschema", "schema_to_xmlschema",
        "database_to_xml", "database_to_xml_and_xmlschema", "database_to_xmlschema",
        "cursor_to_xml", "cursor_to_xmlschema",
        # network / host information
        "inet_server_addr", "inet_server_port", "inet_client_addr", "inet_client_port",
        # large objects and file access
        "lo_import", "lo_export", "lo_open", "lo_close", "lo_creat", "lo_create", "lo_unlink",
        "lo_get", "lo_put", "lo_from_bytea", "lo_truncate", "lo_lseek", "lo_tell",
        "loread", "lowrite", "lo_read", "lo_write",
        # PostGIS functions that modify catalogs / are administrative
        "addgeometrycolumn", "dropgeometrycolumn", "dropgeometrytable", "updategeometrysrid",
        "populate_geometry_columns", "postgis_enable_outdb_rasters", "postgis_disable_outdb_rasters",
        "addrasterconstraints", "droprasterconstraints", "st_addband",
    }
)

#: function name prefixes that are never allowed
DENY_FUNCTION_PREFIXES = ("dblink", "pg_", "lo_", "pgp_", "crypt", "gen_salt", "_pg_")

#: the few ``pg_*`` helpers that are harmless and useful
ALLOW_PG_FUNCTIONS = frozenset(
    {
        "pg_typeof", "pg_size_pretty", "pg_relation_size", "pg_total_relation_size", "pg_table_size",
        "pg_indexes_size", "pg_database_size", "pg_column_size", "pg_get_viewdef", "pg_get_indexdef",
        "pg_get_constraintdef", "pg_input_is_valid", "pg_encoding_to_char", "pg_postmaster_start_time",
        "pg_get_expr", "pg_table_is_visible", "pg_type_is_visible", "pg_collation_is_visible",
    }
)

#: relations (any schema) that must never be read: credentials, activity, settings
DENY_RELATIONS = frozenset(
    {
        "pg_authid", "pg_shadow", "pg_user_mapping", "pg_user_mappings", "pg_auth_members",
        "pg_stat_activity", "pg_stat_ssl", "pg_stat_gssapi", "pg_stat_replication", "pg_stat_wal_receiver",
        "pg_stat_statements", "pg_stat_statements_info", "pg_largeobject", "pg_largeobject_metadata",
        "pg_file_settings", "pg_hba_file_rules", "pg_ident_file_mappings", "pg_settings", "pg_config",
        "pg_subscription", "pg_replication_origin", "pg_replication_slots", "pg_foreign_server",
        "pg_foreign_data_wrapper", "pg_db_role_setting", "pg_shmem_allocations",
        "pg_backend_memory_contexts", "pg_prepared_statements", "pg_prepared_xacts", "pg_cursors",
        "pg_locks",
    }
)

#: schemas that are never exposed when an allowlist is active
SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})

#: statement classes allowed (in addition to SELECT) when --allow-write is set
WRITE_STATEMENTS = (
    "InsertStmt", "UpdateStmt", "DeleteStmt", "CreateStmt", "CreateTableAsStmt", "IndexStmt", "ViewStmt",
    "AlterTableStmt",
)
WRITE_ALTER_SUBTYPES = frozenset({"AT_AddColumn"})


@dataclass
class GuardResult:
    kind: str  # "select" | "write"
    statement: str  # class name of the root statement
    relations: list[tuple[str | None, str]] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


class _Scan(Visitor):
    def __init__(self) -> None:
        self.stmts: list[object] = []
        self.funcs: list[tuple[str | None, str]] = []
        self.rels: list[tuple[str | None, str]] = []
        self.ctes: set[str] = set()
        self.has_into = False
        self.has_locking = False
        self.alter_subtypes: list[str] = []
        self.dml_without_where: list[str] = []

    def visit(self, ancestors, node):  # generic hook, called for every node
        name = node.__class__.__name__
        if name.endswith("Stmt") and name not in ("SelectStmt", "RawStmt"):
            self.stmts.append(node)
        if isinstance(node, ast.SelectStmt):
            if node.intoClause is not None:
                self.has_into = True
            if node.lockingClause:
                self.has_locking = True
        elif isinstance(node, ast.FuncCall):
            parts = [p.sval for p in (node.funcname or ()) if isinstance(p, ast.String)]
            if parts:
                self.funcs.append((parts[-2].lower() if len(parts) > 1 else None, parts[-1].lower()))
        elif isinstance(node, ast.RangeVar):
            self.rels.append((node.schemaname, node.relname))
        elif isinstance(node, ast.CommonTableExpr):
            self.ctes.add(node.ctename)
        elif isinstance(node, ast.AlterTableCmd):
            sub = node.subtype
            self.alter_subtypes.append(getattr(sub, "name", str(sub)))
        elif isinstance(node, (ast.UpdateStmt, ast.DeleteStmt)):
            if node.whereClause is None:
                self.dml_without_where.append(node.__class__.__name__)


def _check_function(name: str) -> None:
    if name in DENY_FUNCTIONS:
        raise GuardError(f"Function {name}() is not allowed.")
    if name in ALLOW_PG_FUNCTIONS:
        return
    for p in DENY_FUNCTION_PREFIXES:
        if name.startswith(p):
            raise GuardError(f"Function {name}() is not allowed (blocked prefix '{p}').")


def validate_sql(
    sql: str,
    settings: Settings | None = None,
    *,
    allow_write: bool = False,
    known_relations: dict[str, set[str]] | None = None,
    max_length: int = 20000,
) -> GuardResult:
    """Validate ``sql`` and return what it touches, or raise :class:`GuardError`.

    ``known_relations`` (schema -> relation names, lower case) is used for exact
    allowlist enforcement of unqualified table names when ``settings.restricted``.
    """
    settings = settings or Settings()
    if not isinstance(sql, str) or not sql.strip():
        raise GuardError("Empty SQL.")
    if len(sql) > max_length:
        raise GuardError(f"SQL is too long (max {max_length} characters).")
    if "\x00" in sql:
        raise GuardError("NUL byte in SQL.")

    try:
        stmts = parse_sql(sql)
    except ParseError as e:
        raise GuardError(f"SQL could not be parsed as PostgreSQL: {e}") from None
    except Exception as e:  # pragma: no cover - defensive
        raise GuardError(f"SQL could not be parsed: {type(e).__name__}") from None

    if len(stmts) == 0:
        raise GuardError("Empty SQL.")
    if len(stmts) > 1:
        raise GuardError("Multiple statements are not allowed. Send exactly one statement.")

    root = stmts[0].stmt
    root_name = root.__class__.__name__
    scan = _Scan()
    scan(stmts)

    if not allow_write:
        if root_name != "SelectStmt":
            raise GuardError(f"Only SELECT statements are allowed (got {root_name.removesuffix('Stmt').upper()}).")
        if scan.stmts:
            raise GuardError(
                "Data-modifying or utility statements are not allowed anywhere in the query "
                f"(found {scan.stmts[0].__class__.__name__})."
            )
        if scan.has_into:
            raise GuardError("SELECT ... INTO creates a table and is not allowed.")
        if scan.has_locking:
            raise GuardError("Row-locking clauses (FOR UPDATE/SHARE) are not allowed.")
        kind = "select"
    else:
        allowed = set(WRITE_STATEMENTS) | {"SelectStmt"}
        if root_name not in allowed:
            raise GuardError(
                f"Statement type {root_name} is not allowed even in write mode. Allowed: INSERT, UPDATE, DELETE, "
                "CREATE TABLE [AS], CREATE INDEX, CREATE VIEW, ALTER TABLE ADD COLUMN, SELECT."
            )
        for st in scan.stmts:
            n = st.__class__.__name__
            if n not in WRITE_STATEMENTS:
                raise GuardError(f"{n} is not allowed in write mode.")
        if scan.dml_without_where:
            raise GuardError(f"{scan.dml_without_where[0].removesuffix('Stmt').upper()} without a WHERE clause is refused.")
        for sub in scan.alter_subtypes:
            if sub not in WRITE_ALTER_SUBTYPES:
                raise GuardError(f"ALTER TABLE action {sub} is not allowed (only ADD COLUMN).")
        if scan.has_locking:
            raise GuardError("Row-locking clauses are not allowed.")
        kind = "write"

    # functions (applies to read and write)
    for _schema, fname in scan.funcs:
        _check_function(fname)

    # relations
    for schema, rel in scan.rels:
        low = rel.lower()
        if low in DENY_RELATIONS:
            raise GuardError(f"Access to {rel} is not allowed.")
        sch = schema.lower() if schema else None
        if settings.restricted:
            if sch is not None:
                if sch in SYSTEM_SCHEMAS:
                    raise GuardError(f"Schema {schema} is not accessible when an allowlist is configured.")
                if not settings.relation_allowed(sch, low):
                    raise GuardError(f"Relation {schema}.{rel} is not in the configured allowlist.")
            else:
                if low.startswith("pg_"):
                    raise GuardError("System catalogs are not accessible when an allowlist is configured.")
                _check_unqualified_allowed(settings, low, rel in scan.ctes, known_relations)

    return GuardResult(
        kind=kind,
        statement=root_name,
        relations=list(scan.rels),
        functions=[f for _s, f in scan.funcs],
    )


def _check_unqualified_allowed(
    settings: Settings, rel_lower: str, is_cte: bool, known: dict[str, set[str]] | None
) -> None:
    """Unqualified name: resolve it against the known relations of the database."""
    if known is None:
        if is_cte:
            return
        raise GuardError("An allowlist is active: qualify table names as schema.table.")
    hits = [sch for sch, names in known.items() if rel_lower in names]
    if not hits:
        return  # CTE / function alias / unknown name: the database will complain if it is invalid
    # Any real relation with that name that is NOT allowed => reject, even when a CTE of the same
    # name exists somewhere in the query (conservative on purpose: scoping is not modelled).
    if any(not settings.relation_allowed(sch, rel_lower) for sch in hits):
        raise GuardError(f"Relation {rel_lower} is not in the configured allowlist.")
