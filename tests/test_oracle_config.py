"""Oracle configuration, backend detection and secret redaction. No database needed."""

from __future__ import annotations

import pytest

pytest.importorskip("oracledb")

from mapsql_mcp.backends.oracle_db import display_name, friendly_oracle_error, resolve_connection  # noqa: E402
from mapsql_mcp.cli import build_parser, main, settings_from_args  # noqa: E402
from mapsql_mcp.config import Settings  # noqa: E402
from mapsql_mcp.errors import MapsqlError, redact  # noqa: E402


def test_backend_detection() -> None:
    assert Settings(database_url="postgresql://u:p@h/db").backend_name == "postgis"
    assert Settings(database_url="postgres://u@h/db").backend_name == "postgis"
    assert Settings(database_url="oracle://u:p@h:1521/svc").backend_name == "oracle"
    assert Settings(database_url="oracle+oracledb://u:p@h/svc").backend_name == "oracle"
    assert Settings(oracle_dsn="host/svc", oracle_user="u", oracle_password="p").backend_name == "oracle"
    assert Settings().backend_name == "postgis"  # nothing configured: keep the historic default
    assert Settings(database_url="postgresql://u@h/db", backend="oracle").backend_name == "oracle"
    with pytest.raises(MapsqlError):
        Settings(backend="mysql").backend_name  # noqa: B018


def test_url_to_connection() -> None:
    p = resolve_connection(Settings(database_url="oracle://scott:ti%40ger@db.example.com:1522/orclpdb1"))
    assert (p.user, p.password, p.dsn) == ("scott", "ti@ger", "tcp://db.example.com:1522/orclpdb1")
    p = resolve_connection(Settings(database_url="oracle://scott:tiger@db.example.com/orclpdb1?protocol=tcps"))
    assert p.dsn == "tcps://db.example.com:1521/orclpdb1"  # default port, TLS
    p = resolve_connection(Settings(database_url="oracle://u:p@h:1522/svc?tls=true&ssl_server_dn_match=yes"))
    assert p.dsn == "tcps://h:1522/svc?ssl_server_dn_match=yes"


def test_dsn_environment_form() -> None:
    desc = "(description=(address=(protocol=tcps)(port=1522)(host=h.example.com))(connect_data=(service_name=s)))"
    p = resolve_connection(Settings(oracle_dsn=desc, oracle_user="ADMIN", oracle_password="pw12345678"))
    assert p.dsn == desc and p.user == "ADMIN"
    # MAPSQL_ORACLE_DSN wins over the URL host; the URL still provides user/password
    p = resolve_connection(Settings(database_url="oracle://u:p@ignored:1521/ignored", oracle_dsn="alias_low"))
    assert (p.user, p.password, p.dsn) == ("u", "p", "alias_low")
    # explicit user/password win over the URL
    p = resolve_connection(Settings(database_url="oracle://u:p@h/s", oracle_user="U2", oracle_password="P2"))
    assert (p.user, p.password) == ("U2", "P2")


def test_incomplete_oracle_configuration_is_explained() -> None:
    with pytest.raises(MapsqlError, match="Oracle connection"):
        Settings(backend="oracle").require_database_url()
    with pytest.raises(MapsqlError, match="service name"):
        resolve_connection(Settings(database_url="oracle://u:p@host:1521"))
    with pytest.raises(MapsqlError, match="no host"):
        resolve_connection(Settings(database_url="oracle://u:p@/svc"))
    with pytest.raises(MapsqlError, match="incomplete"):
        resolve_connection(Settings(oracle_dsn="alias", oracle_user="u"))


def test_settings_repr_hides_secrets() -> None:
    s = Settings(database_url="oracle://u:topsecret@h/s", oracle_password="topsecret", oracle_wallet_password="walletpw")
    assert "topsecret" not in repr(s) and "walletpw" not in repr(s)


def test_redaction_of_oracle_connection_details() -> None:
    s = Settings(database_url="oracle://scott:Tiger-Pa55@db.internal:1522/svc")
    s.register_secrets()
    for text in [
        "cannot connect to oracle://scott:Tiger-Pa55@db.internal:1522/svc",
        "DPY-6005: cannot connect to database (tcps://db.internal:1522/svc). Tiger-Pa55",
        "ORA-12154: cannot resolve (DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST=db.internal)(PORT=1522))"
        "(CONNECT_DATA=(SERVICE_NAME=svc))(SECURITY=(SSL_SERVER_DN_MATCH=YES))) done",
        "connection failed: host=db.internal password=Tiger-Pa55",
    ]:
        out = redact(text)
        assert "Tiger-Pa55" not in out and "db.internal" not in out, out


def test_url_encoded_password_is_registered() -> None:
    Settings(database_url="oracle://u:p%40ss%23w0rd@h/s").register_secrets()
    assert "p@ss#w0rd" not in redact("auth failed for p@ss#w0rd")


def test_display_names() -> None:
    assert display_name("ANKARA_POIS") == "ankara_pois"
    assert display_name("Mixed_Case") == "Mixed_Case"  # needs quotes in SQL: shown as is
    assert display_name("WITH SPACE") == "WITH SPACE"
    assert display_name("A$B#1") == "a$b#1"


def test_cli_backend_flag_and_write_mode(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    args = build_parser().parse_args(["--backend", "oracle"])
    assert settings_from_args(args).backend_name == "oracle"
    monkeypatch.setenv("DATABASE_URL", "oracle://u:p@h:1521/svc")
    with pytest.raises(SystemExit) as e:
        main(["--allow-write", "--check"])
    assert e.value.code == 2
    assert "Oracle" in capsys.readouterr().err


def test_friendly_errors_never_echo_connection_details() -> None:
    import oracledb

    s = Settings(database_url="oracle://u:Sup3rS3cret@db.internal:1522/svc")
    s.register_secrets()
    try:
        oracledb.connect(user="u", password="Sup3rS3cret", dsn="tcps://db.invalid.example:1522/svc", tcp_connect_timeout=1)
    except oracledb.Error as e:
        msg = str(friendly_oracle_error(e))
        assert "Sup3rS3cret" not in msg and "db.invalid.example" not in msg
        assert msg.startswith("Could not talk to the database")


def test_placeholders_in_hints_survive_but_real_urls_do_not() -> None:
    assert "oracle://USER:PASS@host:1521/service_name" in redact("Example: DATABASE_URL=oracle://USER:PASS@host:1521/service_name")
    assert "postgresql://user:pass@host" in redact("Example: postgresql://user:pass@host:5432/db")
    out = redact("failed: oracle://scott:Tiger1@db:1521/x and postgres://a:b@h/db")
    assert "Tiger1" not in out and "oracle://***" in out and "postgres://***" in out
