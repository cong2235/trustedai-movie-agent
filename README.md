# Movie Discovery Agent

[![CI](https://github.com/cong2235/trustedai-movie-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/cong2235/trustedai-movie-agent/actions/workflows/ci.yml)

A conversational assistant that investigates the MovieLens data on the user's behalf. An LLM plans which analyses to
run and explains the answer; every number and title comes from deterministic, tested Python tools (collaborative
filtering, plot search, taste profiles, neighbour opinions, long-term memory). Built for the TrustedAI AI Engineer test
([ASSIGNMENT.md](ASSIGNMENT.md)).

```
user ─► chat (Streamlit / CLI) ─► LLM agent ──► 12 tools, JSON evidence out ─► grounding check on every answer
                                     │            ├─ hybrid recommender: item/user kNN + plot taste + PureSVD, tuned
                                     │            ├─ search: plot embeddings + TF-IDF + tone attributes → LLM re-rank
                                     │            ├─ "people like me": residual kNN prediction with reliability labels
                                     │            └─ memory: seen / dismissed / avoided genres, enforced by the tools
                                     └─ telemetry (SQLite + JSONL) → monitoring dashboard with SLO alerts
```

## Results at a glance

| What | Result | Details |
|---|---|---|
| Ranking, 583 users, temporal split | NDCG@10 **0.129** vs 0.109 PureSVD, 0.108 item-kNN (both gains significant) | [REPORT §1](REPORT.md#1-ranking-test-set-583-users-outputsevaloffline_metricsmd) |
| "What do people like me think of X?" | RMSE 0.871 vs 0.890 bias baseline, better in every evidence bucket | [REPORT §2](REPORT.md#2-what-do-people-like-me-think-of-x-predictor-accuracy) |
| Search, tone queries | NDCG@10 0.026 → **0.147** with LLM re-rank + tone attributes | [REPORT §3](REPORT.md#3-content-search-outputsevalsearch_evalmd-and-tone-attributes-outputsevalattributes_evalmd) |
| Held-out conversations (committed before their first run) | v1 **17/18**, v2 **14/16** on first run; 0 hallucinated titles, 0 wrong numbers | [REPORT §5](REPORT.md#5-conversation-held-out-sets-evalheldout_scenariospy-evalheldout_v2_scenariospy) |
| Where it fails | Sparse users, the long tail, mapping tone words onto the right arguments | [Failure analysis](REPORT.md#failure-analysis) |

## Quickstart

Python 3.10+. The dataset and the LLM-extracted movie attributes are in the repo; plot embeddings are built once.

```bash
pip install -r requirements.txt && pip install -e . --no-deps
cp .env.example .env                                      # add OPENAI_API_KEY (or ANTHROPIC_API_KEY)
python scripts/build_index.py --backend openai-3-small    # ~2 min, ~$0.08 (or --backend bge-small: local, ~29 min)

python app/serve.py                                       # web UI: chat + monitor at http://localhost:8501
python -m movie_agent.cli --user 15                       # terminal chat
python -m movie_agent.cli --user 15 --no-llm              # tools only, no API key
```

Development: `pip install -r requirements-dev.txt`, then `pytest` (113 offline tests, no API key) and `ruff check .`.
CI runs both on Python 3.10 and 3.12. `pre-commit install` enables the same checks locally.

## Documentation

| Document | For |
|---|---|
| [REPORT.md](REPORT.md) | The write-up: problem analysis, approach, evaluation, failure analysis, reflection |
| [SOLUTION.md](SOLUTION.md) | Setup, configuration, code tour, how to reproduce every evaluation |
| [APPENDIX.md](APPENDIX.md) | Engineering history: experiments, bugs found and how they were fixed |
| [ASSIGNMENT.md](ASSIGNMENT.md) | The original problem statement and dataset description |

## Layout

```
src/movie_agent/   the system: data → CF / content / attributes → tools → agent → guardrails, telemetry
app/               Streamlit UI (chat + monitoring)
scripts/           index build, attribute extraction, offline / search / graph evaluation, scenario runner, monitor
eval/              conversation suites: main, memory, held-out
tests/             offline unit and integration tests
outputs/           every metric, transcript and failure case referenced in the report
data/              MovieLens subset (as provided) and derived/movie_attributes.jsonl
```

Author: Võ Trần Công.
