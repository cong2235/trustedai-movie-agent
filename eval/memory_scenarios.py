"""Hard scenarios for short-term (conversation) and long-term (cross-session) memory.

Run:  python scripts/run_scenarios.py --mode llm --suite memory --repeat 3
      python scripts/run_scenarios.py --mode scripted --suite memory     # validates the checks on gold plans

Each scenario targets one way memory can go wrong. Memory state is checked *after every turn*
(memory_has / memory_lacks / memory_titles_match), tools that must not be called are forbidden
(forbid_tools), references to earlier answers must resolve to the right movie (resolves_to_prev / mentions_prev),
and long conversations must stay inside a context budget (max_input_tokens).

Short-term (the conversation itself; older turns are compacted to question + final answer after 2 turns)
  st_second_one_after_compaction   "why the 2nd movie of your FIRST answer?" asked 3 turns later, after that turn's
                                   tool outputs were compacted away - only the answer text is left to resolve it
  st_constraints_carry_and_change  constraints stated once must hold for "three more", then one is lifted and one kept
  st_long_conversation_budget      8 turns; context must stay bounded, and the first recommendation must be recalled

Long-term (per-user store, survives new sessions)
  lt_forget_preference             store a genre dislike, see it enforced, retract it, see horror allowed again
  lt_one_off_not_stored            "just for tonight, no comedies" must be honoured but NOT remembered
  lt_override_in_session           a remembered dislike vs an explicit request for that genre: request wins, memory kept
  lt_dismissed_and_isolation       a dismissed movie is never suggested again to that user - and not affected for others
  lt_recall_liked                  "something like the movie I told you I loved last time" + don't re-suggest it
  lt_ambiguous_seen                "I've seen Star Wars" (6 films): ask or store a Star Wars film, never a wrong one
  lt_seen_but_discussable          a movie remembered as seen can still be discussed ("what do similar users think")
"""

MEMORY_SCENARIOS = [
    # ------------------------------------------------------------------ short-term
    {"id": "st_second_one_after_compaction", "user_id": 15, "turns": [
        {"q": "What should I watch tonight? Give me four options.",
         "expect_tools": ["recommend_movies"], "checks": {"not_rated": True, "min_recs": 3},
         "plan": [("recommend_movies", {"n": 4})]},
        {"q": "What's my blind spot? Which genres am I missing?",
         "expect_tools": ["genre_blind_spots"], "checks": {},
         "plan": [("genre_blind_spots", {})]},
        {"q": "What do people with similar taste think of Inception?",
         "expect_tools": ["similar_users_opinion"], "checks": {},
         "plan": [("similar_users_opinion", {"movie": "Inception"})]},
        {"q": "Going back to your very first list of suggestions - why would I like the second movie on it?",
         "expect_tools": ["explain_match"],
         "expect_args": {"explain_match": {"movie": {"resolves_to_prev": [0, 1]}}}, "checks": {},
         "plan": [("explain_match", {"movie": "$PREV:0:1"})]},
    ]},
    {"id": "st_constraints_carry_and_change", "user_id": 15, "turns": [
        {"q": "I'd like a movie made after 2000, and no horror please.",
         "expect_tools": ["recommend_movies|search_movies"],
         "checks": {"not_rated": True, "min_year": 2001, "exclude_genres": ["Horror"], "min_recs": 1},
         "plan": [("recommend_movies", {"n": 3, "min_year": 2001, "exclude_genres": ["Horror"]})]},
        {"q": "Give me three more.",
         "expect_tools": ["recommend_movies|search_movies"],
         "expect_args": {"recommend_movies|search_movies": {"min_year": {"min": 2000},
                                                            "exclude_genres": {"contains": "Horror"}}},
         "checks": {"not_rated": True, "min_year": 2001, "exclude_genres": ["Horror"], "no_repeats": True, "min_recs": 2},
         "plan": [("recommend_movies", {"n": 3, "min_year": 2001, "exclude_genres": ["Horror"]})]},
        {"q": "Actually, older films are fine too - but still no horror. Two more, please.",
         "expect_tools": ["recommend_movies|search_movies"],
         "expect_args": {"recommend_movies|search_movies": {"min_year": {"absent_or_below": 2000},
                                                            "exclude_genres": {"contains": "Horror"}}},
         "checks": {"not_rated": True, "exclude_genres": ["Horror"], "no_repeats": True, "min_recs": 1},
         "plan": [("recommend_movies", {"n": 2, "exclude_genres": ["Horror"]})]},
    ]},
    {"id": "st_long_conversation_budget", "user_id": 1, "turns": [
        {"q": "What should I watch tonight?", "expect_tools": ["recommend_movies"],
         "checks": {"not_rated": True, "min_recs": 1}, "plan": [("recommend_movies", {"n": 5})]},
        {"q": "Why do you think I'd like the first one?", "expect_tools": ["explain_match"],
         "expect_args": {"explain_match": {"movie": {"resolves_to_prev": [0, 0]}}}, "checks": {},
         "plan": [("explain_match", {"movie": "$PREV:0:0"})]},
        {"q": "What's my blind spot?", "expect_tools": ["genre_blind_spots"], "checks": {},
         "plan": [("genre_blind_spots", {})]},
        {"q": "What do people with similar taste think about Pulp Fiction?", "expect_tools": ["similar_users_opinion"],
         "checks": {}, "plan": [("similar_users_opinion", {"movie": "Pulp Fiction"})]},
        {"q": "Recommend a sci-fi movie made before 1970.", "expect_tools": ["recommend_movies|search_movies"],
         "checks": {"not_rated": True, "include_genres": ["Sci-Fi"], "max_year": 1969}, "max_input_tokens": 10000,
         "plan": [("recommend_movies", {"n": 3, "include_genres": ["Sci-Fi"], "max_year": 1969})]},
        {"q": "Now a dark psychological thriller with a twist.", "expect_tools": ["search_movies|recommend_movies"],
         "checks": {"not_rated": True}, "max_input_tokens": 10000,
         "plan": [("search_movies", {"query": "dark psychological thriller with a twist", "n": 4})]},
        {"q": "Have I rated any Star Wars movies?", "expect_tools": ["get_rating_history"], "checks": {},
         "max_input_tokens": 10000, "plan": [("get_rating_history", {"title_contains": "star wars"})]},
        {"q": "Remind me: what was the very first movie you recommended to me in this conversation?",
         "expect_tools": [], "checks": {"mentions_prev": [0, 0]}, "max_input_tokens": 10000, "llm_only": True,
         "plan": []},
    ]},
    # ------------------------------------------------------------------ long-term
    {"id": "lt_forget_preference", "user_id": 30, "turns": [
        {"q": "Please remember that I don't like horror movies.", "expect_tools": ["remember"],
         "checks": {"memory_has": {"avoid_genre": ["Horror"]}},
         "plan": [("remember", {"kind": "avoid_genre", "note": "Horror"})]},
        {"q": "Recommend me something for tonight.", "new_session": True, "expect_tools": ["recommend_movies"],
         "checks": {"not_rated": True, "exclude_genres": ["Horror"], "min_recs": 1},
         "plan": [("recommend_movies", {"n": 4})]},
        {"q": "Actually, I've changed my mind - I'm fine with horror movies now. Please forget that.",
         "expect_tools": ["forget_memory"], "checks": {"memory_lacks": {"avoid_genre": ["Horror"]}},
         "plan": [("list_memories", {}), ("forget_memory", {"memory_id": 1})]},
        {"q": "Recommend me a good horror movie.", "new_session": True, "expect_tools": ["recommend_movies|search_movies"],
         "checks": {"not_rated": True, "include_genres": ["Horror"], "min_recs": 1,
                    "memory_lacks": {"avoid_genre": ["Horror"]}},
         "plan": [("recommend_movies", {"n": 3, "include_genres": ["Horror"]})]},
    ]},
    {"id": "lt_one_off_not_stored", "user_id": 1, "turns": [
        {"q": "Just for tonight I'm not in the mood for comedies - what should I watch?",
         "expect_tools": ["recommend_movies|search_movies"], "forbid_tools": ["remember"],
         "checks": {"not_rated": True, "exclude_genres": ["Comedy"], "min_recs": 1,
                    "memory_lacks": {"avoid_genre": ["Comedy"], "preference": ["comed"]}},
         "plan": [("recommend_movies", {"n": 4, "exclude_genres": ["Comedy"]})]},
        {"q": "What should I watch tonight?", "new_session": True, "expect_tools": ["recommend_movies"],
         "checks": {"not_rated": True, "memory_lacks": {"avoid_genre": "any"}},
         "plan": [("recommend_movies", {"n": 4})]},
    ]},
    {"id": "lt_override_in_session", "user_id": 30, "turns": [
        {"q": "I never want war movies recommended to me. Please remember that.", "expect_tools": ["remember"],
         "checks": {"memory_has": {"avoid_genre": ["War"]}},
         "plan": [("remember", {"kind": "avoid_genre", "note": "War"})]},
        {"q": "Today I'm curious though - recommend me a war movie, just this once.", "new_session": True,
         "expect_tools": ["recommend_movies|search_movies"], "forbid_tools": ["forget_memory"],
         "checks": {"not_rated": True, "include_genres": ["War"], "min_recs": 1,
                    "memory_has": {"avoid_genre": ["War"]}},
         "plan": [("recommend_movies", {"n": 3, "include_genres": ["War"]})]},
    ]},
    {"id": "lt_dismissed_and_isolation", "user_id": 30, "turns": [
        {"q": "I'm not interested in Fight Club, please never suggest it to me.", "expect_tools": ["remember"],
         "checks": {"memory_has": {"dismissed": ["Fight Club"]}},
         "plan": [("remember", {"kind": "dismissed", "movie": "Fight Club"})]},
        {"q": "What should I watch tonight?", "new_session": True, "expect_tools": ["recommend_movies"],
         "checks": {"not_rated": True, "exclude_titles": ["Fight Club"], "min_recs": 1},
         "plan": [("recommend_movies", {"n": 6})]},
        {"q": "What should I watch tonight?", "user_id": 15, "expect_tools": ["recommend_movies"],
         "checks": {"not_rated": True, "memory_lacks": {"dismissed": "any", "avoid_genre": "any"}},
         "plan": [("recommend_movies", {"n": 4})]},
    ]},
    {"id": "lt_recall_liked", "user_id": 15, "turns": [
        {"q": "I finally watched The Machinist last night and loved it - please remember that.",
         "expect_tools": ["remember"], "checks": {"memory_has": {"liked": ["The Machinist"]}},
         "plan": [("remember", {"kind": "liked", "movie": "The Machinist"})]},
        {"q": "Recommend me something similar to the movie I told you I loved last time.", "new_session": True,
         "expect_tools": ["recommend_movies|search_movies"],
         "expect_args": {"recommend_movies": {"more_like": {"contains_movie": "The Machinist"}}},
         "checks": {"not_rated": True, "exclude_titles": ["The Machinist"], "min_recs": 1},
         "plan": [("recommend_movies", {"n": 4, "more_like": ["The Machinist"]})]},
    ]},
    {"id": "lt_ambiguous_seen", "user_id": 1, "turns": [
        {"q": "I've seen Star Wars, remember that so you don't suggest it.",
         "expect_tools": [], "checks": {"memory_titles_match": {"seen": "star wars"}},
         "plan": [("remember", {"kind": "seen", "movie": "Star Wars: Episode IV - A New Hope"})]},
    ]},
    {"id": "lt_seen_but_discussable", "user_id": 30, "turns": [
        {"q": "I've already seen Forrest Gump.", "expect_tools": ["remember"],
         "checks": {"memory_has": {"seen": ["Forrest Gump"]}},
         "plan": [("remember", {"kind": "seen", "movie": "Forrest Gump"})]},
        {"q": "What do users with similar taste think of Forrest Gump?", "new_session": True,
         "expect_tools": ["similar_users_opinion"],
         "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Forrest Gump"}}},
         "checks": {"mentions_any": ["Forrest Gump"]},
         "plan": [("similar_users_opinion", {"movie": "Forrest Gump"})]},
    ]},
]
