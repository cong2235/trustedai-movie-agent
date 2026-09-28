# Live demo: about 10 minutes

## Setup (5 minutes)

1. Put a valid `OPENAI_API_KEY` in `.env` (see `.env.example`).
2. `python app/server.py` (or `docker compose up -d --build`). Wait for `warm-up: ready` in the log (~45 s), then
   open http://localhost:8501. The sidebar shows **Live agent** when the page is connected to the server.
3. Send one warm-up question (it opens the API connection), then click **New reel**.
4. Keep a terminal open for the fallbacks below.

## Script: one step per requirement

| # | Viewer | Say | What to point at |
|---|---|---|---|
| 1 | 15 | *What should I watch tonight?* | **Req. 1.** Each card shows which of the user's own ratings drive the pick, similar plots they liked, and how similar users rated it. **Production notes** holds the raw `recommend_movies` output |
| 2 | 15 | *Why do you think I'd like the first one?* | **Req. 3.** `explain_match`: co-rated movies, similar plots, similar users and genre fit. Every number in the answer can be found in that tool output |
| 3 | 1 | *What do people with similar taste to mine think about Pulp Fiction?* | **Req. 2.** The scorecard: the user's own 3★, the weighted opinion of 20 similar users, everyone's average, and a reliability label |
| 4 | 15 | *I liked Toy Story but I'm tired of animated movies - what else?* | Constraint plus anchor (*The Princess Bride, Willy Wonka, E.T., Big, Mary Poppins* in rehearsal). *The Silence of the Lambs* from step 1 is what this request returned when the anchor was ignored (REPORT, Failure 1) |
| 5 | 30 | *I've already seen Forrest Gump, and please remember I don't like war movies.* → **New reel** → *What should I watch tonight?* | **Remembered** in the sidebar; the next conversation excludes both, enforced by the tools. **Seen it** on a card and ✕ in the sidebar write to the same store |
| 6 | 15 | *What do similar users think of The Matrix?* | It says the movie is **not in the dataset** instead of guessing |
| 7 | - | **Monitor** tab | Latency, cost per turn (~$0.0016), guardrail triggers and per-tool errors, read from `logs/telemetry.db` |

**Req. 4** is answered from the report rather than the demo: REPORT, Evaluation (held-out 17/18 and 14/16 on the
first run) and Failure Analysis.

Optional, in Vietnamese (viewer 7): *Gợi ý cho tôi vài phim hài nhẹ nhàng để xem tối nay nhé.* The answer comes back
in Vietnamese, and "tối nay" is treated as a one-off, so nothing is stored.

## Fallbacks

| Problem | Fallback |
|---|---|
| API down, or no key | `python -m movie_agent.cli --user 15 --no-llm`, then `/recommend 5`, `/why Big`, `/opinion Pulp Fiction`, `/blind`, `/search dark psychological thriller with a twist`. Same tools and evidence, no LLM |
| A slow first answer (~10-15 s) | The first call opens a connection (DNS/TLS; see APPENDIX, latency). Warm calls show the first token at ~2 s |
| The server does not start | Open `app/chat.html` directly in a browser: it replays the script above from the committed transcripts. Or show `outputs/` (see `outputs/README.md`) |
| The model makes a mistake live | Show the grounding check (the "Revised by grounding check" badge and the Monitor tab), then relate it to the open items in REPORT, Evaluation §5 |
