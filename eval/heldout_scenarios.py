"""Held-out conversation set: written after development was frozen, run once, never used to fix anything.

The main and memory suites (eval/scenarios.py, eval/memory_scenarios.py) were used to find and fix bugs, so their
pass rates are regression results, not estimates of how the agent does on requests it has not seen. This set is
the estimate. It differs from the development suites on purpose:
  * other users (7, 50, 68, 88, 105, 212, 250, 414, 474, 599), from 25 to 1,907 ratings, instead of 1 / 15 / 30
  * new phrasings and request types (era + genre combinations, "like X but no Y", an ambiguous remake title,
    a movie newer than the dataset, rating comparisons)
  * two requests in Vietnamese, which the English-only regex guards in the memory tools do not cover
  * no golden lists: only mechanical checks (tools, argument values, constraints, grounding, memory)

Results go to outputs/eval/scenarios_llm_<model>_heldout.json and are reported as they came out, failures included.
There is no reference plan, so this suite runs in LLM mode only:
    python scripts/run_scenarios.py --mode llm --suite heldout
"""

HELDOUT_SCENARIOS = [
    {
        "id": "h_u7_heist",
        "user_id": 7,
        "turns": [
            {
                "q": "Any good heist or con-artist movies you'd pick for me?",
                "expect_tools": ["search_movies|recommend_movies"],
                "checks": {"not_rated": True, "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u68_romance_90s_no_comedy",
        "user_id": 68,
        "turns": [
            {
                "q": "I'm in the mood for something romantic but not a comedy, ideally from the 90s.",
                "expect_tools": ["recommend_movies|search_movies"],
                "expect_args": {
                    "recommend_movies|search_movies": {
                        "include_genres": {"contains": "Romance"},
                        "exclude_genres": {"contains": "Comedy"},
                    }
                },
                "checks": {
                    "not_rated": True,
                    "include_genres": ["Romance"],
                    "exclude_genres": ["Comedy"],
                    "min_year": 1990,
                    "max_year": 1999,
                    "min_recs": 1,
                },
            },
        ],
    },
    {
        "id": "h_u212_shawshank_vs_everyone",
        "user_id": 212,
        "turns": [
            {
                "q": "Do people who share my taste rate The Shawshank Redemption as highly as everyone else does?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "The Shawshank Redemption"}}},
                "checks": {},
            },
        ],
    },
    {
        "id": "h_u414_harsh_genres",
        "user_id": 414,
        "turns": [
            {
                "q": "Which genres do I rate more harshly than other people do?",
                "expect_tools": ["get_user_profile|genre_blind_spots"],
                "checks": {},
            },
        ],
    },
    {
        "id": "h_u474_old_war",
        "user_id": 474,
        "turns": [
            {
                "q": "Recommend a war film from before 1960 that I haven't rated.",
                "expect_tools": ["recommend_movies|search_movies"],
                "expect_args": {
                    "recommend_movies|search_movies": {"include_genres": {"contains": "War"}, "max_year": {"max": 1960}}
                },
                "checks": {"not_rated": True, "include_genres": ["War"], "max_year": 1959, "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u599_alien_no_horror",
        "user_id": 599,
        "turns": [
            {
                "q": "Something like Alien, but please no horror.",
                "expect_tools": ["recommend_movies"],
                "expect_args": {
                    "recommend_movies": {
                        "more_like": {"contains_movie": "Alien"},
                        "exclude_genres": {"contains": "Horror"},
                    }
                },
                "checks": {"not_rated": True, "exclude_genres": ["Horror"], "exclude_titles": ["Alien"], "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u105_documentaries_then_why",
        "user_id": 105,
        "turns": [
            {
                "q": "Give me three documentaries worth watching.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "include_genres": ["Documentary"], "min_recs": 1},
            },
            {
                "q": "Why did you pick the first one for me?",
                "expect_tools": ["explain_match"],
                "expect_args": {"explain_match": {"movie": True}},
                "checks": {},
            },
        ],
    },
    {
        "id": "h_u50_titanic_ambiguous",
        "user_id": 50,
        "turns": [
            {
                "q": "What do users similar to me think about Titanic?",
                "expect_tools": ["similar_users_opinion|get_movie_details"],
                "checks": {"mentions_any": ["1997", "1953"]},
            },
        ],
    },
    {
        "id": "h_u250_avatar",
        "user_id": 250,
        "turns": [
            {
                "q": "Is Avatar in your database? If so, what do people like me think of it?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Avatar"}}},
                "checks": {},
            },
        ],
    },
    {
        "id": "h_u88_newer_than_dataset",
        "user_id": 88,
        "turns": [
            {
                "q": "What do you think I'd make of Star Wars: The Force Awakens?",
                "expect_tools": ["get_movie_details|similar_users_opinion|explain_match|search_movies"],
                "checks": {"mentions_absent": True},
            },
        ],
    },
    {
        "id": "h_u7_vi_light_comedy_tonight",
        "user_id": 7,
        "turns": [
            {
                "q": "Gợi ý cho tôi vài phim hài nhẹ nhàng để xem tối nay nhé.",
                "expect_tools": ["recommend_movies|search_movies"],
                "forbid_tools": ["remember"],
                "checks": {"not_rated": True, "min_recs": 1, "memory_lacks": {"avoid_genre": "any"}},
            },
        ],
    },
    {
        "id": "h_u68_vi_never_horror",
        "user_id": 68,
        "turns": [
            {
                "q": "Tôi không bao giờ muốn xem phim kinh dị, hãy nhớ điều đó nhé.",
                "expect_tools": ["remember"],
                "checks": {"memory_has": {"avoid_genre": ["Horror"]}},
            },
            {
                "q": "Recommend me something for the weekend.",
                "new_session": True,
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "exclude_genres": ["Horror"], "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u212_mind_bending_not_violent",
        "user_id": 212,
        "turns": [
            {
                "q": "I'd like a mind-bending sci-fi film that isn't too violent.",
                "expect_tools": ["search_movies|recommend_movies"],
                "checks": {"not_rated": True, "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u414_seen_crime",
        "user_id": 414,
        "turns": [
            {
                "q": "I've already watched Heat and Casino, so don't suggest them again. What crime movies would you pick?",
                "expect_tools": ["remember", "recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "include_genres": ["Crime"],
                    "exclude_titles": ["Heat", "Casino"],
                    "min_recs": 1,
                },
            },
        ],
    },
    {
        "id": "h_u599_pulp_fiction_agree",
        "user_id": 599,
        "turns": [
            {
                "q": "How did I rate Pulp Fiction, and would people with similar taste agree with me?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Pulp Fiction"}}},
                "checks": {},
            },
        ],
    },
    {
        "id": "h_u50_80s_twist",
        "user_id": 50,
        "turns": [
            {
                "q": "Suggest a movie from the 1980s with a twist ending.",
                "expect_tools": ["search_movies|recommend_movies"],
                "checks": {"not_rated": True, "min_year": 1980, "max_year": 1989, "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u105_feel_good_tonight",
        "user_id": 105,
        "turns": [
            {
                "q": "Not in the mood for anything sad tonight - a feel-good movie, please.",
                "expect_tools": ["search_movies|recommend_movies"],
                "forbid_tools": ["remember"],
                "checks": {"not_rated": True, "min_recs": 1},
            },
        ],
    },
    {
        "id": "h_u250_horror_just_once",
        "user_id": 250,
        "turns": [
            {
                "q": "Just this once I'd like a horror movie. What would suit me?",
                "expect_tools": ["recommend_movies|search_movies"],
                "forbid_tools": ["remember"],
                "checks": {"not_rated": True, "include_genres": ["Horror"], "min_recs": 1},
            },
        ],
    },
]
