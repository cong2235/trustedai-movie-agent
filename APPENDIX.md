# Appendix: engineering history and supporting detail

Written by Võ Trần Công. This appendix holds the detail behind [REPORT.md](REPORT.md): the experiments, the bugs found
along the way and how each was fixed. The report summarises it; nothing here is needed to follow the report's argument.
Numbers in this appendix describe the run they were measured in. The final numbers are the ones in the report.

- [Should this use a knowledge graph?](#should-this-use-a-knowledge-graph-tested-not-assumed)
- [Production layer: guardrails, logs and monitoring](#production-layer-guardrails-logs-and-monitoring)
- [Hardening round: memory, stricter verification, latency](#hardening-round-memory-stricter-verification-latency)
- [Memory under stress: a dedicated test suite](#memory-under-stress-a-dedicated-test-suite)
- [Data quality: 198 movies carry the wrong plot](#data-quality-198-movies-carry-the-wrong-plot)
- [What running the real LLM found](#what-running-the-real-llm-found)
- [Bugs in the evaluation tooling, and why the LLM judge is not trusted](#bugs-in-the-evaluation-tooling-and-why-the-llm-judge-is-not-trusted)
- [Reproducibility and cost](#reproducibility-and-cost)
- [Live-use review: three Vietnamese questions (user 23)](#live-use-review-three-vietnamese-questions-user-23)
- [Latency root cause: DNS on every new connection](#latency-root-cause-dns-on-every-new-connection)

## Should this use a knowledge graph? (tested, not assumed)

**Short answer: not with this data. It was measured, and the result is at noise level.**

The data *is* already a graph. Users and movies form a bipartite rating graph, and movies link to genres and tags. Item-kNN is a
2-hop walk on that graph (movie → users → movie). The question is whether modelling it *explicitly* as a (knowledge)
graph adds anything. I tested the strongest simple graph recommender, and a version enriched with knowledge nodes, on the
same temporal split, tuned on validation (`scripts/evaluate_graph.py`, `outputs/eval/graph_eval.md`):

| Model (test, 583 users) | NDCG@10 | HR@10 | Long-tail recall | Δ NDCG vs shipped hybrid [95% CI] |
|---|---|---|---|---|
| RP3beta, a random walk on the rating graph | 0.0996 | 0.398 | 0 | −0.030 [−0.041, −0.018] |
| RP3beta + knowledge nodes (19 genres + 386 tags as graph nodes) | 0.1005 | 0.401 | 0 | −0.029 [−0.041, −0.017] |
| Shipped hybrid | 0.1292 | 0.482 | 0 | - |
| Hybrid + RP3beta-KG signal (weight 0.25, chosen on validation) | 0.1299 | 0.485 | 0 | +0.0007 [−0.002, +0.004] |

Why it doesn't help here:
- **The knowledge in this dataset is thin.** It has 19 coarse genres (Drama alone links 2,300 movies, so walks through it are
  almost uniform) and tags on only about 1,000 movies. A KG earns its keep through rich, specific relations (director,
  cast, writer, franchise, "based on"), and those are not in the data.
- **It does not reach the long tail.** The hope was that movie → tag → movie paths would surface rarely-rated
  movies. Tail recall stayed at 0, because the tail movies are also the untagged ones.
- **The signal is redundant.** Its useful part (co-rating structure) is already captured by item-kNN and PureSVD.

When it *would* be worth it: (1) with enrichment via `links.csv` → TMDB/Wikidata (cast, director, franchise,
keywords). Then "movies by the director of Inception, but lighter" becomes a graph query, and path-based explanations
("same screenwriter as a movie you rated 5★") become possible. (2) for multi-hop questions a vector index cannot answer.
Even then, I would first add those entities as **attributes and filters on the existing tools**, measure, and only move
to a graph store (Neo4j / GraphRAG) if multi-hop queries turn out to matter. Given these results, adding graph
infrastructure now would increase complexity without improving quality.


## Production layer: guardrails, logs and monitoring

The offline evaluation shows the system *can* work. In production, the question is whether it *keeps* working, and
you need to notice when it doesn't. Three additions:

1. **Online grounding guardrail** (`guardrails.py`). This is the same checker the scenario suite uses, now run on *every*
   live answer. It asks three questions: does every "Title (Year)" exist in the dataset, did a tool return it, and does
   every decimal number appear in a tool output (±0.05, or ×100 for percentages)? On failure, the model gets one
   revision round with the specific problems listed; the result is re-checked, and both outcomes are logged. A unit
   test drives this loop with a scripted backend that invents "The Matrix (1999), avg 4.9" and checks that the revision
   removes it and that telemetry records `revised=1, issues_after_revision=0`. Writing that test exposed a bug in my
   own checker: numbers at the end of a sentence ("avg 4.9.") were silently skipped by the regex. So the numeric
   check had been weaker than reported until then. After the fix, the conversation suite was re-run and the numbers below come from that run.
2. **Structured telemetry** (`telemetry.py`). Every turn and every tool call is written to SQLite (queried by the
   dashboard) and to a JSONL event log (for log shipping). A record holds latency, LLM calls, tokens, cost, tool
   errors, re-ranker kind/latency/errors, guardrail result, and user 👍/👎. The schema maps 1:1 onto OpenTelemetry
   spans (turn = root span, tool call = child) if this were deployed.
3. **Monitoring with SLOs** (`monitor.py`, the web UI "Monitor" tab, `scripts/monitor.py`). KPI tiles, one
   single-measure chart per metric over time (no dual axes), a per-tool latency/error table, recent guardrail
   triggers and the latest turns. Seven SLOs produce alerts: p95 latency ≤ 20 s, tool errors ≤ 10%, guardrail
   triggers ≤ 10%, unfixed guardrail ≤ 2%, re-rank errors ≤ 5%, cost ≤ $0.05 per turn, thumbs-up ≥ 70%.
   `scripts/monitor.py --json` exits non-zero on a breach, so a cron job or CI step can page on it.

Live numbers from the final 16-turn run (`python scripts/monitor.py`, telemetry DB reset beforehand, re-rank cache cold):

| KPI | Value | SLO |
|---|---|---|
| Latency p50 / p95 | 4.3 s / 9.0 s | p95 ≤ 20 s ✅ |
| LLM calls / tool calls per turn | 2.2 / 1.2 | - |
| Tool error rate | 5.3% (the single error is the expected "The Matrix is not in the dataset") | ≤ 10% ✅ |
| Guardrail trigger rate | 0% (0 of 16 answers needed revision) | ≤ 10% ✅ |
| Tokens per turn (in / out) | 7,027 / 297 | - |
| Cost per turn / total | $0.0012 / $0.020 | ≤ $0.05 ✅ |
| Re-ranker p95 (live calls, cache hits excluded) | 2.7 s | - |
| Slowest tool | `search_movies` 3.2 s (the LLM re-rank is inside it); `recommend_movies` p95 1.2 s | - |

A zero guardrail rate on 16 scripted turns does not show the guardrail is unnecessary. It shows the tool-level fixes
(explicit counts, suppressed predictions) removed the drift seen in earlier runs. The guardrail stays in place as the
safety net for the phrasings this suite doesn't cover. One monitoring bug surfaced while collecting these numbers:
re-rank cache hits were recorded as 4 ms "calls", which flattered the latency SLO. They are now tagged `llm-cache`,
excluded from the latency percentile and reported as a separate cache-hit rate.

Why these metrics: the guardrail trigger rate is the only *online* hallucination signal that needs no labels.
Latency p95 (not the mean) captures what users actually experience with multi-call agents; the 19 s re-rank outlier
seen during testing is exactly what the mean would hide. Cost per turn is a leading indicator of context bloat in long
sessions, since input tokens grow with every turn.

## Hardening round: memory, stricter verification, latency

An audit asked two questions: *does the agent have short- and long-term memory?* and *are the scenario results and
their evidence actually verified?* Answering them honestly turned up real bugs.

**Bugs found and fixed (each now has a regression test)**

| Bug | Impact | Fix |
|---|---|---|
| `"Terminator 2"` resolved to *The Terminator (1984)* (score 92, above the confidence bar); same for Godfather 2, Alien 3, "Ocean's 12" → *Twelve Monkeys* | wrong movie answered **silently** | Sequel markers normalised (II→2, "twelve"→12, ³→3, "part" dropped). A number in the query must appear in the title |
| `"Oldboy"` → *Boy (2010)*, `"The Goonies"` → *Goon (2011)*, `"Solaris 1972"` → the 2002 remake | silent wrong movie | Penalty when a title is only a fragment of the query. A stated year that matches nothing is no longer "confident", so the agent asks |
| Digit strings were always read as movie ids: `"2012"` → *Lethal Weapon 2*, `"21"` → id 21 | 14 numeric titles (21, 54, 187, 1408, 2012, …) could not be looked up | An exact title match wins over the id reading |
| **Evaluation leak:** the LLM re-ranker saw the user tags during the search eval (a missing argument), and the tags are the relevance labels | re-rank gains overstated (clean ablation: topic NDCG 0.445 → 0.422, tone 0.141 → 0.117) | Tags hidden from the re-ranker in eval. All re-rank numbers re-measured |
| **My own latency optimisation cut re-rank quality by ~30%.** To save output tokens I switched the re-ranker to a bare score array in candidate order | topic NDCG 0.422 → 0.294, tone 0.117 → 0.067: the model loses its place in a list of 30 unlabeled numbers | Found only because the leak fix forced a re-measure. Ablation of 3 formats; shipped `[id, score]` pairs: 0.429 / 0.124 at 2.3 s (vs 3.8 s originally). The final re-measure in the search table gives 0.423 / 0.116; the gap is LLM run-to-run variance |
| `explain` still returned a fraction field (`share_rating_4_plus`), the same kind the model had misread before | risk of inverted claims | Replaced with explicit counts |

**Memory**

- *Short-term (the conversation):* the last 2 turns are kept verbatim, including tool calls and outputs. Older turns
  are compacted to (question, final answer), because old tool outputs were most of the context. A profile summary
  and the user's long-term memories are injected into context, so "what should I watch" no longer needs a
  `get_user_profile` round trip.
- *Long-term (across sessions, new):* a per-user SQLite store (`memory.py`) and three tools (`remember`,
  `list_memories`, `forget_memory`). Kinds: seen / dismissed / disliked / liked / preference / **avoid_genre**. This is
  the proposed fix for Failure 3: once a user says "I've seen Forrest Gump", it is never recommended again. The UI
  sidebar shows and deletes memories.
- *Design lesson (measured):* in the first 3× run the model **stored** "doesn't like war movies" as free text but
  did not apply it in the next session, and recommended *Saving Private Ryan* (0/3). Genre dislikes are now structured
  (`avoid_genre`) and enforced **by the tools**, not by the model's attention: 3/3 afterwards. The same principle as
  before: make the correct behaviour the only available behaviour.

**Verification: what the scenario checks now cover**

The earlier suite checked tool names, title existence and "is this number somewhere in the tool outputs". The new
checks (shared by the offline suite and the **online** guardrail):
- **Argument values:** `expect_args` on every scenario. The movie argument must *resolve to the intended film*,
  genres/years must match, and `more_like` must contain the anchor.
- **Per-movie attribution:** a number must belong to the movie its paragraph or list item is about (the bold/heading
  title), not merely exist somewhere.
- **Claims about the user, checked against the dataset:** "you rated X N★", including integers, which the
  decimal-only check had missed.
- **Golden sets** for the three scenarios with a clear right answer type (20 dark psychological thrillers,
  21 family films, 12 pre-1970 sci-fi). They catch answers wrong in kind, not different good picks.
- **Repeats:** every scenario is run 3× (57 turns), so results are pass *rates*, not anecdotes.
- Two new scenarios: *Terminator 2* (sequel regression) and a *two-session memory* conversation.

**The checker had bugs too, and they cost latency.** The first 3× run with the stricter checks reported 44
misattributed numbers and 9 wrong rating claims. Reading every flagged sentence showed that **all 9 rating
"errors" were false positives**:
- "a user similar *to you* rated it 4.5" was read as the user's own rating;
- "you rated *Terminator 2* and *Alien* highly" was parsed as rating Alien "2";
- a predicted rating later in the same clause was taken for the user's rating.

The attribution rule ("last movie before the number") was also wrong for sentences like "**Fight Club** - you rated
*Star Wars* 5★ and similar users gave it 4.9". Those false alarms triggered 23 needless revision rounds. Revised turns
had p50 9.4 s vs 5.0 s for the rest. The fixed checker has regression tests built from those exact sentences, plus
true positives it must still catch.

| 3× run, gpt-4o-mini, 57 turns | Stricter checks, checker v1 | **All fixes** |
|---|---|---|
| Scenario pass rate (42 scenario runs) | 26/42 | **42/42** |
| Turn pass rate | 38/57 | **57/57** |
| Flagged claims (misattributed / wrong rating / ungrounded) | 44 / 9 / 1 | **0 / 0 / 0** (206 decimals, 90 rating claims checked) |
| Arguments correct · golden hit · constraint violations | 94.7% · 100% · 3 | **100% · 100% · 0** |
| Online guardrail revisions | 23 | **1** (fixed on revision) |
| Memory across sessions | 0/3 | **3/3** |
| Latency p50 / p95 per turn | 5.6 s / 28.1 s | **4.6 s / 16.7 s** |
| Time to first streamed token (p50) | not measured | **2.0 s** |
| Cost per turn | - | $0.0012 |

**Latency: would a GPU help? No, and here is the evidence.** In the current configuration torch is not on the
request path: embeddings and re-ranking are API calls, and CF is numpy/scipy. Where the time went, and what was done:

| Where | Finding | Action | Effect |
|---|---|---|---|
| First request after start-up | CF matrices, SVD and genre stats were built lazily: first `recommend` **17 s** in the container | warm-up at process start (`app/server.py`, a background thread before the first visitor) | first call 0.1 s |
| Guardrail revisions | checker false positives → 23 extra rounds | fixed checker | 1 revision in 57 turns |
| LLM re-ranker | verbose JSON per candidate, 600-char excerpts | `[id, score]` pairs (a bare array was faster but lost 30% quality, see above), 400-char excerpts, shared client, 8 s timeout with fallback | 3.8 s → 2.3 s median, quality kept |
| Context size | old tool outputs re-sent every turn | compaction + trimmed `recommend` output (−20%: 6.5k → 5.2k chars) + profile in context | 2.0 LLM calls/turn (was 2.2) |
| Perceived wait | answer appeared all at once | token streaming in the UI | first token p50 2.0 s |
| **Remaining tail** | ~4% of gpt-4o-mini calls take **12-15 s** before the first byte vs 0.9 s normally. Not retries (SDK logs: 0), not the network (DNS 0.1 s, TCP 0.03 s, TLS 0.05 s) | tried **request hedging** (duplicate a call with no first chunk after 2.5-4 s) | **no gain** (80 interleaved real calls): the duplicate stalls too. Disabled by default, kept behind `MOVIE_AGENT_HEDGE_AFTER_S` |

**Correction (later):** this conclusion was wrong. The stall was not on the provider's side and the network
measurement above timed a warm, cached lookup. The cause was DNS on new connections, which is also why the
hedged duplicate (a separate pool, so another new connection) stalled too. See
[Latency root cause](#latency-root-cause-dns-on-every-new-connection).

## Memory under stress: a dedicated test suite

`eval/memory_scenarios.py` holds 10 scenarios (31 turns). Each one targets a specific way short- or long-term memory
can fail. Memory state is checked *after every turn*, references to earlier answers must resolve to the right movie,
and forbidden tools, per-call context budgets and cross-user isolation are all checked. The suite runs 3× with the
real agent (`python scripts/run_scenarios.py --mode llm --suite memory --repeat 3`), plus 28 deterministic unit tests
(`tests/test_memory.py`). Separately, `scripts/audit_answers.py` re-derives every average, count and "you rated"
claim from `ratings.csv` itself, which catches errors that grounding-against-tool-outputs cannot.

| Scenario | First 3× run | Final 3× run |
|---|---|---|
| "Why the 2nd movie of your *first* list?" after that turn was compacted | 3/3 | 3/3 |
| Constraints carried over ("three more"), then one lifted | **0/3** (repeats, off-by-one year) | 3/3 |
| 8-turn conversation: bounded context, recall of the first suggestion | 3/3 | 3/3 |
| Store a genre dislike → enforced → retract it → allowed again | **0/3** | 3/3 |
| "Just for tonight, no comedies" must not be remembered | **0/3** | 3/3 |
| Remembered dislike vs explicit "a war movie, just this once" | **1/3** | 3/3 |
| Dismissed movie never re-suggested; other users unaffected | 3/3 | 3/3 |
| "Something like the movie I loved last time" (and don't re-suggest it) | 3/3 | 3/3 (+5/5 recheck) |
| "I've seen Star Wars" (6 films): never store a wrong movie | 3/3 | 3/3 |
| A movie remembered as seen can still be discussed | 3/3 | 3/3 |
| **Total** | 22/30 | **30/30 scenario runs, 93/93 turns** |

Re-run on the final code (after the `scope` parameter on `remember` and the movie attributes were added): again
30/30 scenario runs and 93/93 turns, with 0 of 407 decimals and 0 of 184 rating claims wrong. A later regression run
(after the `already_seen` and quality-floor fixes) is the one committed in
`outputs/eval/scenarios_llm_gpt-4o-mini_memory.json`: **29/30 · 92/93**. The one failure is a checker false alarm
(numbers in `lt_recall_liked` attributed to a bold evidence title), read by hand and explained in REPORT §4.

Final run: 0 hallucinated or ungrounded titles, 0 of 381 decimals ungrounded or misattributed, 0 of 201 rating claims
wrong, 0 constraint violations, 0 guardrail revisions. The independent dataset audit found 0 discrepancies in
109 claims. Latency p50 4.7 s, first token p50 2.1 s.

**Real bugs this suite found, and where they were fixed.** Each fix is in code, with a regression test; prompt-only
fixes were not accepted.

| Bug (observed behaviour) | Fix |
|---|---|
| "Give me three more" → the model set `allow_repeats=true` (3/3) and re-suggested earlier picks | flag removed from the model-facing schema |
| "After 2000" → `min_year=2000` (2/3) | inclusive semantics spelled out in the schema ("after 2000 → 2001") |
| "More like X" returned **X itself** whenever the user hadn't rated X | anchors always excluded |
| A "liked" (= watched) movie could be recommended again | `liked` added to the excluded kinds |
| Saying "I've seen Forrest Gump" twice stored it twice: SQLite unique indexes treat NULLs as distinct | expression index on COALESCEd columns + migration that dedupes old stores |
| "Forget that I avoid horror" → the model *re-stored* avoid_genre=Horror while telling the user it had removed it | `forget_memory` by content (no id needed) + a guard that rejects storing a preference in a retraction message |
| "Just for tonight, no comedies" was stored permanently (3/3) | one-off guard: the tool sees the user's message and refuses to store it |
| `include_genres=[War]` sent together with `exclude_genres=[War]` → empty or wrong results | an explicit include wins over any exclude of the same genre |
| `more_like="The Machinist (2004)"` sent as a **string** (5 of 10 calls) → iterated character by character → nonsense recommendations | schema-driven argument coercion in the tool dispatcher (string→list, "3"→3, "true"→True) |
| A 2-user neighbourhood average presented as the movie's "average rating" (5.0 vs 4.03) | tool returns both averages under explicit names + prompt rule "say whose number it is" |

**And bugs in my own verification code**, which is why every flag was read by hand before it counted:
- the guardrail misattributed numbers in nested bullets and treated bold *evidence* titles as the subject;
- it rejected 13-word titles (*Dr. Strangelove…*) as hallucinations;
- the context budget summed all calls of a turn instead of measuring each call;
- a patch had written a literal backspace character into a regex, so the independent audit's subject rule never matched.

All of these produced false alarms, not missed errors, and each is now covered by a test built from the real
sentence that triggered it.

## Data quality: 198 movies carry the wrong plot

**Data quality finding: 198 movies carry the wrong plot.** While checking a search result I noticed *Twelve Monkeys*
matched "a Greek chorus narrates… Oedipus…". That is the plot of Woody Allen's *Mighty Aphrodite*. A scan found
**41 groups (198 movies) sharing byte-identical plots**, including one group of 29 titles. The pattern (same years,
unrelated titles) suggests a broken join upstream. Often the plot belongs to *none* of the group, so I could not safely
reassign them. Instead, `plot_ok = False` excludes them from every content signal, search excerpt and "similar plot"
explanation. Tools flag them `plot_unreliable`, and the system prompt tells the model not to describe those plots.
Without this, the agent would have confidently explained *Twelve Monkeys* using the plot of a romantic comedy.

## What running the real LLM found

**What running the real LLM found (3 full runs + targeted repeats, gpt-4o-mini).** Each problem was fixed at the layer
that caused it, and each fix is locked in by a check:

| Run | What went wrong | Caught by | Fix (layer) |
|---|---|---|---|
| 1 | Told user 1 "predicted rating 4.6" for Pulp Fiction, which they had rated 3.0 | reading transcripts | tool never predicts for rated movies + regression test (tool) |
| 1 | Asked for only 5 neighbours to summarise an opinion | reading traces | tool enforces ≥ 10 (tool) |
| 1 | Described co-rating evidence ("people who rated Alien also rated Shutter Island") as "thematic and stylistic similarity" | reading transcripts | prompt: say what each evidence type means (prompt) |
| 2 | "None of the similar users rated it below 2.5", when the tool said `share_2_5_or_less: 0.1` (10% did) | **new numeric-grounding check** | fraction fields replaced by explicit counts `n_rated_2_5_or_lower` (tool) |
| 3 | Dropped "like Toy Story" when calling the tool (0/5 runs) | **new `expect_args` check** | tool description + prompt → 5/5 (tool schema) |
| 1-3 | First search of a session took ~60 s (embedder lazy-loaded) | latency column | warm-up at start-up (infra) |

Two of these fixes were in the *tool contract* rather than the prompt. A field name that invites misreading
(`share_2_5_or_less`) is a tool-design bug, and making the correct behaviour the only available behaviour is more
robust than asking the model to be careful.

## Bugs in the evaluation tooling, and why the LLM judge is not trusted

**The evaluation tooling was also wrong at first.** My first grounding checker used a greedy regex and flagged real
movies as hallucinated, and the title parser read "2001: A Space Odyssey (1968)" as the year 2001. Both were fixed (exact
match on the longest title suffix; last-year-wins). I mention it because an evaluation that raises false alarms is as
misleading as one that misses real failures.

**Why not trust the LLM judge?** Here the judge is gpt-4o-mini judging gpt-4o-mini, and I caught it being wrong in
both directions. *False alarms:* it claimed numbers "do not match the tool outputs" when all of them matched
([evidence](outputs/failure_cases/run1_judge_false_alarm_numbers_were_correct.md)). It called The Princess Bride
"animated". It scored the correct answer "The Matrix is not in the dataset" 1/5 on grounding. *Misses:* it gave 5/5 to the
Toy Story turn that ignored the user's anchor. So pass/fail comes from the mechanical checks (title grounding,
numeric grounding, constraints, tool + argument selection) and my own reading of every transcript. The judge's
"biggest weakness" sentence is useful for triage. For a real eval I would use a stronger judge model than the agent
and calibrate it on ~50 human-labelled turns.

## Reproducibility and cost

**Reproducibility and cost.** Everything except the LLM transcripts is deterministic and runs on a laptop CPU without API
keys. Plot embedding takes about 29 minutes once. The offline evaluation takes about 5.5 minutes, including a 288-point grid search run
three times. A full LLM suite run (19 turns + judge) with gpt-4o-mini uses about 100k input tokens, roughly $0.02-0.04.

## Live-use review: three Vietnamese questions (user 23)

A manual session in the web UI ("gợi ý cho tôi vài phim hành động" → "gợi ý thêm cho tôi 3 phim nữa" → "có phim nào
mới hơn không") was replayed from telemetry and checked against the data. Every title and number was correct and no
movie was repeated. What was wrong, and what changed:

| Finding | Fix | Layer |
|---|---|---|
| For "3 more", the model passed its own earlier picks as `already_seen`, which wrote 8 movies the user never said they had watched into long-term memory | movies suggested in the session are excluded but never stored from `already_seen`; the bad rows were removed (DB backed up first) | tool |
| Answers in English to Vietnamese questions | "reply in the language of the user's latest message" | prompt |
| Co-rating evidence described as shared themes ("aligns with its themes") | explicit forbidden phrasing in the evidence rule | prompt |
| Everyone's average presented as the similar users' | field-by-field "whose number" rule | prompt |
| Kick-Ass / Django (predicted 3.5) presented like the 4-star picks | new `expected_fit` field ("uncertain match: say so") next to `evidence_strength`, which counts evidence rather than judging it | tool |
| "After 2010" for `min_year=2010` | "describe filters exactly as applied" | prompt |
| 14.6 s turn | 10.5 s before the first chunk; diagnosed afterwards as a DNS stall on a new connection (next section) | connection pool |

Replaying after the fixes: Vietnamese answers, memory untouched, numbers attributed correctly. gpt-4o-mini still
sometimes ignores `expected_fit` and once read "newer" as `max_year=2014`, so the prompt rules are not a complete fix.
gpt-4.1-mini on the same conversation (2 runs) followed intent and format rules better but cited almost no numbers and
described plots from its own knowledge without saying so; gpt-4o-mini stays the default until a model change is made
on the full suites.

The regression runs exposed two more guardrail false alarms, both fixed with tests built from the exact sentences:
naming an absent title with its year ("I couldn't find The Matrix (1999)") after a tool had reported it absent, and
numbers in a block without a bold subject when the answer is about a single recommended movie. Main suite after all
changes: 14/14 conversations, 19/19 turns, 0 guardrail revisions; memory suite 10/10.

## Latency root cause: DNS on every new connection

The review above led to a proper look at the latency tail. Telemetry over 882 turns: 89% of turn time is LLM calls
(tools 4%); time to first chunk had p50 0.9 s but p95 12 s, and 8.7% of calls stalled for a near-constant ~12 s.

| Step | Evidence | Script |
|---|---|---|
| Server or client? | A slow call waited 16.8 s for headers while the server reported 633 ms of processing (`openai-processing-ms`) | `scripts/probe_llm_latency.py` |
| Which phase? | Every slow request spent 11.1 s in `connect_tcp` (normally 0.03 s); TLS, send and server time were normal | `scripts/probe_connection_phases.py` |
| Why connect? | `connect_tcp` includes the DNS lookup. On this machine a VMware adapter (VMnet8) lists a DNS server, 192.168.218.4, that never answers; Windows sometimes asks it first and falls back to 8.8.8.8 after the timeout. api.openai.com has a 10-36 s TTL, so almost every new connection needs a fresh lookup | `Resolve-DnsName` per server |
| Why a new connection? | The SDK pool drops idle connections after 5 s; each client (agent, embedder, re-ranker, every session) had its own pool; and over HTTP/1.1 the SDK closes a streamed response right after `[DONE]`, before the chunked body ends, so a streamed call's connection is never reusable | pool trace |

Fix (`src/movie_agent/http.py`): one shared pool for every OpenAI client, idle keep-alive 300 s
(`MOVIE_AGENT_HTTP_KEEPALIVE_S`), and HTTP/2 (`httpx[http2]`), where closing a stream early does not close the
connection.

| Measurement | Before | After |
|---|---|---|
| Interleaved A/B, 40 requests per pool, 20 s idle between each pool's requests: new connections | 40/40 | 1/40 |
| Same: requests stalling > 5 s | 14/40 (35%) | 0/40 |
| Same: time to headers p50 / p95 | 0.93 s / 12.1 s | 0.71 s / 0.88 s |
| 3 conversations x 3 turns, 20 s idle between turns: turn latency p50 / max | 6.1 s / 16.6 s | 5.7 s / 7.4 s (one connection throughout) |
| Main suite, 19 turns: turn latency p50 / p95 | 4.8 s / 14.9 s | 4.6 s / 7.2 s |

What is left: the first call after more than 5 minutes idle (or after the server drops the connection) still opens a
connection and can hit the slow resolver. That part is a property of this machine, not of the app: removing the dead
DNS server from the VMnet8 adapter (or raising that adapter's interface metric) fixes it for every program. In a
normal server or container deployment the resolver would not have this problem, and the pool fix still saves the
TCP + TLS handshake (~80 ms) on most calls. The hedging code in `agent.py` is now known to have targeted the wrong
cause; it stays disabled.
