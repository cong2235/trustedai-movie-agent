# Movie Discovery Agent: setup and code tour

A conversational assistant that **investigates the MovieLens data on the user's behalf**. An LLM (OpenAI or Claude)
plans which analyses to run and calls deterministic Python tools for collaborative filtering, plot search with
re-ranking, taste profiling and neighbour opinions. It answers with explanations grounded in the returned numbers
and titles. Every answer is re-checked against the evidence, every turn is logged, and a dashboard monitors quality,
latency and cost. The full write-up is in [REPORT.md](REPORT.md), with supporting detail in [APPENDIX.md](APPENDIX.md).

```
 user ──► Chat (web UI or CLI)
             ▼
   agent.py        LLM tool-use loop (OpenAI gpt-4o-mini, streamed | Claude); short-term memory =
     │  │              conversation, compacted to (question, answer) after 2 turns; profile + memories in context
     │  ├─ guardrails.py   after every answer: titles exist and came from tools? numbers found in tool outputs
     │  │                  *for that movie*? "you rated X N★" true in the data? → one automatic revision round
     │  ├─ memory.py       long-term per-user memory (SQLite): preferences, seen / dismissed / liked movies
     │  └─ telemetry.py    turn + tool-call records → logs/telemetry.db (SQLite) + logs/app.jsonl
     ▼
   tools.py        12 tools, JSON evidence out; titles fuzzy-resolved; absent/ambiguous titles reported
     ├─ profiles.py     taste profile, genre affinity vs population, blind spots
     ├─ cf.py           user-user Pearson (shrunk), item-item adjusted cosine, residual kNN prediction, PureSVD
     ├─ recommender.py  z-scored blend (tuned on validation) + constraints + request-first 2-stage + MMR + evidence
     ├─ content.py      chunked plot embeddings + TF-IDF   ◄── embedders.py  OpenAI 3-small | local bge-small
     ├─ attributes.py   offline tone attributes per movie (moods, twist 0-3, violence 0-3) from data/derived/
     ├─ rerank.py       stage-2 re-ranker: LLM listwise (tone/structure aware) | cross-encoder | none
     └─ graph.py        RP3beta ± knowledge nodes (evaluated, not shipped: see REPORT, "knowledge graph")
   monitor.py      KPIs + SLO alerts from telemetry  ──► dashboard "Monitor" tab, scripts/monitor.py
```

## Setup

Requires Python 3.10+ (developed on 3.14, Windows, CPU only; CI tests 3.10 and 3.12 on Linux).

```bash
pip install -r requirements.txt && pip install -e . --no-deps   # exact versions used: requirements-lock.txt
cp .env.example .env                       # add OPENAI_API_KEY (and/or ANTHROPIC_API_KEY)
python scripts/verify_dataset.py           # sanity-check the data
python scripts/build_index.py --backend openai-3-small   # ~2 min, ~$0.08  -> artifacts/openai-3-small/
python scripts/build_index.py --backend bge-small        # optional local fallback, ~29 min CPU
pip install -r requirements-dev.txt       # pytest, coverage, ruff, pre-commit
python -m pytest -q                        # 130 tests, offline (no API calls)
ruff check . && ruff format --check .      # the same checks CI runs
```

## Run it

```bash
python app/server.py                        # web UI (warms models at start): Chat + Monitor  → http://localhost:8501
docker compose up -d --build                # same, in a container (see Dockerfile / docker-compose.yml)
python -m movie_agent.cli --user 15                       # terminal chat; /trace /good /bad /stats
python -m movie_agent.cli --user 15 --no-llm              # tools only, no API key
python scripts/monitor.py --hours 24        # health report + SLO alerts (exit code 1 on breach, cron-friendly)
```

Configuration (environment or `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `MOVIE_AGENT_PROVIDER` | auto from keys | `openai` or `anthropic` |
| `MOVIE_AGENT_MODEL` | `gpt-4o-mini` / `claude-opus-5` | agent model |
| `MOVIE_AGENT_EMBEDDINGS` | `auto` | `openai-3-small` if key + index exist, else `bge-small` |
| `MOVIE_AGENT_RERANKER` | `llm` | `llm`, `cross` or `none` (`llm` falls back to `cross` without an OpenAI key) |
| `MOVIE_AGENT_TELEMETRY` | `1` | `0` disables logging |
| `MOVIE_AGENT_MEMORY` | `1` | `0` disables long-term memory (stored in `data_store/memory.db`) |
| `MOVIE_AGENT_HEDGE_AFTER_S` | `0` (off) | duplicate an LLM call with no first chunk after N s; measured no gain here, see REPORT |
| `MOVIE_AGENT_FALLBACKS` | `1` | Claude only: server-side refusal fallback |

## Reproduce the evaluation

```bash
python scripts/evaluate_offline.py     # recommender + rating predictor (temporal split, tuned on validation)
python scripts/evaluate_search.py      # embeddings × re-rankers on topic and tone queries
python scripts/extract_attributes.py   # one-off: LLM tone attributes per movie -> data/derived/ (committed, ~$0.5)
python scripts/evaluate_attributes.py  # attributes vs user tags (recall / lift)
python scripts/evaluate_quality_floor.py  # ranking cost of the default quality floor
python scripts/evaluate_graph.py       # RP3beta / knowledge-graph experiment
python scripts/run_scenarios.py --mode scripted            # conversation suite, no LLM
python scripts/run_scenarios.py --mode llm --judge         # real agent + checks + LLM judge (also fills telemetry)
python scripts/run_scenarios.py --mode llm --suite heldout # held-out set v1 (fixed before its first run)
python scripts/run_scenarios.py --mode llm --suite heldout2 # held-out set v2
python scripts/run_scenarios.py --mode llm --only u15_toy_story_no_animation --repeat 5   # variance of one case
```

All results land in `outputs/eval/` (markdown + JSON + CSV) and `outputs/transcripts_*`.

## Observability

| Signal | Where | SLO / alert |
|---|---|---|
| Latency p50 / p95 per turn | `turns.latency_ms` | p95 ≤ 20 s |
| Tool error rate, per-tool p95 | `tool_calls` | ≤ 10% |
| Guardrail trigger rate (hallucination proxy) and revision fix rate | `turns.guardrail_*`, `revised` | trigger ≤ 10%, unfixed ≤ 2% |
| Tokens and cost per turn / total | `turns.*_tokens`, `cost_usd` | ≤ $0.05 per turn |
| Re-ranker latency and errors | `tool_calls.rerank_*` | errors ≤ 5% |
| User feedback | `turns.feedback` (👍/👎, CLI `/good` `/bad`) | thumbs-up ≥ 70% |

`logs/app.jsonl` has the same events as one JSON object per line, for log shipping.

## External APIs and cost

* **OpenAI** (used for all reported runs): `gpt-4o-mini` for the agent, re-ranker and judge, and `text-embedding-3-small`
  for embeddings. Typical cost is about $0.002 per turn, and the full evaluation cost about $0.30 in total.
* **Anthropic** (optional, **not run live**): Claude as the agent (`claude-opus-5` by default), with prompt caching and a
  server-side refusal fallback. No Anthropic key was available, so this path is verified only offline, against a fake
  client (`tests/test_anthropic_backend.py`: request shape, tool-result pairing, a full agent turn, refusal, fallback
  switch-off). All reported numbers come from `gpt-4o-mini`.
* Offline paths (`--no-llm`, `bge-small`, `MOVIE_AGENT_RERANKER=none`) need no key. Everything in `outputs/` except
  `transcripts_llm_*` and the LLM re-rank rows is deterministic.

## Layout

| Path | What |
|---|---|
| `src/movie_agent/` | the system (data → signals → tools → agent → guardrails/telemetry → UI) |
| `app/server.py`, `app/chat.html` | web UI: FastAPI server and the chat + monitor page |
| `scripts/` | index build, offline / search / graph eval, scenario runner, monitor CLI |
| `eval/scenarios.py` | 14 conversations / 19 turns with automatic checks and reference tool plans |
| `eval/memory_scenarios.py` | 10 conversations / 31 turns targeting short- and long-term memory |
| `eval/heldout_scenarios.py`, `eval/heldout_v2_scenarios.py` | held-out sets (18 / 20 and 16 / 19 conversations / turns), each committed before its first run |
| `data/derived/` | LLM-extracted movie attributes (committed, so nobody pays for them twice) |
| `tests/` | 130 tests (incl. 29 memory, 6 attribute, 12 search tests on a synthetic index, 5 Claude-backend tests on a fake client, 5 web-server tests): title resolution (sequels, numeric titles, fragments), similarity, constraints, split, metrics, guardrails (attribution, rating claims, revision loop), telemetry, memory, compaction, streaming, re-rank fallback, graph |
| `outputs/eval/` | all metrics |
| `outputs/transcripts_*` | full conversations with every tool call and output |
| `outputs/failure_cases/` | saved evidence for failures discussed in the report |
| `logs/` | telemetry DB, JSONL event log, re-rank cache (git-ignored) |
| `data_store/` | long-term user memory `memory.db` (git-ignored; mounted as a volume in Docker) |
