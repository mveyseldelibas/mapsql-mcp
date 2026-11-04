"""SQL safety guard for Oracle.

Parses SQL with ``sqlglot`` (Oracle dialect) and rejects anything that is not a single, plain ``SELECT``
(``WITH`` / set operations / ``FETCH FIRST`` / hierarchical queries are fine). SQL that cannot be parsed is rejected.

Layers (see SECURITY.md): this guard -> ``SET TRANSACTION READ ONLY`` + ``call_timeout`` -> row cap ->
(recommended) a dedicated read-only database user. The guard is deliberately conservative: when in doubt, reject.

Two independent passes protect against parser quirks:

1. a **token pass** (identifiers anywhere, including ``schema.pkg.func`` chains and quoted names): database links
   (``@``), ``DBMS_*`` / ``UTL_*`` / ``CTX_*`` packages, ``SYS``-qualified objects, sequences (``NEXTVAL`` /
   ``CURRVAL``), PL/SQL keywords, URL/XML functions;
2. an **AST pass**: exactly one statement, the root must be a query, no DML/DDL/transaction nodes anywhere
   (``FOR UPDATE``, ``SELECT ... INTO`` ...), relation allowlist.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.dialects.oracle import Oracle
from sqlglot.errors import ParseError, SqlglotError, TokenError

from ..config import Settings
from ..errors import GuardError
from ..guard import GuardResult

# sqlglot logs a warning when it falls back to its generic "Command" node; we reject those anyway
logging.getLogger("sqlglot").setLevel(logging.ERROR)

#: identifier prefixes that are never allowed anywhere in a query (packages that do I/O, DDL, network, scheduling ...)
DENY_PREFIXES = ("DBMS_", "UTL_", "CTX_", "SYS_DBURIGEN", "SYS_XML", "APEX_", "OWA_")

#: exact names that are never allowed (functions / types / keywords)
DENY_WORDS = frozenset(
    {
        # sequences: NEXTVAL changes state
        "NEXTVAL", "CURRVAL",
        # PL/SQL and dynamic SQL
        "BEGIN", "DECLARE", "EXECUTE", "IMMEDIATE", "PRAGMA", "AUTONOMOUS_TRANSACTION",
        # outbound network / URL types
        "HTTPURITYPE", "URITYPE", "DBURITYPE", "XDBURITYPE", "URIFACTORY", "GETCLOB_FROM_URL",
        # XML processing (external entities, XQuery against arbitrary sources)
        "XMLTYPE", "XMLQUERY", "XMLTABLE", "XMLSEQUENCE", "XMLEXISTS", "XMLPARSE", "XMLCAST", "EXTRACTVALUE",
        "UPDATEXML", "INSERTCHILDXML", "DELETEXML", "APPENDCHILDXML", "INSERTXMLBEFORE", "INSERTXMLAFTER",
        "XMLSCHEMA", "XMLDIFF", "XMLPATCH",
        # misc.
        "SQLEXEC", "EXTPROC", "JAVA_EXEC",
    }
)

#: owners whose objects are never readable (a qualified reference ``SYS.x`` is rejected, except SYS.DUAL)
DENY_SCHEMAS = frozenset(
    {
        "SYS", "SYSTEM", "XDB", "CTXSYS", "DVSYS", "LBACSYS", "AUDSYS", "OUTLN", "DBSNMP", "GSMADMIN_INTERNAL",
        "ORDS_METADATA", "ORDS_PUBLIC_USER", "ORDS_PLSQL_GATEWAY", "SYSRAC", "SYSBACKUP", "SYSDG", "SYSKM",
        "APPQOSSYS", "GGSYS", "DBSFWUSER", "REMOTE_SCHEDULER_AGENT", "ORACLE_OCM", "WMSYS", "OJVMSYS",
    }
)

#: relation-name prefixes / suffixes that are never allowed (any schema)
DENY_RELATION_PREFIXES = ("DBA_", "V$", "GV$", "X$", "V_$", "GV_$", "CDB_")
DENY_RELATION_SUFFIXES = ("_DB_LINKS", "_SOURCE", "_CREDENTIALS", "_AUDIT_TRAIL", "_USERS_WITH_DEFPWD")

#: MDSYS relations that are plain reference data and useful for CRS questions
MDSYS_READABLE_PREFIXES = ("SDO_", "CS_SRS", "ALL_SDO_", "USER_SDO_")

#: AST node classes that must not appear anywhere in a query
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterSession", "AlterRename", "AlterColumn",
    "TruncateTable", "Grant", "Revoke", "Command", "Set", "Commit", "Rollback", "Transaction", "Use", "Copy",
    "Declare", "Pragma", "Comment", "Analyze", "Describe", "Show", "Kill", "Refresh", "Execute", "Attach", "Detach",
    "Cache", "Uncache", "LoadData", "Lock", "Into", "Export", "Install", "Summarize",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_ROOT_TYPES = tuple(
    getattr(exp, n) for n in ("Select", "SetOperation", "Union", "Except", "Intersect", "Subquery") if hasattr(exp, n)
)

_SKIP_TOKEN_TYPES = frozenset(
    {"STRING", "RAW_STRING", "NATIONAL_STRING", "NATIONAL_RAW_STRING", "UNICODE_STRING", "BYTE_STRING", "HEREDOC_STRING",
     "HEX_STRING", "BIT_STRING", "NUMBER", "HINT"}
)


@dataclass
class OracleKnown:
    """What the guard knows about the connected account (needed for exact allowlist enforcement)."""

    current_schema: str
    #: schema (lower case) -> relation names (lower case) of tables/views owned by that schema
    objects: dict[str, set[str]] = field(default_factory=dict)
    #: private synonym name (lower case) -> (target schema, target name) lower case
    synonyms: dict[str, tuple[str, str]] = field(default_factory=dict)


def _tokens(sql: str):
    try:
        return Oracle().tokenize(sql)
    except TokenError as e:
        raise GuardError(f"SQL could not be tokenized as Oracle SQL: {str(e)[:120]}") from None
    except SqlglotError as e:  # pragma: no cover - defensive
        raise GuardError(f"SQL could not be tokenized: {type(e).__name__}") from None


def _token_pass(sql: str) -> list[str]:
    """Reject dangerous identifiers wherever they appear. Returns the identifier words seen (upper case)."""
    toks = _tokens(sql)
    words: list[str] = []
    n = len(toks)
    for i, t in enumerate(toks):
        ttype = t.token_type.name
        if ttype in _SKIP_TOKEN_TYPES:
            continue
        text = t.text or ""
        if ttype == "PARAMETER" or "@" in text:
            raise GuardError("Database links (@) and SQL*Plus '@' commands are not allowed.")
        up = text.upper()
        if ttype in ("DOT", "L_PAREN", "R_PAREN", "COMMA", "SEMICOLON", "STAR", "EQ", "NEQ", "LT", "LTE", "GT", "GTE",
                     "PLUS", "DASH", "SLASH", "COLON", "L_BRACKET", "R_BRACKET", "PIPE", "DPIPE", "MOD"):
            continue
        words.append(up)
        for p in DENY_PREFIXES:
            if up.startswith(p):
                raise GuardError(f"{text} (packages/objects with prefix {p.rstrip('_')}) is not allowed.")
        if up in DENY_WORDS:
            raise GuardError(f"{text} is not allowed.")
        nxt = toks[i + 1] if i + 1 < n else None
        if up in DENY_SCHEMAS and nxt is not None and nxt.token_type.name == "DOT":
            after = toks[i + 2].text.upper() if i + 2 < n else ""
            if not (up == "SYS" and after == "DUAL"):
                raise GuardError(f"Access to objects of {up} is not allowed.")
    return words


def _check_relation(
    settings: Settings,
    known: OracleKnown | None,
    ctes: set[str],
    schema: str | None,
    name: str,
) -> None:
    up = name.upper()
    low = name.lower()
    if "$" in name or up.startswith(DENY_RELATION_PREFIXES) or up.endswith(DENY_RELATION_SUFFIXES):
        raise GuardError(f"Access to {name} is not allowed.")
    if schema is not None:
        su = schema.upper()
        if su in DENY_SCHEMAS:
            if su == "SYS" and up == "DUAL":
                return
            raise GuardError(f"Access to objects of {su} is not allowed.")
        if su == "MDSYS" and not up.startswith(MDSYS_READABLE_PREFIXES):
            raise GuardError(f"Access to MDSYS.{name} is not allowed.")
        if settings.restricted and not settings.relation_allowed(schema, name):
            raise GuardError(f"Relation {schema}.{name} is not in the configured allowlist.")
        return
    # unqualified name
    if not settings.restricted:
        return
    if up == "DUAL":
        return
    if known is not None:
        cur = known.current_schema.lower()
        if low in known.synonyms:
            tsch, tname = known.synonyms[low]
            if not tsch or not settings.relation_allowed(tsch, tname):  # tsch == "": synonym over a database link
                raise GuardError(f"Relation {name} is not in the configured allowlist.")
            return
        if low in known.objects.get(cur, set()):
            if not settings.relation_allowed(cur, low):
                raise GuardError(f"Relation {name} is not in the configured allowlist.")
            return
    if low in ctes:
        return
    raise GuardError(
        f"An allowlist is active: qualify table names as schema.table ({name!r} is not a table of the connected schema)."
    )


def validate_oracle_sql(
    sql: str,
    settings: Settings | None = None,
    *,
    known: OracleKnown | None = None,
    max_length: int = 20000,
) -> GuardResult:
    """Validate ``sql`` and return what it touches, or raise :class:`GuardError`."""
    settings = settings or Settings()
    if not isinstance(sql, str) or not sql.strip():
        raise GuardError("Empty SQL.")
    if len(sql) > max_length:
        raise GuardError(f"SQL is too long (max {max_length} characters).")
    if "\x00" in sql:
        raise GuardError("NUL byte in SQL.")

    words = _token_pass(sql)
    if re.search(r"(?m)^[ \t]*/[ \t]*$", sql):
        raise GuardError("A lone '/' (SQL*Plus terminator) is not allowed; send one plain SELECT.")

    try:
        parsed = sqlglot.parse(sql, read="oracle")
    except (ParseError, TokenError) as e:
        first = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
        raise GuardError(f"SQL could not be parsed as Oracle SQL ({first}). PL/SQL blocks and unsupported syntax are rejected.") from None
    except Exception as e:  # pragma: no cover - defensive
        raise GuardError(f"SQL could not be parsed: {type(e).__name__}") from None

    stmts = [s for s in parsed if s is not None]
    if not stmts:
        raise GuardError("Empty SQL.")
    if len(stmts) > 1:
        raise GuardError("Multiple statements are not allowed. Send exactly one statement.")
    root = stmts[0]
    if isinstance(root, exp.Command) or not isinstance(root, _ROOT_TYPES):
        label = type(root).__name__.upper()
        raise GuardError(f"Only SELECT statements are allowed (got {label}).")

    ctes: set[str] = set()
    tables: list[tuple[str | None, str]] = []
    funcs: list[str] = []
    for node in root.find_all(exp.Expression):
        if isinstance(node, _FORBIDDEN_NODES):
            kind = type(node).__name__
            if kind == "Lock":
                raise GuardError("Row-locking clauses (FOR UPDATE) are not allowed.")
            if kind == "Into":
                raise GuardError("SELECT ... INTO is not allowed.")
            raise GuardError(f"Data-modifying, PL/SQL or utility statements are not allowed anywhere in the query (found {kind}).")
        if isinstance(node, exp.CTE):
            ctes.add((node.alias or "").lower())
        elif isinstance(node, exp.Table):
            if node.args.get("catalog"):
                raise GuardError("Three-part names (database links / catalogs) are not allowed.")
            nm = node.name
            if nm:
                db = node.args.get("db")
                tables.append((db.name if db is not None else None, nm))
        elif isinstance(node, exp.Anonymous):
            funcs.append(str(node.name))
        elif isinstance(node, exp.Func):
            funcs.append(node.sql_name())

    for schema, name in tables:
        _check_relation(settings, known, ctes, schema, name)

    return GuardResult(kind="select", statement=type(root).__name__, relations=tables, functions=funcs)
