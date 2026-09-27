"""Held-out conversation set v2: written after the fixes prompted by v1, committed before its only run.

v1 (eval/heldout_scenarios.py) found two failures that were then fixed (already-seen movies not remembered; a
1.83-star film in a tone request), so v1 is no longer unseen. v2 is the new estimate, under the same rules:
  * twelve users never used before (19, 21, 57, 111, 177, 232, 288, 380, 448, 480, 561, 606)
  * about a third of the turns probe the fixed areas in new phrasings (already-watched movies, including two that
    are NOT in the dataset: The Notebook and Interstellar; tone requests, now with a quality bar)
  * the rest are ordinary requests of kinds v1 did not cover
  * two requests in Vietnamese; mechanical checks only; results reported as they come out

    python scripts/run_scenarios.py --mode llm --suite heldout2
"""

QUALITY_BAR = 2.75

HELDOUT_V2_SCENARIOS = [
    {
        "id": "v2_u19_watched_mob_classics",
        "user_id": 19,
        "turns": [
            {
                "q": "I watched Goodfellas and The Godfather last week. Recommend something along those lines I haven't seen.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "min_recs": 1,
                    "exclude_titles": ["Goodfellas", "The Godfather"],
                    "memory_has": {"seen": ["Goodfellas", "The Godfather"]},
                },
            },
        ],
    },
    {
        "id": "v2_u21_seen_romance_two_sessions",
        "user_id": 21,
        "turns": [
            {
                "q": "I've seen Titanic (1997) and The Notebook already - can you suggest a romance?",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "include_genres": ["Romance"],
                    "exclude_titles": ["Titanic 1997"],
                    "min_recs": 1,
                    "memory_has": {"seen": ["Titanic 1997"]},
                },
            },
            {
                "q": "Any other romance ideas for me?",
                "new_session": True,
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "exclude_titles": ["Titanic 1997"], "min_recs": 1},
            },
        ],
    },
    {
        "id": "v2_u57_vi_seen_scifi",
        "user_id": 57,
        "turns": [
            {
                "q": "Tôi đã xem Inception và Interstellar rồi, gợi ý phim khoa học viễn tưởng khác nhé.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "include_genres": ["Sci-Fi"],
                    "exclude_titles": ["Inception"],
                    "min_recs": 1,
                    "memory_has": {"seen": ["Inception"]},
                },
            },
        ],
    },
    {
        "id": "v2_u111_family_evening",
        "user_id": 111,
        "turns": [
            {
                "q": "Something heartwarming and funny for a family evening, please.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "min_recs": 1, "min_mean_rating": QUALITY_BAR},
            },
        ],
    },
    {
        "id": "v2_u177_atmospheric_horror",
        "user_id": 177,
        "turns": [
            {
                "q": "A slow, atmospheric horror film - nothing gory.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "include_genres": ["Horror"],
                    "min_recs": 1,
                    "min_mean_rating": QUALITY_BAR,
                },
            },
        ],
    },
    {
        "id": "v2_u232_fight_club_neighbours",
        "user_id": 232,
        "turns": [
            {
                "q": "What do users with taste like mine think of Fight Club?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Fight Club"}}},
                "checks": {},
            },
        ],
    },
    {
        "id": "v2_u288_70s_cult",
        "user_id": 288,
        "turns": [
            {
                "q": "I'd like a cult classic from the 70s.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "min_year": 1970, "max_year": 1979, "min_recs": 1},
            },
        ],
    },
    {
        "id": "v2_u380_above_crowd",
        "user_id": 380,
        "turns": [
            {
                "q": "Which movies have I rated much higher than most people did?",
                "expect_tools": ["get_rating_history|get_user_profile"],
                "checks": {},
            },
        ],
    },
    {
        "id": "v2_u448_old_comedies_then_why",
        "user_id": 448,
        "turns": [
            {
                "q": "Two comedies from before 1980?",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "include_genres": ["Comedy"], "max_year": 1979, "min_recs": 1},
            },
            {
                "q": "Why the second one?",
                "expect_tools": ["explain_match"],
                "expect_args": {"explain_match": {"movie": True}},
                "checks": {},
            },
        ],
    },
    {
        "id": "v2_u480_blade_runner",
        "user_id": 480,
        "turns": [
            {
                "q": "Is Blade Runner in the dataset, and would I like it?",
                "expect_tools": ["explain_match|similar_users_opinion"],
                "expect_args": {"explain_match|similar_users_opinion": {"movie": {"resolves_to": "Blade Runner"}}},
                "checks": {},
            },
        ],
    },
    {
        "id": "v2_u561_twist_seen_two",
        "user_id": 561,
        "turns": [
            {
                "q": "Recommend a thriller with a twist ending - I've already seen Memento and The Usual Suspects.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {
                    "not_rated": True,
                    "exclude_titles": ["Memento", "The Usual Suspects"],
                    "min_recs": 1,
                    "min_mean_rating": QUALITY_BAR,
                    "memory_has": {"seen": ["Memento", "The Usual Suspects"]},
                },
            },
        ],
    },
    {
        "id": "v2_u606_vi_dark_not_violent",
        "user_id": 606,
        "turns": [
            {
                "q": "Cho tôi một phim tâm lý u tối nhưng không quá bạo lực.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "min_recs": 1, "min_mean_rating": QUALITY_BAR},
            },
        ],
    },
    {
        "id": "v2_u19_outside_usual",
        "user_id": 19,
        "turns": [
            {
                "q": "Surprise me with something outside the genres I usually watch.",
                "expect_tools": ["genre_blind_spots|recommend_movies|search_movies"],
                "checks": {"not_rated": True},
            },
        ],
    },
    {
        "id": "v2_u21_no_musicals",
        "user_id": 21,
        "turns": [
            {
                "q": "I don't like musicals, please keep that in mind for the future.",
                "expect_tools": ["remember"],
                "checks": {"memory_has": {"avoid_genre": ["Musical"]}},
            },
            {
                "q": "What should I watch this weekend?",
                "new_session": True,
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "exclude_genres": ["Musical"], "min_recs": 1},
            },
        ],
    },
    {
        "id": "v2_u57_no_scifi_this_time",
        "user_id": 57,
        "turns": [
            {
                "q": "Just this time, no sci-fi. What should I watch?",
                "expect_tools": ["recommend_movies|search_movies"],
                "forbid_tools": ["remember"],
                "checks": {
                    "not_rated": True,
                    "exclude_genres": ["Sci-Fi"],
                    "min_recs": 1,
                    "memory_lacks": {"avoid_genre": "any"},
                },
            },
        ],
    },
    {
        "id": "v2_u232_absent_recent",
        "user_id": 232,
        "turns": [
            {
                "q": "Tell me about Avengers: Endgame - would I enjoy it?",
                "expect_tools": ["get_movie_details|similar_users_opinion|explain_match|search_movies"],
                "checks": {"mentions_absent": True},
            },
        ],
    },
]
