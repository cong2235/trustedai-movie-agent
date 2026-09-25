# Report: [Your Name]

> Setup and code tour are in [SOLUTION.md](SOLUTION.md). Every number in this report is reproducible with the
> scripts in `scripts/`; raw outputs are in `outputs/`.

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
5. *Stateful*: "why that one?" and "three more" refer back to earlier turns.

**Key technical challenges (from the data, not in the abstract)**
- **Item-side sparsity.** About 51% of movies have fewer than 5 ratings, so collaborative filtering is blind to
  half the catalogue. Content (3,200-character plots) has to cover the long tail.
- **Uneven user histories.** Users have 10 to 1,907 ratings (median 56). User 30 has 18, 13 of them 5★
  (mean 4.61). Mean-centring their ratings would turn a 4★ rating into a "dislike".
- **Popularity and exposure bias.** Ratings are missing-not-at-random: a user rates what they chose to watch.
  Offline metrics reward recommending popular titles, and not having rated a movie is not the same as disliking it.
- **Titles are messy.** Examples: "Usual Suspects, The"; remakes (two *12 Angry Men*); mojibake;
  famous titles that are absent (The Matrix). The assistant must resolve titles without guessing.
- **LLM hallucination.** An LLM "knows" movies and will happily recommend ones that are not in the dataset
  or invent statistics. The architecture has to make grounding the easy path.
- **Evaluating a conversation.** No labelled dialogues exist. The evaluation has to be decomposed into parts
  that can each be measured.

## Approach

### How I broke the problem down

The key split is between **reasoning** and **computation**:

```
        LLM (OpenAI or Claude): plans, calls tools, synthesises, explains
          │   └─ after each answer: grounding guardrail (titles + numbers vs tool outputs) → revise once if needed
          │   └─ every turn/tool call → telemetry (SQLite + JSONL) → monitor dashboard with SLO alerts
          │  JSON in / evidence JSON out
 ┌──────────┬─────────────┬───────────────┬──────────────────┬──────────────┐
 profile    rating history  recommend      search              neighbours'    explain_match /
 (genre     (filterable)    (hybrid CF +   (embeddings+TF-IDF  opinion        blind spots
 affinity)                  content +       → LLM re-rank for  (kNN
                            constraints)    tone/structure)    prediction)
```

- **Deterministic tools** do everything numeric: similarity, prediction, ranking, filtering. They are
  unit-tested and evaluated offline without an LLM. Each tool returns *evidence* (which of your ratings,
  which users, what plot excerpt, how confident), not prose.
- **The LLM** chooses and chains tools. For example, "what do people like me think of Inception?" becomes
  resolve title → item-specific neighbourhood → weighted opinion → compare to everyone → your predicted
  rating. The LLM then turns the evidence into an explanation. It never ranks movies itself.
- **Evaluation mirrors the split**: (1) ranking quality of the engine, (2) accuracy of the neighbour-opinion
  predictor, (3) search relevance, (4) conversation-level checks (grounding, constraints, tool use). In production,
  (5) online guardrails and telemetry keep measuring the same things on live traffic.

### Methods and why

| Component | Choice | Why |
|---|---|---|
| User similarity | Pearson on co-rated items × n/(n+10) significance shrinkage, ≥3 overlaps | Removes rater generosity. Shrinkage stops two users with 3 movies in common from looking like twins. |
| Item similarity | Adjusted cosine (user-mean-centred) with co-rater shrinkage | Standard, strong for top-N, and each score decomposes into "because you rated X". |
| Preference weight | (r − 3)/2 (absolute), not r − user mean | Mean-centring would make user 30's 4★ ratings negative. |
| Rating prediction | Bias baseline μ + b_u + b_i plus similarity-weighted neighbour **residuals**, shrunk toward the baseline | v1 (classic Resnick) was worse than the plain bias baseline; see Evaluation. |
| Plot representation | Plots chunked into ~180-word pieces; movie vector = mean of chunks; search also uses the best chunk. Two interchangeable embedders: OpenAI `text-embedding-3-small` (default when a key is set) or local `bge-small-en-v1.5` | Plots exceed a small embedder's window. The best chunk lets a detail deep in the plot match, and gives the agent an excerpt to quote. OpenAI embeddings: +0.05 NDCG on topic queries, ~$0.08 to index everything. |
| Re-ranking | Stage 2 for free-text requests: gpt-4o-mini reads title / genres / tags / plot excerpt for the top 30 and scores fit 0-10, *including tone and structure*; returns `[id, score]` pairs; blended 0.7 / 0.3 with stage 1 | The largest single quality gain in the project (topic NDCG 0.28 → 0.42, tone 0.03 → 0.12; tags hidden from the re-ranker). A local cross-encoder was tried and rejected (no gain, 1.4-1.8 s on CPU). |
| Lexical search | TF-IDF (1-2 grams) over title + genres + tags×3 + plot, weight 0.1 vs 0.9 dense, + 0.35 × quality z | Tags carry precise labels ("twist ending"). The weight 0.1 was measured: at 0.3, title words polluted results ("French *Twist*", "Morons From *Outer Space*"). |
| Blending | Linear blend of z-scored signals (item-kNN, user-kNN, plot-taste, popularity, + a small PureSVD latent-factor term), weights grid-tuned on a validation split | Every weight is inspectable. The latent term raised NDCG@10 in *every* user segment, so I kept it at a small weight. Explanations still cite only the kNN/content evidence (a trade-off discussed under Reflection). |
| Request handling | Two-stage: retrieve 40 candidates by the explicit request (anchor = co-rating + plot + genre-overlap similarity, or description relevance), then re-rank by percentiles, 0.7 request / 0.3 personal taste | Found through failure analysis (the Toy Story case below). Adding z-scores did not work because CF z-scores are heavy-tailed. |
| Diversity | MMR on plot embeddings (λ = 0.8) | Stops a list from being three sequels of one franchise. |
| Agent | 9 tools, manual tool-use loop, parallel tool calls. Provider-agnostic: Claude (`claude-opus-5`, prompt caching, refusal fallback) or OpenAI (`gpt-4o-mini`, used for development and the reported runs) | A manual loop gives the full trace needed for evaluation. The deterministic tools make the model swappable. |

### Alternatives considered and rejected

- **RAG over the dataset / let the LLM recommend from its own knowledge.** Rejected. The questions need
  *computation* over 74k ratings (similarity, aggregation), which an LLM cannot do by reading chunks. It
  would also recommend movies that are not in the catalogue.
- **Matrix factorisation (SVD/ALS) as the *only* serving model.** Alone, PureSVD is about as accurate as
  item-kNN, but a latent factor cannot be explained to a user. It ended up as a *minor* term in the
  blend (see Evaluation), while the explanations come from the kNN/content evidence.
- **Fixed pipeline / intent classifier instead of an agent.** Workable for the 6 sample queries, but brittle
  for compositional questions ("sci-fi before 1970 that people like me rated highly") and follow-ups. The
  deterministic tools already carry the correctness, so the LLM only adds flexibility. I built a
  no-LLM "scripted" mode anyway as a fallback and as a reference trajectory.
- **Tags as a main signal.** Only 1,026 movies have any tag. Tags are used as a high-weight lexical feature
  and as *evaluation labels* for search, never as a requirement.
- **A bigger embedding model / cross-encoder re-ranker.** Better search quality is likely, but CPU build
  time would grow roughly 3-5×. Noted as future work.

### Decision Log

| Decision | Alternative considered | Why I chose this |
|---|---|---|
| **LLM plans and explains; all numbers come from deterministic, tested tools that return evidence JSON** | LLM reasons directly over retrieved data (RAG), or recommends from its own knowledge | Grounding and correctness become testable without the LLM (93 tests, offline metrics). The model is swappable (Claude / OpenAI) because it holds no logic. The LLM cannot invent a rating it was never given, and the conversation suite checks every title it names against the dataset and the tool outputs. |
| **Neighbourhood CF (item-item + user-user) as the backbone, with a small latent-factor term tuned in** | Pure matrix factorisation (PureSVD / ALS), or pure kNN | kNN evidence falls straight out as explanations ("because you rated Aliens 5★", "12 similar users average 4.4★"). Alone, PureSVD ≈ item-kNN (0.109 vs 0.108 NDCG@10), but blended, the latent term added +0.015 NDCG, significant, and better in every user segment. So I kept it, as a ranking term only. |
| **Temporal per-user split + validation-only tuning + breakdown by segment + bootstrap CIs** | Random split, one headline number | Random splits leak future taste. A single average hides exactly where the system fails (sparse users, long-tail items). CIs stop me from claiming a win that is noise. |
| **LLM listwise re-ranker as stage 2 for free-text requests** | Cross-encoder (ms-marco MiniLM), or a bigger embedding model only | Only the LLM re-ranker moved tone/structure queries (≈4.5× NDCG, from a low base). The cross-encoder is trained on web Q&A relevance and did not help. Cost ≈ $0.001 and 2.3 s median per request, so it runs only when there is a description to judge, and falls back to stage 1 on any error. |
| *(bonus)* **Residual kNN with shrinkage for "what do people like me think of X"** | Classic mean-centred kNN (Resnick) | The first version lost to a no-personalisation bias baseline. The fix beats the baseline in every evidence bucket. |

## Evaluation

### How do I know it works? Four layers, each answering a different question

| Layer | Question | Method | Why this method |
|---|---|---|---|
| 1. Ranking engine | Does `recommend_movies` put movies the user will like near the top? | Temporal per-user split (oldest 80% train / newest 20% test), top-10 over the full unseen catalogue, relevant = test rating ≥ 4. Weights tuned on a separate validation slice. 583 users. | Mimics "what next?". Temporal, because random splits leak future taste. Validation-only tuning keeps the test numbers honest. |
| 2. Neighbour-opinion predictor | When the agent says "people like you rate it ~4.2", how accurate is that? | RMSE / MAE / like-accuracy on 14,571 held-out ratings vs. 4 baselines, **bucketed by how many neighbours rated the movie** | This number is shown to users, so its error must be known. The buckets test whether the `reliability` label means anything. |
| 3. Content search | Does "a courtroom drama" find courtroom dramas? | 30 natural-language queries (18 topic + 12 tone/structure), relevance = user tag (e.g. `court`). Tags are hidden from the lexical index **and the re-rankers** to avoid leakage. | The only free relevance labels in the data. Tags are sparse, so precision is a lower bound and only method-vs-method comparisons are meaningful. |
| 4. Conversation | Does the agent pick the right tools *and arguments*, stay grounded and obey constraints? | 14 scenarios / 19 turns (all 6 sample queries × users 1, 15, 30 + edge cases). Automatic checks: tool and argument selection, title grounding, **numeric grounding**, constraints. Plus an LLM judge (1-5 rubric) and my own reading of every transcript | Grounding and constraint-following can be checked mechanically. The judge and my own review cover "is the explanation good". |

Metrics were chosen for what they tell a product owner. **HR@10** ("did at least one of 10 suggestions land?") is the
most interpretable. **NDCG@10** is the ranking-quality headline. **Popularity / tail share / coverage / diversity**
are there because accuracy alone rewards recommending blockbusters.

### 1. Ranking results (test set, 583 users, `outputs/eval/offline_metrics.md`)

| Model | NDCG@10 | HR@10 | P@10 | R@10 | Coverage | Mean log-popularity of recs |
|---|---|---|---|---|---|---|
| Popularity | 0.080 | 0.326 | 0.056 | 0.063 | 1.8% | 5.34 |
| Top-rated (Bayesian avg) | 0.064 | 0.278 | 0.047 | 0.053 | 1.0% | 4.80 |
| Content only (plot embeddings) | 0.010 | 0.082 | 0.009 | 0.007 | 5.7% | 2.07 |
| UserKNN | 0.106 | 0.415 | 0.077 | 0.090 | 3.9% | 5.12 |
| ItemKNN | 0.108 | 0.410 | 0.078 | 0.092 | 8.2% | 4.77 |
| PureSVD (k=50) | 0.109 | 0.453 | 0.072 | 0.109 | 11.6% | 4.62 |
| Hybrid without latent term (tuned) | 0.114 | 0.443 | 0.083 | 0.099 | 6.6% | 4.94 |
| **Hybrid, tuned (shipped blend)** | **0.129** | **0.482** | **0.093** | **0.118** | 5.7% | 4.96 |
| Hybrid tuned + MMR (shipped in the tool) | 0.127 | 0.480 | 0.091 | 0.115 | 5.7% | 4.96 |

Paired bootstrap (2,000 resamples over users), NDCG@10 of the tuned hybrid minus:
**ItemKNN +0.021 [95% CI 0.014, 0.030]**, **PureSVD +0.020 [0.009, 0.031]**, **Popularity +0.049 [0.036, 0.061]**.
All three gains are statistically distinguishable from zero. An earlier version without the latent term was *not*
distinguishable from PureSVD (CI crossed 0). That result is why the term was added.

**Where it works and where it doesn't: by history size**

| Train ratings | Users | Hybrid NDCG@10 | Hybrid HR@10 | Best single baseline |
|---|---|---|---|---|
| < 20 | 107 | 0.115 | 0.34 | **PureSVD 0.122** (beats the hybrid) |
| 20-49 | 190 | 0.107 | 0.43 | PureSVD 0.107 |
| 50-149 | 188 | 0.108 | 0.50 | ItemKNN 0.095 |
| 150+ | 98 | 0.226 | 0.72 | UserKNN 0.204 |

- It works best for rich histories (72% of heavy users get a hit in 10) and beats every baseline in 3 of 4 segments.
- **It fails most for sparse users.** Only 1 in 3 users with fewer than 20 ratings gets a hit, and PureSVD alone is better there.
  Overall, **52% of users get no hit in the top 10**. For context, each user has only a handful of held-out likes, and a movie the user
  would like but never rated counts as a miss (missing-not-at-random). The absolute numbers understate usefulness,
  but the comparisons are fair.
- **The long tail is not solved.** 844 of 6,691 relevant held-out pairs (12.6%) are movies with fewer than 5 training ratings.
  Every CF model has **tail recall ≈ 0**. Content-only puts 43% of its slots on tail movies but still recovers
  only 0.1% of relevant tail items, so plot similarity alone does not predict *which* obscure movie a user will
  rate highly.
- Things I tried that did **not** help, so they were not shipped: per-segment blend weights (validation NDCG 0.1158 vs 0.1152 →
  below my pre-set +0.002 bar; test 0.1265 vs 0.1290 confirms it), and quality as a heavy signal (the grid chose 0).
- MMR diversification lifts intra-list diversity only slightly (0.238 → 0.242) at −0.002 NDCG (noise level). It stays in
  the tool for its qualitative effect: it stops lists of sequels. That is a judgement call, not a measured win.

### 2. "What do people like me think of X?": predictor accuracy (`knn_v1` = my first version)

| Method | RMSE | MAE | Like-accuracy (≥ 4★) |
|---|---|---|---|
| Global mean | 1.054 | 0.828 | 54.1% |
| User mean | 0.962 | 0.747 | 64.8% |
| Bias baseline μ + b_u + b_i | 0.890 | 0.687 | 68.6% |
| kNN v1: classic mean-centred | 0.911 | 0.695 | 69.5% |
| **kNN v2: baseline + shrunk neighbour residuals (shipped)** | **0.871** | **0.667** | **70.4%** |

| Neighbours who rated the movie | n | RMSE bias baseline | RMSE v1 | RMSE v2 |
|---|---|---|---|---|
| 0 (falls back to baseline) | 798 | 1.071 | 1.071 | 1.071 |
| 1-2 | 1,249 | 0.953 | **1.087** | 0.915 |
| 3-9 | 2,854 | 0.921 | 0.952 | 0.899 |
| 10+ | 9,670 | 0.855 | 0.857 | 0.838 |

v1 looked reasonable in demos but was **worse than a non-personalised baseline**, badly so with 1-2 neighbours
(1.09 vs 0.95). v2 beats the baseline in every bucket. Error falls steadily as evidence grows (1.07 → 0.84), which
supports the `reliability` / `evidence_strength` labels the tools attach. The tool now also requires ≥ 10 neighbours
before summarising.

### 3. Content search (`outputs/eval/search_eval.md`)

30 natural-language queries in two sets: **18 topic** queries (what a movie is about: time travel, boxing, the mafia) and
**12 tone/structure** queries (a twist, dark comedy, surreal, funny). Relevance = user tag. Tags are hidden from the
lexical index *and* from the re-rankers to avoid leakage.

| Embeddings | Pipeline | NDCG@10 topic | NDCG@10 tone | P@10 topic | P@10 tone |
|---|---|---|---|---|---|
| bge-small (local) | dense only | 0.167 | 0.000 | 0.161 | 0.000 |
| bge-small | stage 1: 0.9 dense + 0.1 lexical + quality | 0.232 | 0.019 | 0.217 | 0.025 |
| bge-small | stage 1 + cross-encoder | 0.282 | 0.017 | 0.244 | 0.017 |
| bge-small | stage 1 + LLM re-rank | 0.372 | 0.034 | 0.322 | 0.033 |
| OpenAI 3-small | dense only | 0.215 | 0.009 | 0.211 | 0.008 |
| OpenAI 3-small | stage 1 | 0.282 | 0.026 | 0.250 | 0.025 |
| OpenAI 3-small | stage 1 + cross-encoder | 0.289 | 0.022 | 0.261 | 0.017 |
| **OpenAI 3-small** | **stage 1 + LLM re-rank (shipped)** | **0.423** | **0.116** | **0.356** | **0.108** |

Median added latency: cross-encoder 1.4 s (CPU), LLM re-rank 2.3 s (one gpt-4o-mini call, 30 candidates).

> **Correction.** An earlier version of this table reported 0.445 / 0.141 for the shipped row. That run leaked the
> relevance labels: a missing argument let the re-ranker see the user tags. A clean ablation later showed the leak
> was worth only a little (0.445 → 0.422 topic). The bigger drop, to 0.294, came from my own latency change, a
> bare score array. The shipped `[id, score]` format recovers the quality (details in the hardening section of the
> Open Section).

- **Stage 1 matters, re-ranking matters more.** Quality re-ranking and a small lexical weight lift dense-only retrieval.
  At 0.3, lexical weight *hurt*, because words in titles dominated ("French *Twist*"). The LLM re-ranker then
  lifts topic NDCG by 50% and is the only component that moves tone queries at all. For "a thriller with a shocking
  twist ending" it puts The Usual Suspects first, from 0/5 relevant at stage 1. The rest of its top 5 (North by
  Northwest, Rear Window, Vertigo) are suspense classics without a `twist ending` tag, so tone remains the weakest area.
- **OpenAI vs local embeddings:** a consistent but modest gain (+0.05 topic NDCG at stage 1). Most of the difference
  disappears after LLM re-ranking. The local model remains the no-key fallback.
- **Cross-encoder: rejected.** ms-marco is trained on web question-answer relevance and does not transfer to "does this
  movie feel like X".
- **Caveats, stated plainly.** (1) The LLM re-ranker brings world knowledge: it "knows" The Usual Suspects has a twist,
  beyond what the 400-character excerpt says. Tagged movies skew famous, so the eval flatters it; on obscure movies it
  can only use the excerpt. The re-ranker only *orders* candidates and its score is never shown to the user as a fact,
  so this does not break grounding. (2) The stage-1 weights were chosen on the original 18 topic queries, so the topic
  numbers are somewhat optimistic. The tone set and all re-ranker settings were not tuned on these labels. (3) Tone P@10
  of 0.125 is still low in absolute terms, because tags are sparse (most truly funny movies carry no `funny` tag).

### 4. Conversation suite (`outputs/eval/scenarios_*.json`, transcripts in `outputs/transcripts_*`)

14 conversations / 19 turns: all 6 sample queries × users 1, 15, 30, plus edge cases (absent title, ambiguous title,
a sequel, a two-session memory conversation). They are run two ways. **Scripted** executes hand-written reference
tool plans and renders the output with templates (no LLM). It tests the tool layer end-to-end, and every result is
deterministic. **LLM** is the real agent (gpt-4o-mini, temperature 0.2), which must choose tools and argument values
itself and write the answer. It uses the full production stack: OpenAI embeddings, LLM re-ranking, streaming,
history compaction, long-term memory, the online guardrail and telemetry. **Each scenario is run 3×**, so every
figure is a rate over 57 LLM turns, not a single lucky run.

| Check (per turn) | Scripted (19 turns) | LLM agent (57 turns = 19 × 3) |
|---|---|---|
| Turn / scenario pass rate (all checks below) | 19/19 · 14/14 | **57/57 · 42/42** |
| Required tools called | 100% | 100% |
| Argument *values* correct (movie resolves to the intended film, genres, years, anchors) | 100% | 100% |
| Golden set hit (a recommended movie from the hand-made list of good answers) | 100% | 100% |
| Constraint violations (seen, remembered as seen, excluded genre, era, repeats) | 0 | 0 |
| Titles not in the dataset · titles no tool returned | 0 · 0 | 0 · 0 |
| Decimal numbers not in tool outputs · numbers attached to the wrong movie | n/a | 0 · 0 (206 checked) |
| "You rated X N★" claims contradicting the dataset | n/a | 0 (90 checked) |
| Online guardrail revisions | n/a | 1 of 57 (fixed by the revision) |
| Latency p50 / p95 · first streamed token p50 | <1 s | 4.6 s / 16.7 s · 2.0 s |
| LLM judge, mean 1-5: grounded / personalised / explains / honest / helpful | n/a | 4.95 / 4.30 / 4.37 / 4.77 / 4.74 |

How to read this:
- The mechanical checks are the pass/fail signal. They are clean *because* they were used to find and fix problems
  over several runs (history in the Open Section, including a round where the stricter checks first failed 16 of 42
  scenario runs). A clean result is a regression suite. It does not show the agent is flawless, and a different
  phrasing of the same request could still slip.
- What the checks cannot see: qualitative claims ("similar users loved it"), movies written without "(Year)", and
  whether a *different* good movie would have served the user better. Those are left to reading transcripts.
- The judge's lowest scores point at real weaknesses, e.g. search answers that did not tie results to the user's
  history. `search_movies` now attaches per-user evidence (`for_you`) to each result for that reason.

### Qualitative check on the three suggested users (`outputs/transcripts_scripted/`)

| Request | User | Top results | Verdict |
|---|---|---|---|
| "What should I watch tonight?" | 1 (action/comedy, 190 ratings) | The Godfather II, Ferris Bueller's Day Off, Terminator 2, The Godfather, Shawshank | Fits (they gave Terminator / Full Metal Jacket 5★). Very canonical. |
| "Dark psychological thriller with a twist" | 15 | Gaslight, Color of Night, The Machinist, The Game, Shutter Island | 4 of 5 are good. Color of Night (7 ratings, 2.6★) is weak. |
| "Liked Toy Story, tired of animation" | 15 | E.T., Pirates of the Caribbean, The Princess Bride, Big, Willy Wonka | Good *after* a fix (Failure 1). |
| "What should I watch tonight?" | 30 (18 ratings) | Forrest Gump, Pulp Fiction, Saving Private Ryan, Fight Club, Back to the Future | Plausible but generic (Failure 3). |
| "Sci-fi before 1970" | 1 | 2001: A Space Odyssey, Metropolis, Night of the Living Dead, 20,000 Leagues | Night of the Living Dead is labelled Sci-Fi in MovieLens (noisy genre labels). |

### Failure Analysis

**Failure 1: "I liked Toy Story but I'm tired of animated movies" returned The Silence of the Lambs** *(fixed, partly)*
- *Asked:* user 15, sample query 5. *Got (v1):* The Usual Suspects, The Silence of the Lambs, The Princess Bride,
  Indiana Jones, The Godfather II ([before-fix transcript](outputs/failure_cases/before_fix_toy_story_anchor_ignored.md)).
  Nothing Toy-Story-like.
- *Why:* the anchor signal was added as one z-score to a blend of personal z-scores. CF z-scores are heavy-tailed
  (the user's top candidates reach z ≈ 10-14), so "like Toy Story" was drowned out. While fixing it, two more causes
  surfaced. **Plot similarity captures topic, not tone**: Toy Story's nearest non-animated plot is *Child's Play*,
  a killer-doll horror film. **Co-rating similarity is popularity-biased**: The Silence of the Lambs is "similar" to Toy Story
  mainly because everybody rated both.
- *Fix:* a two-stage design. First retrieve the top 40 by the request (co-rating + plot + **genre overlap as a coarse tone
  signal**), then re-rank by percentiles (0.7 request / 0.3 taste). Now: E.T., The Princess Bride, Willy Wonka, Big,
  Pirates of the Caribbean for user 15, and Mary Poppins, Ferris Bueller, Babe, Elf for user 1.
- *It came back at the agent level.* With the engine fixed, the real gpt-4o-mini agent still returned The Silence of
  the Lambs. It called `recommend_movies(exclude_genres=["Animation","Children"])` but **never passed
  `more_like=["Toy Story"]`**, so the fixed feature was never used. My checks did not catch it: constraints were
  respected and every title was grounded. The LLM judge scored the turn 5/5 "grounded". I added an
  `expect_args` check to the suite and measured over 5 runs: **0/5 before**. I then changed the tool description
  ("set this whenever the user names a movie they liked… genre filters alone just return the user's generic
  favourites") and added one prompt line mapping request parts onto arguments: **5/5 after**
  ([before](outputs/transcripts_llm_gpt-4o-mini_toystory_before_fix/) /
  [after](outputs/transcripts_llm_gpt-4o-mini_toystory_after_fix/)).
- *Remaining weakness:* the anchor weights are hand-set, and there is no offline metric for "respects the anchor". The next step is a
  small labelled set of "more like X" queries, plus tone features (see Failure 2).

**Failure 2: tone and structure requests ("a twist", "light and funny")** *(largely fixed by an LLM re-ranker; measured)*
- *Asked:* user 30, "Something light and funny tonight, nothing violent or dark". *Got:* **Punchline (1988)**, a drama about
  a stand-up comedian with 4 ratings averaging 2.5★, and **Funny People (2009)**, a drama about a comedian facing a terminal illness.
- *Why:* the embeddings match what a plot is *about* ("comedians") rather than how it *feels* ("makes me laugh").
  The same mechanism gives P@10 = 0.0 for "twist ending", "surreal", "atmospheric" and "dark comedy" in the search eval.
  A plot summary says what happens. It rarely says "and it's hilarious" or "the ending recontextualises
  everything". Tags would say that, but only 1,026 movies have tags. The quality floor (≥ 3 ratings) was also too
  permissive: 2.5★ from 4 people should not reach the top 5.
- *Fix:* a second stage in which an LLM reads the top 30 candidates (title, genres, tags, plot excerpt) and scores fit,
  explicitly including tone and structure. The same request now returns **Monty Python's The Meaning of Life, Better Off Dead,
  Clueless, White Christmas**, and tone-query NDCG rose 0.026 → 0.116 (search eval above). I tried a cross-encoder
  first and it did not help.
- *Still weak:* one of the five (Calendar Girls, 8 ratings, 2.6★) is a poorly rated film. The re-ranker judges fit, not
  quality, and the rating floor (≥ 3 ratings) is still permissive. Re-ranking also adds 2.3 s median and had a 19 s
  outlier in testing, which the latency SLO on the monitor is there to catch. The cheaper, more durable fix is to run
  the same LLM judgement **once, offline**: extract structured attributes (tone, has-twist, violence) for all 5,135
  movies (~$1), validate them against tags, and use them as filters at no query-time cost.

**Failure 3: sparse users get a generic blockbuster list** *(partly addressed by long-term memory; ranking unchanged)*
- *Asked:* user 30 (18 ratings, 13 of them 5★), "What should I watch tonight?". *Got:* Forrest Gump, Pulp Fiction,
  Saving Private Ryan, Fight Club, Back to the Future. Each is backed by "strong" evidence (10+ similar users, 4.1-4.5★).
- *Why this is a failure even though each item is defensible:* someone whose favourites are Shawshank, Braveheart and
  Star Wars has very likely *already seen* Forrest Gump. Ratings are missing-not-at-random: 18 ratings is how much they
  logged, not how much they watched. Offline, this segment is the weakest (HR@10 0.34 for users with fewer than 20 ratings;
  user 30 gets 0 hits), the recommendations cluster on popular titles (mean log-popularity 4.96, about 140 ratings per
  movie), and PureSVD beats the hybrid here.
- *What was done:* "probably seen" is now a real state. Long-term memory (`remember`) records seen / dismissed /
  disliked movies and avoided genres, and the tools exclude them in every later session. The two-session scenario
  checks this (3/3): "I've seen Forrest Gump… no war movies" → next session, no Forrest Gump and no war films.
- *Still open:* the system waits for the user to volunteer this. Proactive elicitation ("have you seen these three?")
  and feeding answers back as ratings would help more, as would a novelty penalty and a larger latent-factor weight
  for this segment (PureSVD wins here). The offline ranking numbers above are unchanged by the memory feature, since
  the offline eval has no conversation.

## Reflection

**What works well**
- *Grounded by construction.* The LLM never computes. Every number it can cite comes from a tested tool, every tool
  output carries its own evidence (which ratings, which users, which plot excerpt, how reliable), and the suite checks
  every title the model names. Absent titles ("The Matrix") and ambiguous ones ("Star Wars") are handled explicitly.
- *Measured, not assumed.* Three of my first versions looked fine in demos and were wrong when measured: the kNN
  predictor (worse than baseline), lexical-heavy search (worse than dense only), and additive anchors (ignored the
  request). Offline evaluation found or confirmed all three.
- *Explanations people can check.* "You gave Aliens 5★ and people who rated both liked this" is concrete and falsifiable.
- *Swappable model.* The same tools run under Claude or OpenAI with no change, because the model holds no logic.

**What doesn't work well, and why**
- Sparse users and the long tail (Failure 3, tail recall ≈ 0 for every model including the graph ones). These come from
  the data: the system cannot see what people watched but didn't rate. Tone requests are much better with the LLM
  re-ranker, but at a latency and cost price on every such request.
- *Explanation faithfulness is partial.* The blend includes a latent-factor term that cannot be explained, and the
  evidence shown (kNN neighbours, similar plots) is true but is not the complete reason for the rank. I chose accuracy
  (+0.015 NDCG, significant) over full faithfulness. A stricter product could drop the term, or say "also: overall
  rating patterns".
- *Small, weak labels.* The search eval has 30 queries with sparse labels, and the conversation suite has 19 turns (plus 31 memory turns). They are
  good for finding regressions but not for fine comparisons.
- *Data quality.* 198 movies carry another movie's plot (Open Section). I mask them, which removes content signal for
  some important titles (e.g. Twelve Monkeys, Seven).

**With more time or resources**
1. Move the re-ranker's judgement offline: LLM-extracted attributes per movie (tone, twist, violence), validated
   against tags. This removes the ~2.3 s query-time cost of Failure 2's fix.
2. Proactive elicitation on top of the new long-term memory ("seen it? / loved it?"), with the answers fed back into
   CF as ratings. Evaluate with simulated users drawn from held-out ratings.
3. An implicit-feedback model that treats "rated" as "watched" (e.g. EASE or weighted ALS), on the same protocol.
4. Recover the correct plots via `links.csv` (IMDb/TMDB ids) and a plot source, instead of masking.
5. Scale the conversation eval: about 100 synthetic multi-turn dialogues with varied constraints, a *stronger* judge model
   than the agent, and human spot checks to calibrate the judge.
6. Latency: stream the answer, cache per-user signal vectors, run the re-ranker in parallel with the explain calls,
   and alert on the re-rank p95 (already monitored).
7. Production hardening: move telemetry from SQLite to Postgres/OpenTelemetry, add per-user rate limits and a cost budget
   per session, and run the scenario suite in CI on every prompt or tool change (it caught four regressions here).

## Open Section

### Should this use a knowledge graph? (tested, not assumed)

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


### Production layer: guardrails, logs and monitoring

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
3. **Monitoring with SLOs** (`monitor.py`, the Streamlit "Monitor" tab, `scripts/monitor.py`). KPI tiles, one
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

### Hardening round: memory, stricter verification, latency

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
| First request after start-up | CF matrices, SVD and genre stats were built lazily: first `recommend` **17 s** in the container | warm-up at process start (`app/serve.py`, a background thread before the first visitor) | first call 0.1 s |
| Guardrail revisions | checker false positives → 23 extra rounds | fixed checker | 1 revision in 57 turns |
| LLM re-ranker | verbose JSON per candidate, 600-char excerpts | `[id, score]` pairs (a bare array was faster but lost 30% quality, see above), 400-char excerpts, shared client, 8 s timeout with fallback | 3.8 s → 2.3 s median, quality kept |
| Context size | old tool outputs re-sent every turn | compaction + trimmed `recommend` output (−20%: 6.5k → 5.2k chars) + profile in context | 2.0 LLM calls/turn (was 2.2) |
| Perceived wait | answer appeared all at once | token streaming in the UI | first token p50 2.0 s |
| **Remaining tail** | ~4% of gpt-4o-mini calls take **12-15 s** before the first byte vs 0.9 s normally. Not retries (SDK logs: 0), not the network (DNS 0.1 s, TCP 0.03 s, TLS 0.05 s) | tried **request hedging** (duplicate a call with no first chunk after 2.5-4 s) | **no gain** (80 interleaved real calls): the duplicate stalls too. Disabled by default, kept behind `MOVIE_AGENT_HEDGE_AFTER_S` |

The remaining p95 is dominated by that provider-side stall. Options that address it are operational, not code: a
provisioned-throughput deployment (e.g. Azure OpenAI PTU), a different region or provider, or fewer sequential LLM
calls per turn (e.g. a router that answers simple lookups with a single call).

### Memory under stress: a dedicated test suite

`eval/memory_scenarios.py` holds 10 scenarios (31 turns). Each one targets a specific way short- or long-term memory
can fail. Memory state is checked *after every turn*, references to earlier answers must resolve to the right movie,
and forbidden tools, per-call context budgets and cross-user isolation are all checked. The suite runs 3× with the
real agent (`python scripts/run_scenarios.py --mode llm --suite memory --repeat 3`), plus 30 deterministic unit tests
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

**Data quality finding: 198 movies carry the wrong plot.** While checking a search result I noticed *Twelve Monkeys*
matched "a Greek chorus narrates… Oedipus…". That is the plot of Woody Allen's *Mighty Aphrodite*. A scan found
**41 groups (198 movies) sharing byte-identical plots**, including one group of 29 titles. The pattern (same years,
unrelated titles) suggests a broken join upstream. Often the plot belongs to *none* of the group, so I could not safely
reassign them. Instead, `plot_ok = False` excludes them from every content signal, search excerpt and "similar plot"
explanation. Tools flag them `plot_unreliable`, and the system prompt tells the model not to describe those plots.
Without this, the agent would have confidently explained *Twelve Monkeys* using the plot of a romantic comedy.

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

**Reproducibility and cost.** Everything except the LLM transcripts is deterministic and runs on a laptop CPU without API
keys. Plot embedding takes about 29 minutes once. The offline evaluation takes about 5.5 minutes, including a 288-point grid search run
three times. A full LLM suite run (19 turns + judge) with gpt-4o-mini uses about 100k input tokens, roughly $0.02-0.04.
