# h_u599_alien_no_horror (user 599)

**User:** Something like Alien, but please no horror.

<details><summary>Tool calls: recommend_movies({"include_genres": ["Sci-Fi"], "exclude_genres": ["Horror"], "more_like": ["Alien (1979)"], "min_avg_rating": 3})</summary>

`recommend_movies` (362 ms) ->
```json
{
 "user_id": 599,
 "applied_constraints": {
  "include_genres": [
   "Sci-Fi"
  ],
  "exclude_genres": [
   "Horror"
  ],
  "min_ratings": 3,
  "more_like": [
   "Alien (1979)"
  ],
  "min_avg_rating": 3
 },
 "excluded_already_suggested": 0,
 "recommendations": [
  {
   "movie_id": 2010,
   "title": "Metropolis (1927)",
   "genres": [
    "Drama",
    "Sci-Fi"
   ],
   "n_ratings": 21,
   "avg_rating": 3.86,
   "because_you_rated": [
    {
     "title": "Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb (1964)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.15
    },
    {
     "title": "2001: A Space Odyssey (1968)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.13
    }
   ],
   "similar_plots_you_liked": [
    {
     "title": "RoboCop (1987)",
     "your_rating": 4.0,
     "plot_similarity": 0.53
    },
    {
     "title": "Stalker (1979)",
     "your_rating": 4.0,
     "plot_similarity": 0.52
    }
   ],
   "similar_users_who_rated_it": {
    "n": 20,
    "avg_rating": 3.85,
    "n_rated_4_or_higher": 12
   },
   "predicted_rating_for_you": 3.1,
   "genre_fit": {
    "Drama": {
     "your_avg": 2.81,
     "n": 664
    },
    "Sci-Fi": {
     "your_avg": 2.69,
     "n": 264
    }
   },
   "signal_breakdown_z": {
    "anchor": 3.9,
    "item_knn": 2.8,
    "user_knn": 2.4
   },
   "evidence_strength": "strong",
   "attributes": {
    "moods": [
     "dark",
     "thought-provoking"
    ],
    "twist_0_3": 2,
    "violence_0_3": 1
   }
  },
  {
   "movie_id": 2021,
   "title": "Dune (1984)",
   "genres": [
    "Adventure",
    "Sci-Fi"
   ],
   "n_ratings": 37,
   "avg_rating": 3.43,
   "because_you_rated": [
    {
     "title": "Léon: The Professional (1994)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.17
    },
    {
     "title": "Akira (1988)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.16
    }
   ],
   "similar_plots_you_liked": [
    {
     "title": "The Fifth Element (1997)",
     "your_rating": 4.0,
     "plot_similarity": 0.52
    },
    {
     "title": "Star Wars: Episode V - The Empire Strikes Back (1980)",
     "your_rating": 5.0,
     "plot_similarity": 0.52
    }
   ],
   "similar_users_who_rated_it": {
    "n": 20,
    "avg_rating": 3.52,
    "n_rated_4_or_higher": 8
   },
   "predicted_rating_for_you": 2.9,
   "genre_fit": {
    "Adventure": {
     "your_avg": 2.71,
     "n": 278
    },
    "Sci-Fi": {
     "your_avg": 2.69,
     "n": 264
    }
   },
   "signal_breakdown_z"
```
</details>

**Assistant:**

Here are some recommendations similar to *Alien (1979)*, while avoiding horror:

1. **Metropolis (1927)**  
   This classic sci-fi film has a strong average rating of 3.86. You rated *2001: A Space Odyssey (1968)* and *Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb (1964)* both 5 stars, and users who liked those films also enjoyed *Metropolis*. Its themes are dark and thought-provoking, aligning well with your taste.

2. **Dune (1984)**  
   With an average rating of 3.43, this adaptation of Frank Herbert's novel features a mix of adventure and sci-fi elements. You rated *Akira (1988)* and *Léon: The Professional (1994)* both 5 stars, and similar users rated *Dune* positively as well. It’s thought-provoking and action-packed, which might resonate with you.

3. **Chronicle (2012)**  
   This film combines action, sci-fi, and thriller elements, holding an average rating of 3.38. You rated *Blade Runner (1982)* 4.5 stars, and users with similar tastes found *Chronicle* engaging. It has a tense and emotional vibe, which could appeal to you.

4. **Forbidden Planet (1956)**  
   This sci-fi drama has an average rating of 3.81 and features themes that resonate with *Alien*. You rated *Aliens (1986)* 5 stars, and this film's plot is similar to both *2001: A Space Odyssey* and *Alien*. It’s atmospheric and thought-provoking, fitting your preferences well.

5. **Futurama: Bender's Big Score (2007)**  
   An animated sci-fi comedy with an average rating of 3.9, this film is quirky and funny. While it has a lighter tone, it shares thematic elements with *2001: A Space Odyssey*, which you rated highly. It could provide a fun twist on the sci-fi genre.

Let me know if you'd like more options or details on any of these films!

> PASS · tools_ok=True · golden=None · text_ok=True · memory=[] · issues=none
