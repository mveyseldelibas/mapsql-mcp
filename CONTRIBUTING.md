# Contributing

Thanks for helping! Bug reports, new spatial tools, guard bypasses and benchmark results are all welcome.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
```

You need a **throwaway** PostGIS database for the DB tests (they drop and recreate the schemas `mt` and `mt_other`):

```bash
docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=mapsql_test postgis/postgis:16-3.4
export MAPSQL_TEST_DATABASE_URL=postgresql://postgres:pw@localhost:55432/mapsql_test
pytest                                       # DB tests are skipped automatically if the database is unreachable
```

No Docker? On Windows, `scripts/dev_cluster.ps1 init|start|stop` manages a local cluster on port 55432 from an existing
PostgreSQL+PostGIS installation without touching other services.

Oracle Spatial (optional): `pip install -e ".[dev,oracle]"`; the guard tests (`tests/test_oracle_guard.py`) need no database. For the
database-backed tests use an Oracle account you may write to once to build the demo schema (an Autonomous Database "Always Free"
instance is enough): put `[oracle]` (admin user, password, dsn) in the git-ignored `oracle-test.ini`, run
`python scripts/oracle_demo_db.py`, then `pytest -m oracle`.

## Ground rules

* **Safety first.** Any change near `guard.py`, `db.py` or the SQL builders needs tests. For guard changes add hostile cases
  to `tests/test_guard.py` (and legitimate ones to `ALLOWED`). Structured tools must never interpolate user strings:
  use `psycopg.sql.Identifier` / `Literal` and validate columns against the catalog.
* **One tool = one clear job** with an LLM-friendly description. If a required input is missing or ambiguous, raise
  `NeedsClarification` with a message that tells the model what to ask, instead of guessing.
* Keep responses small: honour the geometry budget (`shape_response`), keep `result_id`s for `render_map`.
* Both backends implement the same contract (`backends/base.py`): a new tool needs a method in `service.py` (PostGIS) **and**
  `backends/oracle.py`. Oracle SQL: values are bind variables (`Binds`), identifiers go through `quote_ident`, and anything that
  could silently differ from PostGIS must either match or fail with a clear message.
* New tool? Add: service method (`service.py`), MCP wrapper (`server.py`), a test in `tests/test_tools.py`, a row in the
  README table and, if sensible, a benchmark question with a reference `solution` in `bench/questions_src.py`.
* Style: type hints, `from __future__ import annotations`, short docstrings, no new dependencies without a reason.

## Benchmark contributions

```bash
python bench/run_bench.py --oracle
python bench/run_bench.py --model <name> --base-url <openai-compatible-url> --api-key-env <ENV>
```

Open a PR adding a row to `bench/RESULTS.md` with the model, date, server version and the saved JSON under
`bench/results/`. To change questions, edit `bench/questions_src.py` and run `python bench/make_gold.py`.

## Releasing (maintainer)

Bump `version` in `pyproject.toml` and `__init__.py`, update `CHANGELOG.md`, `pytest`, `python -m build`, upload with `twine`.
