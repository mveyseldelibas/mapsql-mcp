#!/usr/bin/env python
"""mapsql-bench runner.

Three modes:

  --validate-gold   re-run the gold SQL and compare with questions.json (detects data drift)
  --oracle          replay the reference tool call of every question through the real MCP server
                    (no LLM): proves the toolset can express every question. Expected score: 100%.
  (default)         evaluate an OpenAI-compatible chat model in an agent loop that can call the
                    mapsql-mcp tools (any provider: OpenAI, OpenRouter, Together, vLLM, Ollama, LM Studio ...)

Examples
  python bench/run_bench.py --oracle
  python bench/run_bench.py --model gpt-4o-mini --api-key-env OPENAI_API_KEY
  python bench/run_bench.py --model qwen2.5:7b --base-url http://localhost:11434/v1
  python bench/run_bench.py --model my-model --base-url https://openrouter.ai/api/v1 --api-key-env OPENROUTER_API_KEY --category distance

The runner starts ``python -m mapsql_mcp`` over stdio against the demo database
(DATABASE_URL / --database-url, default: the local dev cluster on 127.0.0.1:55432).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.json"
DEFAULT_DB = os.environ.get("MAPSQL_DEMO_URL", "postgresql://postgres@127.0.0.1:55432/mapsql_demo")

PROTOCOL = """\
You are being evaluated on spatial questions about a PostGIS database. Use the tools; do not guess.
Finish with exactly ONE final line in one of these forms:
  ANSWER: <value>        a bare number (no units, no thousands separators), or the exact name, or a JSON list of
                         names such as ["a","b"] when several items are asked for
  CLARIFY: <question>    if essential information is missing or ambiguous (do NOT query or guess in that case)
  CANNOT_ANSWER: <why>   if the database cannot answer the question
Distances from the tools are in metres, areas in km2 (unless the question asks for another unit, convert it)."""


# --------------------------------------------------------------------------- scoring


def norm_text(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).casefold().strip().strip("\"'")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def to_number(x: object) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    m = re.search(r"-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?|-?\.\d+", str(x))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_final(text: str) -> tuple[str, object]:
    """-> (kind, value) with kind in answer|clarify|cannot|none"""
    for line in reversed([l.strip() for l in (text or "").splitlines() if l.strip()]):
        m = re.match(r"^\**\s*(ANSWER|CLARIFY|CANNOT_ANSWER)\s*:\s*(.*)$", line, re.I)
        if m:
            kind = {"answer": "answer", "clarify": "clarify", "cannot_answer": "cannot"}[m.group(1).lower()]
            val = m.group(2).strip().strip("*")
            if kind == "answer":
                try:
                    return kind, json.loads(val)
                except ValueError:
                    return kind, val
            return kind, val
    return "none", None


def score(q: dict, kind: str, value: object) -> tuple[bool, str]:
    t = q["type"]
    if t == "clarify":
        return (kind in ("clarify", "cannot"), "asked" if kind in ("clarify", "cannot") else f"answered instead of asking ({kind})")
    if kind != "answer":
        return False, f"no answer ({kind})"
    gold = q["gold"]
    if t == "number":
        n = to_number(value)
        if n is None:
            return False, f"not a number: {value!r}"
        tol = max(q.get("tol", 0.0), abs(gold) * q.get("rel", 0.0), 1e-9 if isinstance(gold, float) else 0.0)
        if isinstance(gold, int) or float(gold).is_integer():
            tol = max(tol, 0.0)
        ok = abs(n - float(gold)) <= tol + 1e-9
        return ok, f"{n} vs {gold} (tol {tol})"
    if t == "text":
        return norm_text(value) == norm_text(gold), f"{value!r} vs {gold!r}"
    if t == "set":
        items = value if isinstance(value, list) else [x for x in re.split(r"[;,]\s*", str(value)) if x]
        got, want = {norm_text(i) for i in items}, {norm_text(i) for i in gold}
        return got == want, f"{sorted(got)} vs {sorted(want)}"
    return False, f"unknown type {t}"


def dig(obj: object, path: str) -> object:
    """Tiny path language: a.b.0.c and a[*].b (list projection)."""
    cur: list[object] = [obj]
    projected = False
    for part in path.split("."):
        star = part.endswith("[*]")
        key = part[:-3] if star else part
        nxt: list[object] = []
        for c in cur:
            if key != "":
                if isinstance(c, list):
                    c = c[int(key)]
                elif isinstance(c, dict):
                    c = c[key]
            if star:
                nxt.extend(c)  # type: ignore[arg-type]
                projected = True
            else:
                nxt.append(c)
        cur = nxt
    return cur if projected else cur[0]


# --------------------------------------------------------------------------- MCP plumbing


def text_of(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


def is_error(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


class McpSession:
    def __init__(self, database_url: str):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {**os.environ, "DATABASE_URL": database_url}
        out = Path(os.environ.get("TEMP", "/tmp")) / "mapsql-bench-out"
        self.params = StdioServerParameters(
            command=sys.executable, args=["-m", "mapsql_mcp", "--no-basemap", "--output-dir", str(out)], env=env
        )
        self._stdio_client = stdio_client
        self._ClientSession = ClientSession

    async def __aenter__(self):
        self._cm = self._stdio_client(self.params)
        r, w = await self._cm.__aenter__()
        self._sess_cm = self._ClientSession(r, w)
        self.session = await self._sess_cm.__aenter__()
        init = await self.session.initialize()
        self.instructions = init.instructions or ""
        return self

    async def __aexit__(self, *exc):
        await self._sess_cm.__aexit__(*exc)
        await self._cm.__aexit__(*exc)


# --------------------------------------------------------------------------- modes


def load_questions(args) -> list[dict]:
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    if args.category:
        qs = [q for q in qs if q["category"] in args.category.split(",")]
    if args.ids:
        want = set(args.ids.split(","))
        qs = [q for q in qs if q["id"] in want]
    if args.limit:
        qs = qs[: args.limit]
    return qs


def validate_gold(args) -> int:
    import psycopg

    qs = [q for q in load_questions(args) if q["type"] != "clarify"]
    bad = 0
    with psycopg.connect(args.database_url) as conn:
        for q in qs:
            rows = conn.execute(q["gold_sql"]).fetchall()
            got = sorted(str(r[0]) for r in rows) if q["type"] == "set" else rows[0][0]
            ok, detail = score(q, "answer", got if q["type"] != "set" else got)
            if not ok:
                bad += 1
                print(f"DRIFT {q['id']}: {detail}")
    print(f"gold validation: {len(qs) - bad}/{len(qs)} consistent with the database")
    return 1 if bad else 0


async def oracle(args) -> int:
    qs = load_questions(args)
    results = []
    async with McpSession(args.database_url) as m:
        for q in qs:
            if q["type"] == "clarify":
                continue
            sol = q["solution"]
            res = await m.session.call_tool(sol["tool"], sol["args"])
            if is_error(res):
                results.append((q, False, "tool error: " + text_of(res)[:150]))
                continue
            data = json.loads(text_of(res))
            try:
                value = dig(data, sol["path"])
            except (KeyError, IndexError, TypeError, ValueError) as e:
                results.append((q, False, f"extract failed {sol['path']}: {e!r}"))
                continue
            if q["id"] == "D09":  # tool reports metres, the question asks for km
                value = round(float(value) / 1000)
            ok, detail = score(q, "answer", value)
            results.append((q, ok, detail))
    passed = sum(1 for _, ok, _ in results if ok)
    for q, ok, detail in results:
        if not ok:
            print(f"FAIL {q['id']} [{q['category']}]: {detail}")
    print(f"oracle (reference tool calls, no LLM): {passed}/{len(results)} reproduce the gold answers")
    return 0 if passed == len(results) else 1


def mcp_tool_to_openai(t) -> dict:
    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
    return {"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1500], "parameters": schema}}


def chat(client, base_url: str, headers: dict, payload: dict, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            r = client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = repr(e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"chat request failed: {last}")


async def agent(args) -> int:
    import httpx

    qs = load_questions(args)
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif "localhost" not in args.base_url and "127.0.0.1" not in args.base_url:
        print(f"warning: ${args.api_key_env} is not set", file=sys.stderr)
    client = httpx.Client()
    results = []
    async with McpSession(args.database_url) as m:
        tools = [t for t in (await m.session.list_tools()).tools if args.with_render or t.name != "render_map"]
        openai_tools = [mcp_tool_to_openai(t) for t in tools]
        system = (m.instructions + "\n\n" + PROTOCOL).strip()
        for q in qs:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": q["question"]}]
            calls = tokens = 0
            final = ""
            err = None
            t0 = time.time()
            try:
                for _step in range(args.max_steps):
                    payload = {"model": args.model, "messages": msgs, "tools": openai_tools, "temperature": args.temperature}
                    resp = chat(client, args.base_url, headers, payload)
                    tokens += (resp.get("usage") or {}).get("total_tokens", 0)
                    msg = resp["choices"][0]["message"]
                    msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls") and v is not None})
                    tcs = msg.get("tool_calls") or []
                    if not tcs:
                        final = msg.get("content") or ""
                        break
                    for tc in tcs:
                        calls += 1
                        fn = tc["function"]
                        try:
                            targs = json.loads(fn.get("arguments") or "{}")
                            res = await m.session.call_tool(fn["name"], targs)
                            out = ("ERROR: " if is_error(res) else "") + text_of(res)
                        except Exception as e:  # noqa: BLE001
                            out = f"ERROR: {e}"
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out[: args.max_tool_chars]})
                else:
                    err = "max_steps reached"
            except Exception as e:  # noqa: BLE001
                err = repr(e)
            kind, value = parse_final(final)
            ok, detail = score(q, kind, value) if not err or final else (False, err)
            results.append(dict(id=q["id"], category=q["category"], ok=ok, detail=detail, kind=kind, value=value,
                                tool_calls=calls, tokens=tokens, seconds=round(time.time() - t0, 1), error=err))
            print(f"{'PASS' if ok else 'FAIL'} {q['id']} [{q['category']}] calls={calls} {detail[:90]}", flush=True)
    summarize(results, args)
    return 0


def summarize(results: list[dict], args) -> None:
    cats: dict[str, list[dict]] = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r)
    total = sum(r["ok"] for r in results)
    answerable = [r for r in results if r["category"] != "ambiguous"]
    asked = [r for r in results if r["category"] == "ambiguous"]
    print("\n| category | passed | total |\n|---|---|---|")
    for c, rs in cats.items():
        print(f"| {c} | {sum(r['ok'] for r in rs)} | {len(rs)} |")
    print(f"| **all** | **{total}** | **{len(results)}** |")
    if answerable:
        print(f"answerable accuracy: {sum(r['ok'] for r in answerable)}/{len(answerable)}; "
              f"asked-instead-of-answering on answerable: {sum(r['kind'] in ('clarify', 'cannot') for r in answerable)}")
    if asked:
        print(f"ask-when-ambiguous: {sum(r['ok'] for r in asked)}/{len(asked)}")
    print(f"avg tool calls {sum(r['tool_calls'] for r in results) / len(results):.1f}, tokens {sum(r['tokens'] for r in results)}")
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', args.model)}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"model": args.model, "base_url": args.base_url, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved", path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=DEFAULT_DB)
    ap.add_argument("--validate-gold", action="store_true")
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY", help="name of the environment variable holding the API key")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--max-tool-chars", type=int, default=12000)
    ap.add_argument("--with-render", action="store_true", help="also expose render_map (images are not sent back to the model)")
    ap.add_argument("--category", help="comma separated categories")
    ap.add_argument("--ids", help="comma separated question ids")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    if args.validate_gold:
        raise SystemExit(validate_gold(args))
    if args.oracle:
        raise SystemExit(asyncio.run(oracle(args)))
    if not args.model:
        ap.error("--model is required (or use --oracle / --validate-gold)")
    raise SystemExit(asyncio.run(agent(args)))


if __name__ == "__main__":
    main()
