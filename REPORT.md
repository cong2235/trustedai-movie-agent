# Report: Võ Trần Công

> Setup and code overview: [SOLUTION.md](SOLUTION.md). Engineering history, experiments and the defects identified during
> development: [APPENDIX.md](APPENDIX.md). All figures in this report can be reproduced with the scripts in `scripts/`;
> the raw outputs are in `outputs/` (see [outputs/README.md](outputs/README.md) for a reading guide).

## Summary

- **Design.** The language model is restricted to planning and explanation. Every figure and title in an answer is
  produced by deterministic, tested tools (collaborative filtering, plot search, taste profiles and neighbour
  opinions). Each answer is verified against the tool outputs and the dataset before it is shown to the user.
- **Recommendation quality.** The hybrid recommender reaches NDCG@10 of 0.129 on a per-user temporal split of 583 users,
  compared with 0.109 for PureSVD and 0.108 for item-kNN; both differences are statistically significant. The
  "similar users" predictor outperforms a bias baseline in every evidence bucket (RMSE 0.871 against 0.890).
- **Conversations.** Two held-out conversation sets were committed to version control before their first run. On that
  first run, 17 of 18 and 14 of 16 conversations passed, with no fabricated titles, no incorrect figures and no
  incorrect statements about the user's own ratings.
- **Limitations.** Users with short rating histories receive predominantly popular titles (one in three obtains a hit in
  the top ten). No model, including the graph-based models, recovers long-tail items. The most frequent remaining
  conversational error is the mapping of tone descriptors (for example "atmospheric") onto the correct tool argument.
- **Principal lesson.** Several initial versions appeared correct in demonstrations but proved deficient under
  measurement: a predictor weaker than its baseline, an evaluation affected by label leakage, and a latency
  optimisation that reduced search quality by 30%. Each is documented below together with its correction.

**Scope.** The work substantially exceeded the suggested three to four hours. The components requested by the brief
are covered in the sections *Approach* through *Failure Analysis*: the tools, the agent, the offline and
conversational evaluation, and the failure analysis. The remaining components were added subsequently, while the
system was being tested as a product: long-term memory, the online grounding check, telemetry and monitoring, the web
interface, containerisation, continuous integration and the latency investigation. These are described in the Open
Section and in APPENDIX.md and may be disregarded when assessing the core submission.

## Problem Analysis

**Users and their needs.** The intended user wishes to select a film without performing the analysis personally. A
search function already answers requests of the form "find X". This user instead requires an assistant that
(a) infers their taste from their rating history, (b) combines evidence that the user cannot readily assemble, such
as "the opinion of people who rate as I do" or "films whose plots resemble those I rated highly", and (c) explains
its reasoning, so that the user can assess a suggestion rather than accept it without justification.

**Characteristics of a good conversational recommendation.**
1. *Personal relevance* rather than general popularity; the system should also recognise when it lacks the evidence
   to personalise.
2. *Grounding*: every statement (for example "seven similar users rated it 4.4") is derived from the data, and every
   film exists in the catalogue.
3. *Compliance with the request*: expressions such as "not animated", "before 1970" or "similar to Toy Story" are
   hard constraints rather than hints.
4. *Calibrated confidence*: a conclusion drawn from two neighbours is identified as such.
5. *Statefulness*: follow-up requests ("why that one?", "three more") refer to earlier turns, and a statement such as
   "I have seen it" is retained.

**Principal technical challenges, as observed in the data**
- **Item-side sparsity.** Approximately 51% of the films have fewer than five ratings, so collaborative filtering
  provides no signal for half of the catalogue. Content (plots averaging 3,200 characters) must cover the long tail.
- **Uneven user histories.** Users have between 10 and 1,907 ratings (median 56). User 30 has 18 ratings, 13 of which
  are five stars (mean 4.61); mean-centring would therefore convert a four-star rating into a negative preference.
- **Popularity and exposure bias.** Ratings are missing not at random, since users rate what they chose to watch.
  Offline metrics consequently favour popular titles, and the absence of a rating does not indicate dislike.
- **Inconsistent titles.** The data contains inverted articles ("Usual Suspects, The"), remakes (two films titled
  *Titanic*), encoding errors, and well-known titles that are absent (*The Matrix*). Titles must be resolved without
  guessing.
- **Plots describe events rather than tone.** Descriptors such as "light and funny" or "a twist ending" rarely appear
  in a plot summary, and the user tags that would express them cover only about 1,000 films.
- **Hallucination.** A language model has prior knowledge of films and may recommend titles that are absent from the
  dataset or state fabricated statistics. The architecture must make grounded answers the default.
- **Evaluation of a conversational system.** No labelled dialogues are available, so the evaluation must be
  decomposed into components that can each be measured.

## Approach

### How I broke the problem down

The central separation is between **reasoning** and **computation**:

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

- **Deterministic tools** perform all numerical work: similarity, prediction, ranking and filtering. They are covered
  by unit tests (130 tests, executed in continuous integration) and evaluated offline without a language model. Each
  tool returns *evidence* (which of the user's ratings, which users, which plot excerpt, and with what confidence)
  rather than prose.
- **The language model** selects and sequences the tools. The question "What do people like me think of Inception?"
  is decomposed into title resolution, an item-specific neighbourhood, a weighted opinion, a comparison with the
  overall rating and a predicted rating for the user. The model converts this evidence into an explanation; it never
  ranks films itself.
- **The evaluation mirrors this separation**: (1) ranking quality of the engine, (2) accuracy of the neighbour-opinion
  predictor, (3) search relevance, (4) conversation-level checks on development suites, and (5) held-out conversation
  sets fixed before they were run.

### Methods and why

| Component | Choice | Rationale |
|---|---|---|
| User similarity | Pearson correlation on co-rated items × n/(n+10) significance shrinkage, minimum of three overlaps | Removes differences in rater generosity; shrinkage prevents two users with three films in common from appearing identical. |
| Item similarity | Adjusted cosine (user-mean-centred) with co-rater shrinkage | A standard, strong top-N method whose scores decompose into statements of the form "because you rated X". |
| Preference weight | (r − 3)/2 on the absolute scale rather than r − user mean | Mean-centring would render the four-star ratings of user 30 negative. |
| Rating prediction | Bias baseline μ + b_u + b_i plus similarity-weighted neighbour **residuals**, shrunk toward the baseline | The first version (classic Resnick) performed worse than the plain bias baseline; see Evaluation. |
| Plot representation | Chunks of about 180 words; the film vector is the mean of its chunks, and search also uses the best-matching chunk. OpenAI `text-embedding-3-small` (default when a key is available) or the local `bge-small-en-v1.5` | Plots exceed the context window of a small embedding model. The best chunk allows a detail late in the plot to match and supplies an excerpt the agent can cite. |
| Tone attributes | Computed once per film, offline: gpt-4o-mini reads the plot (opening and ending) and assigns moods from a fixed list of 17, a twist grade from 0 to 3 (a reveal must be quoted from the plot) and a violence grade from 0 to 3. Tags are never shown to it | Plot embeddings do not capture tone. Pre-computation provides a low-cost signal and a violence *filter* for approximately $0.5 in total, instead of a model call per request. |
| Re-ranking | A second stage for free-text requests: gpt-4o-mini scores the top 30 candidates for fit from 0 to 10, *including tone and structure*; output as `[id, score]` pairs; blended 0.7 / 0.3 with the first stage | The largest single quality improvement (topic NDCG 0.28 → 0.42, tone 0.03 → 0.12). A local cross-encoder was evaluated and rejected (no improvement, 1.4 s on CPU). |
| Lexical search | TF-IDF (unigrams and bigrams) over title, genres, tags (weighted three times) and plot; weight 0.1 against 0.9 for dense retrieval, plus 0.35 × quality z-score | Tags carry precise labels. At a weight of 0.3, title words degraded the results ("French *Twist*"). |
| Blending | Linear combination of z-scored signals (item-kNN, user-kNN, plot taste, popularity and a small PureSVD term), with weights grid-tuned on a validation split | Every weight can be inspected. The latent term improved NDCG@10 in every user segment. Explanations cite only kNN and content evidence (see Reflection). |
| Request handling | Two stages: retrieve 40 candidates by the explicit request (anchor similarity from co-ratings, plot and genre overlap; description relevance; requested attributes), then re-rank by percentiles, 0.7 request and 0.3 taste | Identified through failure analysis (Toy Story, below). Adding z-scores was ineffective because collaborative-filtering z-scores are heavy-tailed. |
| Diversity | Maximal marginal relevance on plot embeddings (λ = 0.8) | Prevents a list from consisting of several instalments of one franchise. |
| Quality floor | Films with at least three ratings require a raw mean of at least 2.75 (default; relaxed, with notice to the user, when it alone would leave no candidate) | Bayesian shrinkage raised a 1.83-star film to 3.1, allowing it through every filter. The floor changes NDCG@10 on personal ranking by 0.0000 (`outputs/eval/quality_floor.md`) and affects only request-driven results. |
| Memory | Short-term: the last two turns are retained verbatim and earlier turns are compacted to (question, answer). Long-term: a per-user SQLite store (seen, dismissed, liked, disliked, avoided genre, preference), *enforced by the tools* | A statement such as "I have seen it" must persist beyond the session, and a remembered dislike must not depend on the model's attention. |
| Agent | Twelve tools, an explicit tool-use loop and parallel tool calls. OpenAI `gpt-4o-mini` was used for development and for every reported run. A Claude backend is implemented and verified offline against a mocked client, but has not been evaluated live | An explicit loop provides the complete trace required for evaluation. Because the tools are deterministic, the model can be exchanged. |

### Alternatives considered and rejected

- **Retrieval-augmented generation over the dataset, or recommendation from the model's own knowledge.** The
  questions require *computation* over 74,000 ratings (similarity and aggregation), which a language model cannot
  perform by reading retrieved chunks, and such a model would recommend films that are absent from the catalogue.
- **Matrix factorisation as the sole model.** PureSVD alone is approximately as accurate as item-kNN, but a latent
  factor cannot be explained to a user. It is retained as a minor term in the blend.
- **A fixed pipeline or intent classifier instead of an agent.** This would suffice for the six sample queries but is
  fragile for compositional questions ("science fiction before 1970 that people like me rated highly") and for
  follow-up turns. A deterministic "scripted" mode without a language model is nevertheless provided, as a fallback
  and as a reference trajectory.
- **Tags as a primary signal.** Only 1,026 films carry any tag. Tags serve as a high-weight lexical feature and as the
  *evaluation labels* for search, never as a prerequisite.
- **A knowledge graph.** This was evaluated rather than assumed. RP3beta with genre and tag nodes, blended into the
  hybrid, changed NDCG@10 by +0.0007 [95% CI −0.002, +0.004]. The data lacks the rich relations (cast, director) that
  a graph would exploit ([APPENDIX](APPENDIX.md#should-this-use-a-knowledge-graph-tested-not-assumed)).

### Decision Log

| Decision | Alternative considered | Why I chose this |
|---|---|---|
| **The language model plans and explains; all figures come from deterministic, tested tools that return evidence as JSON** | The model reasons over retrieved data (RAG) or recommends from its own knowledge | Correctness can be tested without the language model (130 tests and the offline metrics). The model cannot state a rating it was not given, and every title and figure it writes is verified against the tool outputs. |
| **Neighbourhood collaborative filtering as the backbone, with a small latent-factor term** | Pure matrix factorisation, or pure kNN | kNN evidence translates directly into explanations ("because you rated Aliens five stars"). In isolation PureSVD matches item-kNN (0.109 against 0.108 NDCG@10); in the blend the latent term adds +0.015, which is significant and consistent across all segments. |
| **Per-user temporal split, validation-only tuning, segment analysis and bootstrap confidence intervals** | A random split and a single headline figure | A random split leaks future preferences into training. A single average conceals precisely where the system fails (users with short histories, the long tail). Confidence intervals prevent a difference within noise from being reported as an improvement. |
| **Tone handled by offline attributes *and* an LLM re-ranker rather than by either alone** | Re-ranker only (the earlier design) or attributes only | Attributes alone recover part of the tone improvement at no latency cost; combined with the re-ranker they give the best tone NDCG (0.147 against 0.116). The improvement is not yet significant on 12 queries, so both are retained; the attributes also provide a violence filter that the re-ranker cannot. |
| **Correct behaviour enforced in the tools rather than in the prompt** | Prompt instructions | Remembered genre dislikes, the rule that a seen film is never suggested again, the rule that "more like X" never returns X, and the distinction between one-off and lasting preferences are all enforced in code. Prompt-only corrections failed in measured runs; for example, a stored dislike was ignored in 3 of 3 runs until the tool enforced it. |

## Evaluation

### How do I know it works? Five layers, each answering a different question

| Layer | Question | Method | Rationale |
|---|---|---|---|
| 1. Ranking engine | Does `recommend_movies` place films the user will like near the top? | Per-user temporal split (oldest 80% for training, newest 20% for testing), top ten over the full unseen catalogue, relevance defined as a test rating of at least 4, weights tuned on a separate validation slice; 583 users | Reflects the question "what next?". Tuning on validation data only keeps the test figures unbiased. |
| 2. Neighbour opinion | When the agent reports that "people like you rate it about 4.2", how accurate is that figure? | RMSE, MAE and like-accuracy on 14,571 held-out ratings against four baselines, grouped by the number of neighbours who rated the film | This figure is shown to users, so its error must be known. |
| 3. Content search | Does "a courtroom drama" retrieve courtroom dramas, and does "a twist ending" retrieve films with twists? | 18 topic and 12 tone queries; relevance defined by user tags, which are hidden from the index and from the re-rankers | These are the only relevance labels available without annotation. Because tags are sparse, only comparisons between methods are informative. |
| 4. Conversation (development) | Are the correct tools and *argument values* used, are answers grounded, are constraints respected and is memory correct? | A main suite (14 conversations, 19 turns) and a memory suite (10 conversations, 31 turns), each run three times; automatic checks, an LLM judge and manual review | Grounding and constraints can be verified automatically. These suites were used to find defects and therefore serve as regression tests. |
| 5. Conversation (held-out) | Does the system generalise to requests it was not developed on? | 18 new conversations (20 turns) with ten new users, two in Vietnamese; committed to version control before the first run and run once | The pass rates of layer 4 are optimistic by construction; this layer provides the estimate. |

HR@10 (whether at least one of ten suggestions was relevant) is the most interpretable ranking metric, and NDCG@10 is
the headline metric. Popularity, coverage and long-tail share are reported because accuracy alone rewards the
recommendation of popular titles.

### 1. Ranking (test set, 583 users, `outputs/eval/offline_metrics.md`)

| Model | NDCG@10 | HR@10 | P@10 | R@10 | Coverage | Mean log-popularity |
|---|---|---|---|---|---|---|
| Popularity | 0.080 | 0.326 | 0.056 | 0.063 | 1.8% | 5.34 |
| Content only (plot embeddings) | 0.010 | 0.082 | 0.009 | 0.007 | 5.7% | 2.07 |
| UserKNN | 0.106 | 0.415 | 0.077 | 0.090 | 3.9% | 5.12 |
| ItemKNN | 0.108 | 0.410 | 0.078 | 0.092 | 8.2% | 4.77 |
| PureSVD (k=50) | 0.109 | 0.453 | 0.072 | 0.109 | 11.6% | 4.62 |
| **Hybrid, tuned (deployed)** | **0.129** | **0.482** | **0.093** | **0.118** | 5.7% | 4.96 |
| Hybrid + MMR (as served by the tool) | 0.127 | 0.480 | 0.091 | 0.115 | 5.7% | 4.96 |

Paired bootstrap over users, hybrid minus baseline: **ItemKNN +0.021 [0.014, 0.030]**, **PureSVD +0.020
[0.009, 0.031]**, **Popularity +0.049 [0.036, 0.061]**.

| Training ratings | Users | Hybrid NDCG@10 | Hybrid HR@10 | Best single baseline |
|---|---|---|---|---|
| < 20 | 107 | 0.115 | 0.34 | **PureSVD 0.122** (exceeds the hybrid) |
| 20-49 | 190 | 0.107 | 0.43 | PureSVD 0.107 |
| 50-149 | 188 | 0.108 | 0.50 | ItemKNN 0.095 |
| 150+ | 98 | 0.226 | 0.72 | UserKNN 0.204 |

- The hybrid performs best for users with long histories (72% of heavy users obtain a hit in the top ten) and exceeds
  every baseline in three of the four segments.
- **It is weakest for users with short histories**: one in three users with fewer than 20 ratings obtains a hit, and
  PureSVD alone performs better in that segment. Overall, 52% of users obtain no hit in the top ten. A film the user
  would enjoy but never rated is counted as a miss, so the absolute figures understate practical usefulness; the
  comparisons between models remain valid.
- **The long tail is not addressed.** Films with fewer than five training ratings account for 12.6% of the relevant
  held-out pairs, and every collaborative model has a tail recall of approximately zero. The content-only model
  allocates 43% of its slots to the tail but recovers only 0.1% of it.
- **Limitation of the split.** The split is temporal *per user* rather than at a global cut-off date, so the training
  data may contain other users' ratings made after a test user's test period. This introduces a small amount of
  "future popularity" into every model equally; comparisons are unaffected, and absolute figures are slightly
  optimistic.
- Evaluated and not adopted: per-segment blend weights (below the pre-set validation threshold of +0.002) and a heavy
  quality weighting.

### 2. "What do people like me think of X?": predictor accuracy

| Method | RMSE | MAE | Like-accuracy (≥ 4 stars) |
|---|---|---|---|
| User mean | 0.962 | 0.747 | 64.8% |
| Bias baseline μ + b_u + b_i | 0.890 | 0.687 | 68.6% |
| kNN v1: classic mean-centred | 0.911 | 0.695 | 69.5% |
| **kNN v2: baseline + shrunk neighbour residuals (deployed)** | **0.871** | **0.667** | **70.4%** |

| Neighbours who rated the film | n | RMSE baseline | RMSE v1 | RMSE v2 |
|---|---|---|---|---|
| 0 (falls back to baseline) | 798 | 1.071 | 1.071 | 1.071 |
| 1-2 | 1,249 | 0.953 | **1.087** | 0.915 |
| 3-9 | 2,854 | 0.921 | 0.952 | 0.899 |
| 10+ | 9,670 | 0.855 | 0.857 | 0.838 |

The first version appeared reasonable in demonstrations but performed **worse than a non-personalised baseline**,
markedly so with one or two neighbours. The second version outperforms the baseline in every bucket, and its error
decreases as evidence increases, which supports the reliability labels attached by the tools. The tool requires at
least ten neighbours before summarising an opinion.

### 3. Content search (`outputs/eval/search_eval.md`) and tone attributes (`outputs/eval/attributes_eval.md`)

| Embeddings | Pipeline | NDCG@10 topic | NDCG@10 tone | P@10 tone |
|---|---|---|---|---|
| OpenAI 3-small | dense only | 0.215 | 0.009 | 0.008 |
| OpenAI 3-small | stage 1: 0.9 dense + 0.1 lexical + quality | 0.282 | 0.026 | 0.025 |
| OpenAI 3-small | stage 1 + cross-encoder | 0.289 | 0.022 | 0.017 |
| OpenAI 3-small | stage 1 + LLM re-rank | 0.423 | 0.116 | 0.108 |
| OpenAI 3-small | stage 1 + attributes (no model call at query time) | 0.282 | 0.059 | 0.058 |
| **OpenAI 3-small** | **stage 1 + attributes + LLM re-rank (deployed)** | **0.423** | **0.147** | **0.133** |
| bge-small (local) | stage 1 + LLM re-rank | 0.372 | 0.034 | 0.033 |
| bge-small (local) | stage 1 + attributes + LLM re-rank | 0.372 | 0.113 | 0.108 |

- **Re-ranking has the largest effect**: +50% topic NDCG and the only substantial improvement on tone queries. The
  cross-encoder, trained on web question-answering relevance, does not transfer to the question "does this film feel
  like X" and was rejected.
- **Attributes improve tone retrieval, but the evidence is limited.** With OpenAI embeddings, attributes combined with
  re-ranking improve on re-ranking alone by +0.031 NDCG [95% CI −0.03, +0.10], better on 7 of 12 queries and worse on
  4; the difference is *not significant*. With the local embedding model the difference is +0.079 [+0.02, +0.14].
  Attributes alone approximately double first-stage tone NDCG at no additional latency and therefore also serve as the
  fallback when the re-ranker is unavailable. Topic queries request no attributes and are unaffected.
- **Validity of the attributes**, measured against tags that the extractor never saw: every attribute is
  over-represented among films tagged with the corresponding concept (lift of 2.5 to 47; twist grade 3: 10; dark
  comedy: 10), but recall is low for rare moods (surreal 0.12, satirical 0.07), and only 5 to 19 tagged films are
  available per attribute. The 0-3 twist grade replaced a binary label after pilot runs classified *Sense and
  Sensibility* and *Balto* as twist endings and flagged 22% of 60 films.
- **Limitations.** In this evaluation, tone queries are mapped to attributes manually (as the agent is expected to
  do); whether the agent does so is tested in layer 5. The mood list was written with knowledge of the tone queries.
  The language model brings prior knowledge, and tagged films are disproportionately well known, which favours both
  model-based components. The deployed re-ranking row was initially reported with a label leak (0.445, corrected to
  0.423; [APPENDIX](APPENDIX.md#hardening-round-memory-stricter-verification-latency)).

### 4. Conversation, development suites (`outputs/eval/scenarios_llm_*.json`, `outputs/transcripts_*`)

The complete system was used (gpt-4o-mini at temperature 0.2, OpenAI embeddings, attributes, LLM re-ranking,
history compaction, long-term memory and the online grounding check), and each scenario was run three times. These
runs precede the two final edge-case corrections (absent titles in `exclude_titles`, relaxation of the quality floor),
which affect paths that these suites do not exercise; both held-out sets were re-run after those corrections.

| Per turn | Main suite (57 turns) | Memory suite (93 turns) |
|---|---|---|
| Scenario runs passed · turns passed | 42/42 · 57/57 | 29/30 · 92/93 (one checker false alarm, below) |
| Required tools called · argument values correct | 100% · 100% | 100% · 100% |
| Golden-set hit (main) / memory state correct after every turn (memory) | 100% | 100% |
| Constraint violations (seen, remembered, genre, period, repeats) | 0 | 0 |
| Titles absent from the dataset · titles returned by no tool | 0 · 0 | 0 · 0 |
| Decimal figures ungrounded · misattributed | 0 · 0 of 255 | 0 · 3 of 408 flagged, all correct on inspection |
| "You rated X N stars" statements contradicting the data | 0 of 89 | 0 of 188 |
| Latency p50 / p95 | 3.9 s / 14.3 s | 4.4 s / 13.9 s |
| LLM judge, 1-5: grounded / personalised / explains / honest / helpful | 4.70 / 4.30 / 4.33 / 4.63 / 4.65 | - |

The single failed memory turn is a false alarm. The statement "**The Usual Suspects (1995)** … You rated **The
Shawshank Redemption (1994)** highly … averaging 4.24" is correct, but the model also set the evidence title in bold,
and the checker attributed the figure to that title. The checker has since been corrected: a bold title is treated as
the subject only when it opens a line, a list item or a sentence, and a regression test built from this sentence
confirms the correction; the suite has not been re-run since. These suites were used to identify and correct defects
over many runs (a stricter version initially failed 16 of 42 runs; see the
[APPENDIX](APPENDIX.md#hardening-round-memory-stricter-verification-latency)), so a clean result here is a regression
result rather than evidence that the agent is free of errors. The judge is gpt-4o-mini assessing gpt-4o-mini and was
found to err in both directions; it is therefore used for triage only and never for pass or fail decisions.

### 5. Conversation, held-out sets (`eval/heldout_scenarios.py`, `eval/heldout_v2_scenarios.py`)

Each set was written after development had stopped, committed to version control before its first run, and scored on
that first run. Set v1 covers users 7, 50, 68, 88, 105, 212, 250, 414, 474 and 599 (25 to 1,907 ratings). Set v2,
written after the corrections prompted by v1, covers twelve further new users; one third of its turns probe the
corrected areas with new phrasings (including two "already watched" films that are absent from the dataset), and it
applies a quality threshold to tone requests. Both sets include two requests in Vietnamese.

| First run (the estimate) | Conversations | Turns | Fabricated titles · incorrect figures · incorrect rating statements |
|---|---|---|---|
| v1 | **17/18** | 19/20 | 0 · 0 of 100 · 0 of 31 |
| v2 | **14/16** | 17/19 | 0 · 0 of 93 · 0 of 29 |

Behaviours confirmed: the ambiguous title "Titanic" (1953 and 1997) was resolved and named; titles released after 2014
(*The Force Awakens*, *Avengers: Endgame*) were reported as absent; the preferences "never horror" and "no musicals" were
stored and enforced in the following session; and one-off requests ("tối nay", "just this time") were not stored, so
the English-only rule that guards against storing them was not required.

Failures, issues invisible to the automatic checks (identified by reading every transcript), and their resolution:

| Finding | Set | Status |
|---|---|---|
| "I've already watched Heat and Casino" was applied to one request but not remembered | v1 | **Fixed**: `already_seen` both excludes and stores in a single call |
| *Maid to Order* (1.83 stars) was offered as a light comedy | v1 | **Fixed**: a default quality floor on the raw mean |
| An absent title ("Interstellar") in `exclude_titles` caused the entire call to fail, after which the model listed films the user had already rated | v2 | **Fixed** (a defect in the v1 correction): unresolvable titles are reported and the request proceeds |
| The quality floor removed the only candidate ("a war film before 1960", user 474), producing no answer | v1 re-run | **Fixed**: the floor is relaxed and the answer states that the suggestion is below the usual threshold |
| "A slow, atmospheric horror film" was issued without `include_genres=Horror`; the mood retrieved *Stalker* and *The Seventh Seal* | v2 | **Open** |
| "Mind-bending science fiction" was issued without `moods`; *Back to the Future* was described as mind-bending | v1 | **Open** |
| *Twelve Monkeys* (plot flagged as unreliable) was described from general knowledge, and the flag wording was shown to the user | v1 | **Open** |

After these corrections, v1 passes 18 of 18 and v2 passes 15 of 16 (the remaining failure is the atmospheric-horror
case). Because both sets have now informed corrections, these later figures are regression results rather than
estimates. The estimate is the first-run row: answers are correct and grounded in almost every case, and the failures
concern *completeness* (remembering, mapping a tone or genre onto the correct argument) and the initial absence of a
quality floor, rather than fabrication.

### Qualitative check on the three suggested users (`outputs/transcripts_scripted/`)

| Request | User | Top results | Assessment |
|---|---|---|---|
| "What should I watch tonight?" | 1 | The Godfather II, Ferris Bueller's Day Off, Terminator 2, The Godfather, The Shawshank Redemption | Consistent with the history (five-star ratings for Terminator and Full Metal Jacket); strongly canonical. |
| "Dark psychological thriller with a twist" | 15 | Gaslight, Color of Night, The Machinist, The Game, Shutter Island | Four of five are appropriate; Color of Night (7 ratings, 2.6 stars) is weak. |
| "Liked Toy Story, tired of animation" | 15 | E.T., Pirates of the Caribbean, The Princess Bride, Big, Willy Wonka | Appropriate *after* a correction (Failure 1). |
| "What should I watch tonight?" | 30 | Forrest Gump, Pulp Fiction, Saving Private Ryan, Fight Club, Back to the Future | Plausible but generic (Failure 3). |

### Failure Analysis

**Failure 1: "I liked Toy Story but I'm tired of animated movies" returned *The Silence of the Lambs*** *(corrected at two layers)*
- *Request:* user 15, sample query 5. *Result:* The Usual Suspects, The Silence of the Lambs, The Princess Bride,
  Indiana Jones and the Last Crusade, The Godfather II
  ([transcript](outputs/failure_cases/before_fix_toy_story_anchor_ignored.md)).
- *Cause:* the anchor contributed a single z-score to a blend of heavy-tailed collaborative-filtering z-scores (top
  candidates reach z ≈ 10-14), so the request "similar to Toy Story" was outweighed. Two further causes emerged: plot
  similarity captures topic rather than tone (the nearest non-animated plot to Toy Story is *Child's Play*, a horror
  film about a murderous doll), and co-rating similarity is biased toward popular films (most users rated both Toy
  Story and The Silence of the Lambs).
- *Correction:* two-stage retrieval driven by the request (co-rating, plot and genre overlap), followed by
  percentile re-ranking with weights of 0.7 for the request and 0.3 for taste. The result is now E.T., The Princess
  Bride, Willy Wonka, Big and Pirates of the Caribbean.
- *Recurrence at the agent level:* the live agent called `recommend_movies(exclude_genres=[...])` without passing
  `more_like=["Toy Story"]`, and the LLM judge scored that turn 5 of 5. A check on argument values measured the defect
  (0 of 5 runs); a more precise tool description corrected it (5 of 5), and the check now applies to every scenario.

**Failure 2: tone and structure requests ("light and funny", "a twist")** *(largely corrected; measured)*
- *Request:* user 30, "Something light and funny tonight, nothing violent or dark". *Result:* **Punchline** (a drama
  about a stand-up comedian, 4 ratings, 2.5 stars) and **Funny People** (about a comedian facing a terminal illness).
- *Cause:* embeddings match what a plot is *about* ("comedians") rather than how it *feels*. The same mechanism
  produced P@10 of zero for "twist ending", "surreal" and "dark comedy".
- *Correction, in two steps:* an LLM re-ranker that assesses fit including tone (tone NDCG 0.026 → 0.116), followed by
  offline tone attributes with a violence filter (→ 0.147, not yet significant). The request now returns *Monty
  Python's The Meaning of Life*, *Better Off Dead*, *Clueless* and *White Christmas*.
- *Subsequent quality floor:* the re-ranker assesses fit rather than quality, so poorly rated films were admitted
  (*Calendar Girls*, 2.6 stars; *Maid to Order*, 1.83 stars, held-out v1). A raw-mean floor now excludes them at no
  measurable ranking cost.
- *Remaining weakness:* the agent does not always map tone descriptors onto the attribute arguments (held-out
  "mind-bending"), and a mood can outweigh the requested genre ("atmospheric horror" → *Stalker*). Recall of the rare
  moods is low.

**Failure 3: users with short histories receive a generic list of popular films** *(partly addressed by long-term memory)*
- *Request:* user 30 (18 ratings), "What should I watch tonight?". *Result:* Forrest Gump, Pulp Fiction, Saving
  Private Ryan, Fight Club and Back to the Future, each supported by "strong" evidence (at least ten similar users,
  4.1 to 4.5 stars).
- *Why this is nevertheless a failure:* a user whose favourites are The Shawshank Redemption, Braveheart and Star Wars
  has very probably *seen* Forrest Gump. Eighteen ratings reflect what the user recorded, not what they watched.
  Offline, this segment is the weakest (HR@10 of 0.34), and PureSVD outperforms the hybrid there.
- *Measures taken:* "seen" is a persistent state that the tools enforce in every subsequent session (3 of 3 in the
  two-session scenario). Held-out v1 showed that the agent sometimes did not store this state even when informed
  (Heat and Casino); the `already_seen` argument of the recommendation tools now stores it within the same call.
  *Still open:* the system relies on the user to volunteer this information. Proactive elicitation ("have you seen
  these three?") and a larger latent-factor weight for users with short histories are the next steps.

## Reflection

**Strengths**
- *Grounding by construction.* The language model performs no computation. Across the 228 turns of the final runs
  (main suite, memory suite and both held-out sets), there were no fabricated titles and no incorrect statements about
  the user's ratings, and the three flagged figures were all found to be correct.
- *Decisions based on measurement.* Several initial versions appeared satisfactory in demonstrations but proved
  deficient when measured: the kNN predictor (weaker than its baseline), lexically weighted search, additive anchors, a
  latency optimisation that reduced re-ranking quality by 30%, and false alarms in the grounding check itself.
- *Correct behaviour enforced in code.* Memory rules, exclusions and argument type coercion are implemented in the
  tools and therefore hold even when the model errs.
- *Verifiable explanations.* A statement such as "You gave Aliens five stars, and people who rated both liked this"
  is concrete and can be checked.

**Weaknesses and their causes**
- *Users with short histories and the long tail* (Failure 3; tail recall of approximately zero for every model,
  including the graph-based models). The data does not record what users watched without rating.
- *Completeness of tool use for new phrasings.* Most held-out failures consisted of the model omitting an argument
  (`remember`, `moods`, `include_genres`). Moving facts into arguments the model already supplies (`already_seen`)
  resolved one class of these errors; the mapping from tone descriptors to arguments remains open.
- *Partial faithfulness of explanations.* The PureSVD term influences the ranking but is never cited. Accuracy
  (+0.015 NDCG, significant) was preferred over complete faithfulness.
- *Small and weak labels.* The search evaluation uses 30 queries with sparse tag labels, 5 to 19 tagged films per
  attribute, and 39 held-out turns. These are sufficient to detect regressions and large effects but not fine
  differences.
- *A single evaluated model.* Every reported run uses gpt-4o-mini. The Claude backend is covered only by offline tests
  against a mocked client.
- *Data quality.* 198 films carry the plot of another film; they are masked, which removes the content signal for some
  important titles (*Twelve Monkeys*, *Seven*).

**With more time or resources**
1. Map request terms to arguments on the server side as a fallback (genre names to `include_genres`, tone descriptors to
   `moods`) and cap the attribute weight when a genre is named, followed by a third held-out set, since both current
   sets have now been used.
2. Re-run the memory suite with the corrected subject rule of the grounding check, which should remove the last false
   alarm.
3. Proactive elicitation built on long-term memory ("have you seen it?", "did you like it?"), with the answers fed back
   into collaborative filtering as ratings, and evaluated with simulated users drawn from held-out ratings.
4. An implicit-feedback model that treats a rating as evidence of viewing (EASE or weighted ALS), and a split at a
   global cut-off date.
5. Recovery of the correct plots through `links.csv` (IMDb and TMDB identifiers) instead of masking them.
6. A larger conversational evaluation (about 100 synthetic multi-turn dialogues), a judge stronger than the agent and
   calibrated on about 50 human-labelled turns, and the same suites run with the Claude backend.

## Open Section

- **Knowledge graph, evaluated rather than assumed.** RP3beta on the rating graph, with and without genre and tag
  nodes, scored below the hybrid (−0.030 NDCG), and blending it in produced +0.0007 [−0.002, +0.004]. A graph is
  beneficial when rich relations (cast, director, franchise) are available, which this dataset lacks.
  [Details](APPENDIX.md#should-this-use-a-knowledge-graph-tested-not-assumed).
- **Data-quality finding.** *Twelve Monkeys* matched a search for "a Greek chorus narrates…", which is the plot of
  *Mighty Aphrodite*. A scan identified 41 groups (198 films) sharing byte-identical plots, most probably the result of
  a faulty join upstream. These films are excluded from every content signal and flagged to the model.
  [Details](APPENDIX.md#data-quality-198-movies-carry-the-wrong-plot).
- **Production layer.** The grounding check used in the evaluation also runs on every live answer (with one automatic
  revision round). Every turn and tool call is logged to SQLite and JSONL, and a web dashboard and a command-line
  report present latency, cost, guardrail rate and user feedback against seven service-level objectives.
  [Details](APPENDIX.md#production-layer-guardrails-logs-and-monitoring).
- **Evaluation of the evaluation tooling.** The checks themselves contained defects: false alarms from an overly
  greedy regular expression, a year parser that read "2001: A Space Odyssey" as the year 2001, a literal backspace
  character in a regular expression, a label leak in the search evaluation, and a subject rule that treated bold
  evidence titles as the subject. Every flag was reviewed manually before it was counted, and each defect in the
  tooling has a regression test built from the sentence that exposed it.
  [Details](APPENDIX.md#bugs-in-the-evaluation-tooling-and-why-the-llm-judge-is-not-trusted).
- **Engineering practice.** Linting and formatting with ruff, a pre-commit hook, and GitHub Actions running the 130
  offline tests on Python 3.10 and 3.12 (80% line coverage; the search path is covered through a synthetic
  genre-vector index). The LLM suites are run manually and their outputs committed; the held-out sets are committed
  before they are run.
- **Latency.** The tail latency (p95 of about 15 s) was traced to DNS resolution on new connections rather than to the
  provider: an unresponsive resolver on the development machine, combined with an SDK connection pool that reconnected
  on nearly every call (a 5 s keep-alive, and streamed responses that cannot be reused over HTTP/1.1). A single shared
  HTTP/2 connection pool reduced the main-suite p95 from 14.9 s to 7.2 s and the share of stalled requests from 35% to
  zero in an interleaved A/B comparison. [Details](APPENDIX.md#latency-root-cause-dns-on-every-new-connection).
- **Cost.** An agent turn costs about $0.0015 (a complete three-fold run of the main suite costs about $0.1); the
  attribute extraction cost about $0.5 once and the embedding index $0.08 once. Everything except the LLM transcripts
  and the LLM re-ranking rows is deterministic and runs on a laptop CPU without an API key.
