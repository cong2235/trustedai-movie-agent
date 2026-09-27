"""Conversation test suite.

Each scenario is a short conversation for one user. Per turn:
  q               what the user says
  new_session     start a fresh conversation first (long-term memory carries over)
  expect_tools    tools a competent agent must call (alternatives with '|'; extra calls are fine)
  expect_args     {tool(s): {arg: spec}}: some call must pass the right *values*. spec is True (present),
                  {"resolves_to": title}, {"contains": value}, {"contains_movie": title}, {"max": n} or {"min": n}
  golden_any      hand-made list of good answers; at least one must be recommended
  checks          not_rated / exclude_genres / include_genres / max_year / min_year / no_repeats /
                  exclude_titles on the recommended movies; mentions_absent / mentions_any on the text;
                  memory_has {kind: [titles] | min_count} on the long-term memory after the turn
  plan            reference tool plan for the no-LLM "scripted" mode (and the gold trajectory)

Golden lists are deliberately broad ("any good dark psychological thriller"), because many answers are right;
they catch answers that are wrong in kind (a horror film for "like Toy Story"), not a different good pick.
"""

DARK_TWIST_GOLD = [
    "Shutter Island",
    "The Game 1997",
    "The Machinist",
    "The Usual Suspects",
    "Memento",
    "Fight Club",
    "Seven",
    "Gaslight 1944",
    "Twelve Monkeys",
    "Primal Fear",
    "Lucky Number Slevin",
    "Mulholland Drive",
    "Identity",
    "Vertigo",
    "Psycho 1960",
    "Black Swan",
    "Donnie Darko",
    "Secret Window",
    "Session 9",
    "Fracture",
]
FAMILY_GOLD = [
    "E.T. the Extra-Terrestrial",
    "Big",
    "The Princess Bride",
    "Willy Wonka & the Chocolate Factory",
    "Babe",
    "Mary Poppins",
    "Home Alone",
    "Jumanji",
    "Hook",
    "Night at the Museum",
    "Pirates of the Caribbean: The Curse of the Black Pearl",
    "Ferris Bueller's Day Off",
    "Back to the Future",
    "Elf",
    "The Wizard of Oz",
    "Stuart Little",
    "Hugo",
    "Charlie and the Chocolate Factory",
    "Matilda",
    "Mrs. Doubtfire",
    "Groundhog Day",
]
OLD_SCIFI_GOLD = [
    "2001: A Space Odyssey",
    "Metropolis",
    "Forbidden Planet",
    "The Day the Earth Stood Still",
    "Planet of the Apes 1968",
    "Invasion of the Body Snatchers 1956",
    "Fantastic Voyage",
    "20,000 Leagues Under the Sea",
    "Them!",
    "Alphaville",
    "Barbarella",
    "Village of the Damned 1960",
]

SCENARIOS = [
    {
        "id": "u1_tonight_then_why",
        "user_id": 1,
        "turns": [
            {
                "q": "What should I watch tonight?",
                "expect_tools": ["recommend_movies"],
                "checks": {"not_rated": True},
                "plan": [("recommend_movies", {"n": 5})],
            },
            {
                "q": "Why do you think I'd like the first one?",
                "expect_tools": ["explain_match"],
                "expect_args": {"explain_match": {"movie": True}},
                "checks": {},
                "plan": [("explain_match", {"movie": "$FIRST_REC"})],
            },
            {
                "q": "Give me three more, but nothing older than 1990.",
                "expect_tools": ["recommend_movies"],
                "expect_args": {"recommend_movies": {"min_year": {"min": 1990}}},
                "checks": {"not_rated": True, "no_repeats": True, "min_year": 1990},
                "plan": [("recommend_movies", {"n": 3, "min_year": 1990})],
            },
        ],
    },
    {
        "id": "u1_pulp_fiction_neighbours",
        "user_id": 1,
        "turns": [
            {
                "q": "What do people with similar taste to mine think about Pulp Fiction?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Pulp Fiction"}}},
                "checks": {"mentions_any": ["3.0", "3 stars", "gave it a 3", "rated it 3", "3★"]},
                "plan": [("similar_users_opinion", {"movie": "Pulp Fiction"})],
            },
        ],
    },
    {
        "id": "u1_terminator_2",
        "user_id": 1,
        "turns": [  # regression: sequels used to resolve to part 1
            {
                "q": "What do users with similar taste think of Terminator 2?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Terminator 2: Judgment Day"}}},
                "checks": {"mentions_any": ["Terminator 2", "Judgment Day"]},
                "plan": [("similar_users_opinion", {"movie": "Terminator 2"})],
            },
        ],
    },
    {
        "id": "u1_blind_spots",
        "user_id": 1,
        "turns": [
            {
                "q": "What's my blind spot? What genres am I missing?",
                "expect_tools": ["genre_blind_spots"],
                "checks": {},
                "plan": [("genre_blind_spots", {})],
            },
        ],
    },
    {
        "id": "u1_old_scifi",
        "user_id": 1,
        "turns": [
            {
                "q": "Recommend me a great sci-fi movie made before 1970.",
                "expect_tools": ["recommend_movies|search_movies"],
                "expect_args": {
                    "recommend_movies|search_movies": {
                        "include_genres": {"contains": "Sci-Fi"},
                        "max_year": {"max": 1970},
                    }
                },
                "golden_any": OLD_SCIFI_GOLD,
                "checks": {"not_rated": True, "include_genres": ["Sci-Fi"], "max_year": 1969},
                "plan": [("recommend_movies", {"n": 4, "include_genres": ["Sci-Fi"], "max_year": 1969})],
            },
        ],
    },
    {
        "id": "u15_dark_twist",
        "user_id": 15,
        "turns": [
            {
                "q": "I want a dark psychological thriller with a twist.",
                "expect_tools": ["search_movies|recommend_movies"],
                "golden_any": DARK_TWIST_GOLD,
                "checks": {"not_rated": True},
                "plan": [("search_movies", {"query": "dark psychological thriller with a twist ending", "n": 5})],
            },
            {
                "q": "Why would I like the top one?",
                "expect_tools": ["explain_match"],
                "expect_args": {"explain_match": {"movie": True}},
                "checks": {},
                "plan": [("explain_match", {"movie": "$FIRST_REC"})],
            },
        ],
    },
    {
        "id": "u15_inception_neighbours",
        "user_id": 15,
        "turns": [
            {
                "q": "What do people with similar taste to mine think of Inception?",
                "expect_tools": ["similar_users_opinion"],
                "expect_args": {"similar_users_opinion": {"movie": {"resolves_to": "Inception"}}},
                "checks": {},
                "plan": [("similar_users_opinion", {"movie": "Inception"})],
            },
        ],
    },
    {
        "id": "u15_toy_story_no_animation",
        "user_id": 15,
        "turns": [
            {
                "q": "I liked Toy Story but I'm tired of animated movies - what else?",
                "expect_tools": ["recommend_movies"],
                "expect_args": {
                    "recommend_movies": {
                        "more_like": {"contains_movie": "Toy Story"},
                        "exclude_genres": {"contains": "Animation"},
                    }
                },
                "golden_any": FAMILY_GOLD,
                "checks": {"not_rated": True, "exclude_genres": ["Animation", "Horror"]},
                "plan": [("recommend_movies", {"n": 5, "more_like": ["Toy Story"], "exclude_genres": ["Animation"]})],
            },
        ],
    },
    {
        "id": "u15_absent_title",
        "user_id": 15,
        "turns": [
            {
                "q": "What do similar users think of The Matrix?",
                "expect_tools": ["similar_users_opinion|get_movie_details"],
                "checks": {"mentions_absent": True},
                "plan": [("similar_users_opinion", {"movie": "The Matrix"})],
            },
        ],
    },
    {
        "id": "u15_ambiguous_seen",
        "user_id": 15,
        "turns": [
            {
                "q": "Have I rated any Star Wars movies? What did I give them?",
                "expect_tools": ["get_rating_history"],
                "expect_args": {"get_rating_history": {"title_contains": True}},
                "checks": {"mentions_any": ["A New Hope", "Empire Strikes Back"]},
                "plan": [("get_rating_history", {"title_contains": "star wars"})],
            },
        ],
    },
    {
        "id": "u30_sparse_tonight",
        "user_id": 30,
        "turns": [
            {
                "q": "What should I watch tonight?",
                "expect_tools": ["recommend_movies"],
                "checks": {"not_rated": True},
                "plan": [("recommend_movies", {"n": 5})],
            },
            {
                "q": "Why do you think I'd like that first one?",
                "expect_tools": ["explain_match"],
                "expect_args": {"explain_match": {"movie": True}},
                "checks": {},
                "plan": [("explain_match", {"movie": "$FIRST_REC"})],
            },
        ],
    },
    {
        "id": "u30_blind_spots",
        "user_id": 30,
        "turns": [
            {
                "q": "What's my blind spot? What genres am I missing?",
                "expect_tools": ["genre_blind_spots"],
                "checks": {},
                "plan": [("genre_blind_spots", {})],
            },
        ],
    },
    {
        "id": "u30_light_funny",
        "user_id": 30,
        "turns": [
            {
                "q": "Something light and funny tonight, nothing violent or dark please.",
                "expect_tools": ["recommend_movies|search_movies"],
                "checks": {"not_rated": True, "include_genres": ["Comedy"], "exclude_genres": ["Horror", "War"]},
                "plan": [
                    (
                        "recommend_movies",
                        {
                            "n": 5,
                            "include_genres": ["Comedy"],
                            "exclude_genres": ["Horror", "War", "Crime", "Thriller"],
                            "mood_or_description": "light-hearted funny feel-good comedy",
                        },
                    )
                ],
            },
        ],
    },
    # long-term memory across two sessions (Failure 3's proposed fix)
    {
        "id": "u30_memory_two_sessions",
        "user_id": 30,
        "turns": [
            {
                "q": "I've already seen Forrest Gump and Back to the Future. Also, remember that I don't like war movies.",
                "expect_tools": ["remember"],
                "checks": {"memory_has": {"seen": ["Forrest Gump", "Back to the Future"], "avoid_genre": ["War"]}},
                "plan": [
                    ("remember", {"kind": "seen", "movie": "Forrest Gump", "scope": "lasting"}),
                    ("remember", {"kind": "seen", "movie": "Back to the Future", "scope": "lasting"}),
                    ("remember", {"kind": "avoid_genre", "note": "War", "scope": "lasting"}),
                ],
            },
            {
                "q": "What should I watch tonight?",
                "new_session": True,
                "expect_tools": ["recommend_movies"],
                "checks": {
                    "not_rated": True,
                    "exclude_titles": ["Forrest Gump", "Back to the Future"],
                    "exclude_genres": ["War"],
                },
                "plan": [("recommend_movies", {"n": 5})],
            },  # the tool applies the remembered genre itself
        ],
    },
]
