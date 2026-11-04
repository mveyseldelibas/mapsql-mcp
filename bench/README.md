# mapsql-bench

60 English GIS questions over the demo database (`demo/build_demo_db.py`), with gold answers, plus a runner that
tests an LLM in an agent loop against the real `mapsql-mcp` tools.

| category | n | examples |
|---|---|---|
| attribute | 13 | "How many pharmacies are in the Ankara POI data?" |
| distance | 10 | "How many cafes are within 300 m of Kizilay Square (lat, lon)?", nearest hospital, city-to-city distance |
| spatial | 8 | points in a park, country containing a point, rivers x countries, touching neighbourhoods |
| aggregate | 10 | population sums, geodesic areas / lengths, "which continent has the highest GDP?" |
| join | 6 | "Which neighbourhood contains the most pharmacies?" |
| buffer | 3 | buffer areas, distance to a polygon |
| bbox | 2 | extremes of a layer |
| ambiguous | 8 | "Find the pharmacies nearby." - the right answer is a **clarifying question** |

Files: `questions_src.py` (source of truth: question, `gold_sql`, one reference tool call) -> `make_gold.py` ->
`questions.json` (with gold values) -> `run_bench.py`.

## Protocol

The model gets the server instructions plus: finish with one line `ANSWER: <value>` (number, name or JSON list),
`CLARIFY: <question>` (information missing - do not guess) or `CANNOT_ANSWER: <why>`.
Numbers are scored with a tolerance, names as case/accent-insensitive sets/strings; for *ambiguous* questions any
`CLARIFY`/`CANNOT_ANSWER` passes and an `ANSWER` fails. The runner also reports how often a model *asked* when it could
have answered, tool calls per question and tokens.

## Modes

```bash
python bench/run_bench.py --validate-gold      # gold == database? (data drift)
python bench/run_bench.py --oracle             # reference tool calls reproduce the gold? (toolset coverage) -> 52/52
python bench/run_bench.py --model M [--base-url URL] [--api-key-env ENV] [--category distance,join] [--ids A01,D03]
```

Any OpenAI-compatible `/chat/completions` endpoint with tool calling works (OpenAI, OpenRouter, Together, vLLM,
Ollama, LM Studio). Results are written to `bench/results/<model>_<timestamp>.json`. `render_map` is not offered unless
`--with-render` is set (images cannot be returned through plain chat-completions tool messages).

The demo database is built from a pinned snapshot, so gold answers are reproducible. Use `build_demo_db.py --refresh`
only if you intend to regenerate the gold file.
