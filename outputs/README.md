# outputs/: what is here and where to start

Everything in this folder was produced by the scripts in `scripts/` and is committed, so the system can be evaluated
without running it. All LLM runs used `gpt-4o-mini`. The numbers below are copied from the `aggregate` block of each
JSON file.

## Start here (what REPORT.md cites)

| What | File | Headline |
|---|---|---|
| Ranking and rating-prediction metrics | `eval/offline_metrics.md` | hybrid NDCG@10 0.129; residual-kNN RMSE 0.871 |
| Content search: embeddings × re-rankers × attributes | `eval/search_eval.md` | tone NDCG@10 0.026 → 0.147 |
| Tone attributes vs user tags | `eval/attributes_eval.md` | recall and lift per attribute |
| Quality floor impact | `eval/quality_floor.md` | shipped floor 2.75: no ranking cost (NDCG@10 unchanged at 0.129) |
| Graph / knowledge-graph experiment | `eval/graph_eval.md` | no gain (noise level) |
| **Held-out conversations, first run (the estimate)** | `eval/scenarios_llm_gpt-4o-mini_heldout.json`, `…_heldout_v2.json`; transcripts in `transcripts_llm_gpt-4o-mini_heldout/`, `…_heldout_v2/` | v1 17/18, v2 14/16 |
| Development suite, 3× per scenario | `eval/scenarios_llm_gpt-4o-mini.json`; `transcripts_llm_gpt-4o-mini/` | 42/42 runs, 57/57 turns |
| Memory suite, 3× per scenario | `eval/scenarios_llm_gpt-4o-mini_memory.json`; `transcripts_llm_gpt-4o-mini_memory/` | 29/30 runs, 92/93 turns (1 checker false alarm) |

Each transcript (`*.md`) shows the user turn, every tool call with its JSON output, the answer, and the verdict of the
automatic checks. Files named `_r1`, `_r2` and `_r3` are repeats of the same scenario.

## Regression runs (after fixes; not estimates)

| Folder | Why it exists |
|---|---|
| `transcripts_llm_gpt-4o-mini_heldout_after_fixes/`, `…_heldout_final/`, `…_heldout_v2_final/` | the held-out sets re-run after the fixes they prompted (v1 18/18, v2 15/16) |
| `transcripts_llm_gpt-4o-mini_lang_prompt/`, `…_memory_lang_prompt/` | re-run after "reply in the user's language" was added to the prompt |
| `transcripts_llm_gpt-4o-mini_http2/` | re-run after the HTTP connection changes, to check for behaviour regressions |
| `transcripts_llm_gpt-4o-mini_memory_recheck_long/`, `…_recheck_liked/` | targeted re-runs of two memory scenarios after checker and argument-coercion fixes |

## Evidence for specific findings

| Folder / file | Finding |
|---|---|
| `transcripts_llm_gpt-4o-mini_toystory_before_fix/` vs `…_after_fix/` | the agent dropped "like Toy Story" from the tool call (0/5), then passed it (5/5) after a schema change |
| `failure_cases/` | saved transcripts for bugs discussed in APPENDIX.md: the anchor being ignored, the judge's false alarm, co-rating described as a theme, a misread fraction field |
| `transcripts_scripted/`, `transcripts_scripted_memory/` | the same suites on hand-written tool plans with no LLM; these validate the checks themselves |
| `eval/latency_probe_*.json`, `eval/connection_phases.json` | latency investigation (DNS on new connections; see APPENDIX.md) |
| `eval/*_run.log` | console logs of the runs above |
