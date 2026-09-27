# Report: Võ Trần Công

> Setup and code tour: [SOLUTION.md](SOLUTION.md). Engineering history, experiments and every bug found along the way:
> [APPENDIX.md](APPENDIX.md). Every number here is reproducible with the scripts in `scripts/`; raw outputs are in `outputs/`.

## Problem Analysis

**Who uses this and what do they need?** Someone who wants something to watch and does not want to run
the analysis themselves. A search box already answers "find X". What this user needs is an assistant that
(a) knows their taste from their history, (b) can combine evidence the user cannot easily gather, such as
"people who rate like me" or "movies whose plots resemble the ones I loved", and (c) says *why*, so the
user can judge the suggestion rather than trust a black box.

**What makes a good recommendation in a conversation?**
1. *Relevant to this user*, not just popular. The system should also know when it has no basis to be personal.
2. *Grounded*: every claim ("7 similar users gave it 4.4") comes from the data, and every movie exists in the catalogue.
3. *Obeys the request*: "not animated", "before 1970", "like Toy Story" are hard constraints, not hints.
4. *Honest about confidence*: a guess from 2 neighbours is labelled as such.
5. *Stateful*: "why that one?" and "three more" refer back to earlier turns, and "I've seen it" is remembered.

**Key technical challenges (from the data, not in the abstract)**
- **Item-side sparsity.** About 51% of movies have fewer than 5 ratings, so collaborative filtering is blind to
  half the catalogue. Content (3,200-character plots) has to cover the long tail.
- **Uneven user histories.** Users have 10 to 1,907 ratings (median 56). User 30 has 18, 13 of them 5★
  (mean 4.61). Mean-centring their ratings would turn a 4★ rating into a "dislike".
- **Popularity and exposure bias.** Ratings are missing-not-at-random: a user rates what they chose to watch.
  Offline metrics reward popular titles, and not having rated a movie is not the same as disliking it.
- **Titles are messy.** "Usual Suspects, The"; remakes (two *Titanic*s); mojibake; famous titles that are absent
  (The Matrix). The assistant must resolve titles without guessing.
- **Plots describe what happens, not how it feels.** "Light and funny" or "a twist ending" is rarely written in a
  plot summary, and tags that would say it cover only ~1,000 movies.
- **LLM hallucination.** An LLM "knows" movies and will happily recommend ones that are not in the dataset
  or invent statistics. The architecture has to make grounding the easy path.
- **Evaluating a conversation.** No labelled dialogues exist, so the evaluation has to be decomposed into parts
  that can each be measured.

## Approach

### How I broke the problem down

The key split is between **reasoning** and **computation**:

```
        LLM (OpenAI gpt-4o-mini; Claude supported): plans, calls tools, synthesises, explains
          │   └─ after each answer: grounding guardrail (titles + numbers vs tool outputs) → revise once if needed
          │  JSON in / evidence JSON out
 ┌──────────┬─────────────┬────────────────┬──────────────────────┬──────────────┬───────────────┐
 profile    rating history  recommend        search                 neighbours'    explain_match /
 (genre     (filterable)    (hybrid CF +     (embeddings + TF-IDF   opinion        blind spots /
 affinity)                  content +        + tone attributes      (residual kNN  long-term memory
                            constraints)     → LLM re-rank)         prediction)
```

- **Deterministic tools** do everything numeric: similarity, prediction, ranking, filtering. They are
  unit-tested (100 tests) and evaluated offline without an LLM. Each tool returns *evidence* (which of your ratings,
  which users, what plot excerpt, how confident), not prose.
- **The LLM** chooses and chains tools. "What do people like me think of Inception?" becomes resolve title →
  item-specific neighbourhood → weighted opinion → compare to everyone → your predicted rating. The LLM turns the
  evidence into an explanation. It never ranks movies itself.
- **Evaluation mirrors the split**: (1) ranking quality of the engine, (2) accuracy of the neighbour-opinion
  predictor, (3) search relevance, (4) conversation-level checks on a development suite, and (5) a held-out
  conversation set that was fixed before it was run.

### Methods and why

| Component | Choice | Why |
|---|---|---|
| User similarity | Pearson on co-rated items × n/(n+10) significance shrinkage, ≥3 overlaps | Removes rater generosity. Shrinkage stops two users with 3 movies in common from looking like twins. |
| Item similarity | Adjusted cosine (user-mean-centred) with co-rater shrinkage | Standard, strong for top-N, and each score decomposes into "because you rated X". |
| Preference weight | (r − 3)/2 (absolute), not r − user mean | Mean-centring would make user 30's 4★ ratings negative. |
| Rating prediction | Bias baseline μ + b_u + b_i plus similarity-weighted neighbour **residuals**, shrunk toward the baseline | v1 (classic Resnick) was worse than the plain bias baseline; see Evaluation. |
| Plot representation | ~180-word chunks; movie vector = mean of chunks; search also uses the best chunk. OpenAI `text-embedding-3-small` (default with a key) or local `bge-small-en-v1.5` | Plots exceed a small embedder's window. The best chunk lets a detail deep in the plot match and gives the agent an excerpt to quote. |
| Tone attributes | Offline, once per movie: gpt-4o-mini reads the plot (start + ending) and assigns moods from a fixed list of 17, a 0-3 twist grade (a reveal must quote the plot) and a 0-3 violence grade. Tags are never shown to it | Plot embeddings miss tone. Pre-computing it gives tools a cheap signal and a violence *filter*, at ~$0.5 once instead of an LLM call per request. |
| Re-ranking | Stage 2 for free-text requests: gpt-4o-mini scores the top 30 for fit 0-10, *including tone and structure*; `[id, score]` pairs; blended 0.7 / 0.3 with stage 1 | The largest single quality gain (topic NDCG 0.28 → 0.42, tone 0.03 → 0.12). A local cross-encoder was tried and rejected (no gain, 1.4 s on CPU). |
| Lexical search | TF-IDF (1-2 grams) over title + genres + tags×3 + plot, weight 0.1 vs 0.9 dense, + 0.35 × quality z | Tags carry precise labels. At weight 0.3, title words polluted results ("French *Twist*"). |
| Blending | Linear blend of z-scored signals (item-kNN, user-kNN, plot-taste, popularity, + a small PureSVD term), weights grid-tuned on a validation split | Every weight is inspectable. The latent term raised NDCG@10 in every user segment. Explanations still cite only kNN/content evidence (see Reflection). |
| Request handling | Two-stage: retrieve 40 candidates by the explicit request (anchor = co-rating + plot + genre overlap, description relevance, requested attributes), then re-rank by percentiles, 0.7 request / 0.3 taste | Found through failure analysis (Toy Story, below). Adding z-scores did not work because CF z-scores are heavy-tailed. |
| Diversity | MMR on plot embeddings (λ = 0.8) | Stops a list from being three sequels of one franchise. |
| Memory | Short-term: last 2 turns verbatim, older turns compacted to (question, answer). Long-term: per-user SQLite (seen / dismissed / liked / disliked / avoid_genre / preference), *enforced by the tools* | "I've seen it" must survive the session, and a remembered dislike must not depend on the model paying attention. |
| Agent | 12 tools, manual tool-use loop, parallel calls. OpenAI `gpt-4o-mini` for development and every reported run; a Claude backend exists but has not been evaluated | A manual loop gives the full trace needed for evaluation. The deterministic tools make the model swappable. |

### Alternatives considered and rejected

- **RAG over the dataset / let the LLM recommend from its own knowledge.** The questions need *computation* over
  74k ratings (similarity, aggregation), which an LLM cannot do by reading chunks, and it would recommend movies
  that are not in the catalogue.
- **Matrix factorisation as the *only* model.** PureSVD alone is about as accurate as item-kNN, but a latent factor
  cannot be explained to a user. It ended up as a minor term in the blend.
- **Fixed pipeline / intent classifier instead of an agent.** Workable for the 6 sample queries, brittle for
  compositional questions ("sci-fi before 1970 that people like me rated highly") and follow-ups. A no-LLM
  "scripted" mode exists anyway, as a fallback and a reference trajectory.
- **Tags as a main signal.** Only 1,026 movies have any tag. Tags are a high-weight lexical feature and the
  *evaluation labels* for search, never a requirement.
- **A knowledge graph.** Tested rather than assumed: RP3beta with genre and tag nodes, blended into the hybrid,
  moved NDCG@10 by +0.0007 [CI −0.002, +0.004]. The data has no rich relations (cast, director) for a graph to
  exploit ([APPENDIX](APPENDIX.md#should-this-use-a-knowledge-graph-tested-not-assumed)).

### Decision Log

| Decision | Alternative considered | Why I chose this |
|---|---|---|
| **LLM plans and explains; all numbers come from deterministic, tested tools that return evidence JSON** | LLM reasons over retrieved data (RAG), or recommends from its own knowledge | Correctness becomes testable without the LLM (100 tests, offline metrics). The LLM cannot invent a rating it was never given, and every title and number it writes is checked against the tool outputs. |
| **Neighbourhood CF as the backbone, with a small latent-factor term** | Pure matrix factorisation, or pure kNN | kNN evidence falls straight out as explanations ("because you rated Aliens 5★"). Alone, PureSVD ≈ item-kNN (0.109 vs 0.108 NDCG@10); blended, the latent term added +0.015, significant and better in every segment. |
| **Temporal per-user split + validation-only tuning + segments + bootstrap CIs** | Random split, one headline number | Random splits leak future taste. A single average hides exactly where the system fails (sparse users, long tail). CIs stop me from claiming a win that is noise. |
| **Tone as offline attributes *and* an LLM re-ranker, not either alone** | Re-ranker only (the previous design), or attributes only | Attributes alone recover part of the tone gain at no latency; together with the re-ranker they give the best tone NDCG (0.147 vs 0.116). The gain is not yet significant on 12 queries, so both stay, and the attributes also add a violence filter the re-ranker could not provide. |
| **Correct behaviour enforced by tools, not by the prompt** | Prompt instructions | Remembered genre dislikes, "never re-suggest a seen movie", "more like X never returns X" and one-off vs lasting preferences are all checked in code. Prompt-only fixes failed in measured runs (e.g. a stored dislike was ignored in 3/3 runs until the tool enforced it). |

## Evaluation

### How do I know it works? Five layers, each answering a different question

| Layer | Question | Method | Why this method |
|---|---|---|---|
| 1. Ranking engine | Does `recommend_movies` put movies the user will like near the top? | Temporal per-user split (oldest 80% train / newest 20% test), top-10 over the full unseen catalogue, relevant = test rating ≥ 4, weights tuned on a separate validation slice. 583 users | Mimics "what next?". Validation-only tuning keeps the test numbers honest. |
| 2. Neighbour opinion | When the agent says "people like you rate it ~4.2", how accurate is that? | RMSE / MAE / like-accuracy on 14,571 held-out ratings vs 4 baselines, bucketed by how many neighbours rated the movie | This number is shown to users, so its error must be known. |
| 3. Content search | Does "a courtroom drama" find courtroom dramas? Does "a twist ending" find twists? | 18 topic + 12 tone queries, relevance = user tag, tags hidden from the index and the re-rankers | The only free relevance labels in the data. Tags are sparse, so only method-vs-method comparisons mean much. |
| 4. Conversation (development) | Right tools *and argument values*, grounded, constraints obeyed, memory correct? | Main suite (14 conversations / 19 turns) + memory suite (10 / 31), each run 3×; mechanical checks, an LLM judge, and my own reading | Grounding and constraints can be checked mechanically. These suites were used to find bugs, so they are regression tests. |
| 5. Conversation (held-out) | Does it generalise to requests it was not developed on? | 18 new conversations / 20 turns, ten new users, two in Vietnamese; committed to git before the first run and run once | Layer 4's pass rates are inflated by construction. This is the estimate. |

HR@10 ("did at least one of 10 suggestions land?") is the most interpretable ranking metric, NDCG@10 the headline;
popularity, coverage and tail share are there because accuracy alone rewards recommending blockbusters.

### 1. Ranking (test set, 583 users, `outputs/eval/offline_metrics.md`)

| Model | NDCG@10 | HR@10 | P@10 | R@10 | Coverage | Mean log-popularity |
|---|---|---|---|---|---|---|
| Popularity | 0.080 | 0.326 | 0.056 | 0.063 | 1.8% | 5.34 |
| Content only (plot embeddings) | 0.010 | 0.082 | 0.009 | 0.007 | 5.7% | 2.07 |
| UserKNN | 0.106 | 0.415 | 0.077 | 0.090 | 3.9% | 5.12 |
| ItemKNN | 0.108 | 0.410 | 0.078 | 0.092 | 8.2% | 4.77 |
| PureSVD (k=50) | 0.109 | 0.453 | 0.072 | 0.109 | 11.6% | 4.62 |
| **Hybrid, tuned (shipped)** | **0.129** | **0.482** | **0.093** | **0.118** | 5.7% | 4.96 |
| Hybrid + MMR (as served by the tool) | 0.127 | 0.480 | 0.091 | 0.115 | 5.7% | 4.96 |

Paired bootstrap over users, hybrid minus: **ItemKNN +0.021 [0.014, 0.030]**, **PureSVD +0.020 [0.009, 0.031]**,
**Popularity +0.049 [0.036, 0.061]**.

| Train ratings | Users | Hybrid NDCG@10 | Hybrid HR@10 | Best single baseline |
|---|---|---|---|---|
| < 20 | 107 | 0.115 | 0.34 | **PureSVD 0.122** (beats the hybrid) |
| 20-49 | 190 | 0.107 | 0.43 | PureSVD 0.107 |
| 50-149 | 188 | 0.108 | 0.50 | ItemKNN 0.095 |
| 150+ | 98 | 0.226 | 0.72 | UserKNN 0.204 |

- It works best for rich histories (72% of heavy users get a hit in 10) and beats every baseline in 3 of 4 segments.
- **It fails most for sparse users**: 1 in 3 users with fewer than 20 ratings gets a hit, and PureSVD alone is better
  there. Overall, 52% of users get no hit in the top 10. A movie the user would like but never rated counts as a
  miss, so absolute numbers understate usefulness; the comparisons are fair.
- **The long tail is not solved.** 12.6% of relevant held-out pairs are movies with fewer than 5 training ratings, and
  every CF model has tail recall ≈ 0. Content-only puts 43% of its slots on the tail but recovers 0.1% of it.
- **Caveat on the split.** It is temporal *per user*, not a global cut-off date, so training can contain other users'
  ratings made after a test user's test period. This leaks a little "future popularity" into every model equally;
  comparisons are unaffected, absolute numbers are slightly optimistic.
- Tried and not shipped: per-segment blend weights (below my pre-set +0.002 validation bar), heavy quality weighting.

### 2. "What do people like me think of X?": predictor accuracy

| Method | RMSE | MAE | Like-accuracy (≥ 4★) |
|---|---|---|---|
| User mean | 0.962 | 0.747 | 64.8% |
| Bias baseline μ + b_u + b_i | 0.890 | 0.687 | 68.6% |
| kNN v1: classic mean-centred | 0.911 | 0.695 | 69.5% |
| **kNN v2: baseline + shrunk neighbour residuals (shipped)** | **0.871** | **0.667** | **70.4%** |

| Neighbours who rated the movie | n | RMSE baseline | RMSE v1 | RMSE v2 |
|---|---|---|---|---|
| 0 (falls back to baseline) | 798 | 1.071 | 1.071 | 1.071 |
| 1-2 | 1,249 | 0.953 | **1.087** | 0.915 |
| 3-9 | 2,854 | 0.921 | 0.952 | 0.899 |
| 10+ | 9,670 | 0.855 | 0.857 | 0.838 |

v1 looked reasonable in demos but was **worse than a non-personalised baseline**, badly so with 1-2 neighbours. v2
beats the baseline in every bucket, and error falls as evidence grows, which supports the `reliability` labels the
tools attach. The tool requires ≥ 10 neighbours before summarising an opinion.

### 3. Content search (`outputs/eval/search_eval.md`) and tone attributes (`outputs/eval/attributes_eval.md`)

| Embeddings | Pipeline | NDCG@10 topic | NDCG@10 tone | P@10 tone |
|---|---|---|---|---|
| OpenAI 3-small | dense only | 0.215 | 0.009 | 0.008 |
| OpenAI 3-small | stage 1: 0.9 dense + 0.1 lexical + quality | 0.282 | 0.026 | 0.025 |
| OpenAI 3-small | stage 1 + cross-encoder | 0.289 | 0.022 | 0.017 |
| OpenAI 3-small | stage 1 + LLM re-rank | 0.423 | 0.116 | 0.108 |
| OpenAI 3-small | stage 1 + attributes (no LLM at query time) | 0.282 | 0.059 | 0.058 |
| **OpenAI 3-small** | **stage 1 + attributes + LLM re-rank (shipped)** | **0.423** | **0.147** | **0.133** |
| bge-small (local) | stage 1 + LLM re-rank | 0.372 | 0.034 | 0.033 |
| bge-small (local) | stage 1 + attributes + LLM re-rank | 0.372 | 0.113 | 0.108 |

- **Re-ranking matters most**: +50% topic NDCG, and the only large move on tone. The cross-encoder (trained on web
  Q&A relevance) does not transfer to "does this movie feel like X" and was rejected.
- **Attributes help tone, but the evidence is thin.** With OpenAI embeddings, attributes + re-rank vs re-rank alone is
  +0.031 NDCG [95% CI −0.03, +0.10], better on 7 of 12 queries and worse on 4: *not significant*. With the local
  embedder it is +0.079 [+0.02, +0.14]. Attributes alone roughly double stage-1 tone NDCG at no added latency, so they are
  also the fallback when the re-ranker is unavailable. Topic queries request no attributes and are unchanged.
- **The attributes themselves**, checked against tags the extractor never saw: every attribute is over-represented on
  movies tagged with the same concept (lift 2.5-47×; twist grade 3: 10×, dark-comedy: 10×), but recall is low for
  rare moods (surreal 0.12, satirical 0.07) and there are only 5-19 tagged movies per attribute. The 0-3 twist grade
  replaced a yes/no label after pilots marked Sense and Sensibility and Balto as twist endings and flagged 22% of 60 movies.
- **Caveats.** Tone queries are mapped to attributes by hand in this eval (what the agent is meant to pass); whether
  the agent does so is tested in layer 5. The mood list was written knowing the tone queries. The LLM brings world
  knowledge, and tagged movies skew famous, which flatters both LLM components. The shipped re-rank row was earlier
  mis-reported with a label leak (0.445 → 0.423 after the fix; [APPENDIX](APPENDIX.md#hardening-round-memory-stricter-verification-latency)).

### 4. Conversation, development suites (`outputs/eval/scenarios_llm_*.json`, `outputs/transcripts_*`)

The real agent with the full stack (gpt-4o-mini, temperature 0.2, OpenAI embeddings, attributes, LLM re-ranking,
compaction, long-term memory, online guardrail), each scenario run 3× on the final code:

| Per turn | Main suite (57 turns) | Memory suite (93 turns) |
|---|---|---|
| Scenario runs passed · turns passed | 41/42 · 56/57 | 30/30 · 93/93 |
| Required tools called · argument values correct | 100% · 100% | 100% · 100% |
| Golden-set hit (main) / memory state correct after every turn (memory) | 100% | 100% |
| Constraint violations (seen, remembered, genre, era, repeats) | 0 | 0 |
| Titles not in the dataset · titles no tool returned | 0 · 0 | 0 · 0 |
| Decimal numbers ungrounded · misattributed | 0 · 1 of 227 (a checker false alarm, below) | 0 · 0 of 407 |
| "You rated X N★" claims contradicting the data | 0 of 98 | 0 of 184 |
| Latency p50 / p95 · first token p50 | 3.8 s / 8.0 s · 1.9 s | 4.3 s / 15.7 s · 1.9 s |
| LLM judge 1-5: grounded / personalised / explains / honest / helpful | 4.63 / 4.32 / 4.37 / 4.61 / 4.65 | - |

The one failed turn is a false alarm: "*Forrest Gump*, which has an average rating of 4.16" is correct, but the checker
only recognises titles written as "Title (Year)", so it attributed 4.16 to the *Shawshank Redemption (1994)* named just
before. These suites were used to find and fix problems over many runs (a stricter version once failed 16 of 42 runs;
history in the [APPENDIX](APPENDIX.md#hardening-round-memory-stricter-verification-latency)), so a clean result here is a
regression result, not proof the agent is flawless. The judge is gpt-4o-mini grading gpt-4o-mini and was caught wrong
in both directions, so it is used for triage only, never for pass/fail.

### 5. Conversation, held-out set (`eval/heldout_scenarios.py`, `outputs/transcripts_llm_gpt-4o-mini_heldout/`)

Written after development stopped, committed before it ran, run once, and not used to change anything. Users 7, 50,
68, 88, 105, 212, 250, 414, 474 and 599 (25 to 1,907 ratings), new request types, two requests in Vietnamese.

**Result: 17 of 18 conversations and 19 of 20 turns passed the mechanical checks**; 0 hallucinated titles, 0 of 100
decimals and 0 of 31 rating claims wrong; latency p50 4.4 s / p95 16.0 s. What passed and is worth noting: the
ambiguous "Titanic" (1953 and 1997 both exist) was resolved to 1997 and named; "The Force Awakens" (2015, after the
dataset ends) was reported as absent; "remember that I never want horror" in Vietnamese was stored and enforced in the
next session; a one-off request in Vietnamese ("tối nay") was correctly *not* stored, so the English-only regex
backstop was not needed.

What failed or was weak, including problems the checks cannot see (my reading of all 20 transcripts):
- **Failed:** "I've already watched Heat and Casino, so don't suggest them again" → the agent excluded them *for this
  request* but never called `remember`, so a later session could suggest them again.
- **Tone not mapped:** "a mind-bending sci-fi film that isn't too violent" → the agent passed `max_violence` but not
  `moods=["mind-bending"]`, and *Back to the Future* came back as "mind-bending".
- **Quality:** "vài phim hài nhẹ nhàng" (light comedies) → *Maid to Order*, average 1.83★, presented as worth a try.
- **Leaky wording:** *Twelve Monkeys*, whose plot is flagged unreliable, was described from general knowledge, and the
  answer said "although the plot is noted as unreliable" to the user.

So the honest estimate on unseen requests is "correct and grounded almost always; the misses are in *completeness*
(remembering, mapping tone) and *quality floor*, not in hallucination".

### Qualitative check on the three suggested users (`outputs/transcripts_scripted/`)

| Request | User | Top results | Verdict |
|---|---|---|---|
| "What should I watch tonight?" | 1 | The Godfather II, Ferris Bueller's Day Off, Terminator 2, The Godfather, Shawshank | Fits (they gave Terminator / Full Metal Jacket 5★). Very canonical. |
| "Dark psychological thriller with a twist" | 15 | Gaslight, Color of Night, The Machinist, The Game, Shutter Island | 4 of 5 good; Color of Night (7 ratings, 2.6★) is weak. |
| "Liked Toy Story, tired of animation" | 15 | E.T., Pirates of the Caribbean, The Princess Bride, Big, Willy Wonka | Good *after* a fix (Failure 1). |
| "What should I watch tonight?" | 30 | Forrest Gump, Pulp Fiction, Saving Private Ryan, Fight Club, Back to the Future | Plausible but generic (Failure 3). |

### Failure Analysis

**Failure 1: "I liked Toy Story but I'm tired of animated movies" returned The Silence of the Lambs** *(fixed at two layers)*
- *Asked:* user 15, sample query 5. *Got:* The Usual Suspects, The Silence of the Lambs, The Princess Bride, Indiana
  Jones, The Godfather II ([transcript](outputs/failure_cases/before_fix_toy_story_anchor_ignored.md)).
- *Why:* the anchor was one z-score added to a blend of heavy-tailed CF z-scores (top candidates reach z ≈ 10-14), so
  "like Toy Story" was drowned out. Two more causes surfaced: plot similarity captures topic, not tone (Toy Story's
  nearest non-animated plot is *Child's Play*, a killer-doll horror film), and co-rating similarity is
  popularity-biased (everybody rated both Toy Story and The Silence of the Lambs).
- *Fix:* two-stage retrieval by the request (co-rating + plot + genre overlap), then percentile re-ranking 0.7 request /
  0.3 taste. Now: E.T., The Princess Bride, Willy Wonka, Big, Pirates of the Caribbean.
- *It came back at the agent level:* the real agent called `recommend_movies(exclude_genres=[...])` but never passed
  `more_like=["Toy Story"]`, and the judge scored that turn 5/5. An argument-value check measured it (0/5 runs); a
  sharper tool description fixed it (5/5), and the check now runs on every scenario.

**Failure 2: tone and structure requests ("light and funny", "a twist")** *(largely fixed, measured)*
- *Asked:* user 30, "Something light and funny tonight, nothing violent or dark". *Got:* **Punchline** (a drama about a
  stand-up comedian, 4 ratings, 2.5★) and **Funny People** (a comedian facing a terminal illness).
- *Why:* embeddings match what a plot is *about* ("comedians"), not how it *feels*. The same mechanism gave P@10 = 0 for
  "twist ending", "surreal" and "dark comedy".
- *Fix, in two steps:* an LLM re-ranker that judges fit including tone (tone NDCG 0.026 → 0.116), then offline tone
  attributes with a violence filter (→ 0.147, not yet significant). The request now returns Monty Python's The Meaning
  of Life, Better Off Dead, Clueless, White Christmas.
- *Still weak:* the re-ranker judges fit, not quality, so poorly rated films still slip in (Calendar Girls, 2.6★; *Maid
  to Order*, 1.83★, in the held-out set). The agent does not always map tone words onto the attribute arguments
  (held-out "mind-bending"). Rare moods have low attribute recall.

**Failure 3: sparse users get a generic blockbuster list** *(partly addressed by long-term memory)*
- *Asked:* user 30 (18 ratings), "What should I watch tonight?". *Got:* Forrest Gump, Pulp Fiction, Saving Private Ryan,
  Fight Club, Back to the Future, each backed by "strong" evidence (10+ similar users, 4.1-4.5★).
- *Why it is a failure anyway:* someone whose favourites are Shawshank, Braveheart and Star Wars has very likely *seen*
  Forrest Gump. 18 ratings is how much they logged, not how much they watched. Offline this segment is the weakest
  (HR@10 0.34) and PureSVD beats the hybrid there.
- *What was done:* "seen" is a real, remembered state that the tools enforce in every later session (3/3 in the
  two-session scenario). *Still open:* the system waits for the user to volunteer it, and the held-out set shows the
  agent sometimes does not store it even when told (Heat / Casino). Proactive elicitation ("seen these three?") and a
  larger latent-factor weight for sparse users are the next steps.

## Reflection

**What works well**
- *Grounded by construction.* The LLM never computes. Across 170 final-run turns, 0 hallucinated titles, 0 wrong claims
  about the user's ratings, and one flagged number that turned out to be correct.
- *Measured, not assumed.* Several first versions looked fine in demos and were wrong when measured: the kNN predictor
  (worse than baseline), lexical-heavy search, additive anchors, a latency optimisation that cost 30% re-rank quality,
  and my own guardrail's false alarms.
- *Correct behaviour in code.* Memory rules, exclusions and argument coercion live in the tools, so they hold even when
  the model is careless.
- *Explanations people can check.* "You gave Aliens 5★ and people who rated both liked this" is concrete and falsifiable.

**What doesn't work well, and why**
- *Sparse users and the long tail* (Failure 3; tail recall ≈ 0 for every model, graph ones included). The data cannot
  show what people watched but did not rate.
- *Completeness of tool use on new phrasings*: the held-out misses are the model not calling `remember` or not passing
  `moods`. Tool-enforced rules only help once the tool is called.
- *Quality floor on tone requests*: fit is judged, quality is not.
- *Explanation faithfulness is partial.* The PureSVD term influences rank but is never cited. I chose accuracy
  (+0.015 NDCG, significant) over full faithfulness.
- *Small, weak labels*: 30 search queries with sparse tag labels, 5-19 tagged movies per attribute, 20 held-out turns.
  Good for spotting regressions and large effects, not for fine comparisons.
- *Only one model evaluated.* Every reported run uses gpt-4o-mini. The Claude backend is implemented but untested.
- *Data quality*: 198 movies carry another movie's plot; they are masked, which removes content signal for some
  important titles (Twelve Monkeys, Seven).

**With more time or resources**
1. Close the held-out gaps in the tool contract: when `exclude_titles` comes from "I've seen X", have
   `recommend_movies` return a hint (or store it) so memory does not depend on a second call; map tone words to
   `moods` server-side from the free-text query as a fallback. Then write a *new* held-out set, since this one is spent.
2. A quality floor for tone requests (a minimum Bayesian average unless the user asks for obscure films).
3. Proactive elicitation on top of long-term memory ("seen it? / loved it?"), fed back into CF as ratings, evaluated
   with simulated users drawn from held-out ratings.
4. An implicit-feedback model that treats "rated" as "watched" (EASE or weighted ALS), and a global-time split.
5. Recover the correct plots via `links.csv` (IMDb/TMDB ids) instead of masking them.
6. A larger conversation eval (~100 synthetic multi-turn dialogues), a *stronger* judge than the agent, calibrated
   on ~50 human-labelled turns, and the same suites run for the Claude backend.

## Open Section

- **Knowledge graph: tested, not assumed.** RP3beta on the rating graph, with and without genre/tag nodes, scored
  below the hybrid (−0.030 NDCG), and blending it in gave +0.0007 [−0.002, +0.004]. A graph earns its keep with rich
  relations (cast, director, franchise), which this data lacks. [Details](APPENDIX.md#should-this-use-a-knowledge-graph-tested-not-assumed).
- **Data-quality finding.** *Twelve Monkeys* matched a search for "a Greek chorus narrates…", which is the plot of
  *Mighty Aphrodite*. A scan found 41 groups (198 movies) sharing byte-identical plots, likely a broken upstream join.
  They are excluded from every content signal and flagged to the model. [Details](APPENDIX.md#data-quality-198-movies-carry-the-wrong-plot).
- **Production layer.** The grounding checker from the eval also runs on every live answer (one automatic revision
  round), every turn and tool call is logged to SQLite + JSONL, and a Streamlit dashboard and CLI report latency,
  cost, guardrail rate and user feedback against seven SLOs. [Details](APPENDIX.md#production-layer-guardrails-logs-and-monitoring).
- **Evaluating the evaluator.** My checkers had bugs too: false alarms from a greedy regex, a year parser that read
  "2001: A Space Odyssey" as 2001, a literal backspace in a regex, a label leak in the search eval. Every flag was read
  by hand before it counted, and each checker bug has a regression test built from the sentence that triggered it.
  [Details](APPENDIX.md#bugs-in-the-evaluation-tooling-and-why-the-llm-judge-is-not-trusted).
- **Cost.** Agent turns cost about $0.0015 each (a full 3× main-suite run is about $0.1), the attribute extraction about $0.5 once, the
  embedding index $0.08 once. Everything except the LLM transcripts and LLM re-rank rows is deterministic and runs on a
  laptop CPU without an API key.
