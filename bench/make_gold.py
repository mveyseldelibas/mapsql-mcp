#!/usr/bin/env python
"""Run every ``gold_sql`` of questions_src.py against the demo database and write questions.json.

    python bench/make_gold.py [--database-url postgresql://.../mapsql_demo]
"""

from __future__ import annotations

import argparse
import decimal
import json
import os
from pathlib import Path

import psycopg

from questions_src import Q


def norm(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("MAPSQL_DEMO_URL", "postgresql://postgres@127.0.0.1:55432/mapsql_demo"))
    args = ap.parse_args()
    out = []
    with psycopg.connect(args.database_url) as conn:
        conn.read_only = True
        for q in Q:
            item = {k: v for k, v in q.items() if v is not None and k != "gold_sql"}
            if q["type"] == "clarify":
                item["gold"] = None
            else:
                rows = conn.execute(q["gold_sql"]).fetchall()
                if q["type"] == "set":
                    item["gold"] = sorted(str(r[0]) for r in rows)
                else:
                    if len(rows) != 1:
                        raise SystemExit(f"{q['id']}: gold_sql must return exactly one row (got {len(rows)})")
                    item["gold"] = norm(rows[0][0])
                if item["gold"] in (None, [], 0) and q["type"] != "number":
                    raise SystemExit(f"{q['id']}: empty gold answer")
                item["gold_sql"] = q["gold_sql"]
            out.append(item)
    ids = [q["id"] for q in out]
    assert len(ids) == len(set(ids)), "duplicate ids"
    path = Path(__file__).with_name("questions.json")
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    cats = {}
    for q in out:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    print(f"wrote {len(out)} questions to {path}: {cats}")
    for q in out:
        if q["type"] != "clarify":
            print(f"  {q['id']}: {str(q['gold'])[:70]}")


if __name__ == "__main__":
    main()
