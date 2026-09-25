"""Deterministic answer templates for the no-LLM 'scripted' mode.

They turn tool outputs into readable answers so the evidence layer can be demoed and regression-tested
without an API key. They are intentionally plain: the LLM's job is to do this with judgement.
"""

from __future__ import annotations


def _reason(r: dict) -> str:
    bits = []
    if r.get("because_you_rated"):
        b = r["because_you_rated"][:2]
        bits.append("people who rated " + " and ".join(f"{x['title']} (you: {x['your_rating']:g}★)" for x in b)
                    + " the way you did also rated this highly")
    s = r.get("similar_users_who_rated_it")
    if s:
        bits.append(f"{s['n']} users with similar taste rated it {s['avg_rating']:.1f}★ on average")
    if not bits and r.get("similar_plots_you_liked"):
        p = r["similar_plots_you_liked"][0]
        bits.append(f"its plot is close to {p['title']}, which you rated {p['your_rating']:g}★")
    if r.get("predicted_rating_for_you"):
        bits.append(f"predicted rating for you {r['predicted_rating_for_you']}★")
    return "; ".join(bits) + f" [evidence: {r.get('evidence_strength', 'n/a')}]"


def render(tool: str, out: dict) -> str:
    if "error" in out:
        closest = out.get("closest_titles")
        msg = f"⚠ {out['error']}"
        if closest:
            msg += "\nClosest titles: " + ", ".join(c["title"] for c in closest[:3])
        return msg
    if tool == "get_user_profile":
        top = ", ".join(f"{m['title']} ({m['your_rating']:g}★)" for m in out["top_rated"][:4])
        likes = ", ".join(f"{g['genre']} (avg {g['avg_rating']})" for g in out["genres_rated_above_own_average"])
        return (f"You have {out['n_ratings']} ratings (avg {out['avg_rating']}, history: {out['history_size']}). "
                f"Favourites include {top}." + (f" You rate {likes} above your own average." if likes else ""))
    if tool == "recommend_movies":
        lines = [f"{i}. **{r['title']}** - {_reason(r)}" for i, r in enumerate(out["recommendations"], 1)]
        return "Recommendations:\n" + "\n".join(lines) if lines else "No movies match those constraints."
    if tool == "search_movies":
        lines = [f"{i}. **{r['title']}** ({', '.join(r['genres'][:3])}; {r['n_ratings']} ratings, avg {r['avg_rating']}) - "
                 f"matched: \"{r['matching_plot_excerpt'][:160]}\"" for i, r in enumerate(out["results"], 1)]
        return "Matches:\n" + "\n".join(lines)
    if tool == "similar_users_opinion":
        s = out["similar_users"]
        mine = f" You rated it {out['your_rating']:g}★ yourself." if out.get("your_rating") is not None else ""
        if not s.get("n"):
            return f"{out['movie']}: {s['note']}{mine}"
        return (f"{out['movie']}: the {s['n']} most similar users who rated it give it {s['weighted_avg_rating']}★ "
                f"(similarity-weighted); {s['n_rated_4_or_higher']} of them gave 4★+ and {s['n_rated_2_5_or_lower']} gave 2.5★ or less. Everyone else: "
                f"{out['everyone']['avg_rating']}★ over {out['everyone']['n']} ratings.{mine} "
                f"Reliability: {out['reliability']}.")
    if tool == "explain_match":
        parts = [f"Why {out['title']} might suit you:"]
        for b in out.get("because_you_rated", []):
            parts.append(f"- co-rating: you gave {b['title']} {b['your_rating']:g}★, and people rate the two similarly (sim {b['co_rating_similarity']})")
        for p in out.get("similar_plots_you_liked", [])[:2]:
            parts.append(f"- plot: similar to {p['title']} which you rated {p['your_rating']:g}★ (plot sim {p['plot_similarity']})")
        s = out.get("similar_users_who_rated_it")
        if s:
            parts.append(f"- people: {s['n']} similar users average {s['avg_rating']}★ ({s['n_rated_4_or_higher']} gave 4★+)")
        for d in out.get("similar_plots_you_disliked", []):
            parts.append(f"- caution: plot resembles {d['title']}, which you rated {d['your_rating']:g}★")
        parts.append(f"Evidence strength: {out['evidence_strength']}.")
        return "\n".join(parts)
    if tool == "genre_blind_spots":
        lines = []
        for b in out["blind_spots"][:4]:
            eps = ", ".join(e["title"] for e in b["entry_points_liked_by_similar_users"][:2])
            lines.append(f"- **{b['genre']}**: {b['your_n_rated']} of your ratings ({b['your_share']:.0%} vs {b['population_share']:.0%} "
                         f"for the average user); similar users rate it {b['similar_users_relative_liking']:+.2f} vs their norm. Try: {eps}")
        return "Your blind spots:\n" + "\n".join(lines) if lines else "No clear genre blind spots."
    if tool == "get_rating_history":
        items = ", ".join(f"{r['title']} ({r['your_rating']:g}★)" for r in out["ratings"])
        return f"{out['n_matching']} matching ratings: {items}" if out["n_matching"] else "No matching ratings."
    if tool == "find_similar_users":
        return "Most similar users: " + ", ".join(f"user {u['user_id']} (sim {u['similarity']}, {u['n_movies_in_common']} in common)"
                                                   for u in out["similar_users"])
    if tool == "remember":
        what = out.get("movie") or out.get("note")
        return f"Noted ({out['kind']}): {what} - {out['effect']}."
    if tool == "list_memories":
        return "Remembered: " + ("; ".join(f"{m['kind']}: {m['movie'] or m['note']}" for m in out["memories"]) or "nothing")
    if tool == "forget_memory":
        return "Forgotten." if out["ok"] else "Nothing to forget."
    if tool == "get_movie_details":
        return f"{out['title']} - {', '.join(out['genres'])}; {out['n_ratings']} ratings, avg {out['avg_rating']}."
    return str(out)[:500]
