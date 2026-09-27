# h_u68_romance_90s_no_comedy (user 68)

**User:** I'm in the mood for something romantic but not a comedy, ideally from the 90s.

<details><summary>Tool calls: recommend_movies({"n": 5, "include_genres": ["Romance"], "exclude_genres": ["Comedy"], "min_year": 1990, "max_year": 1999})</summary>

`recommend_movies` (264 ms) ->
```json
{
 "user_id": 68,
 "applied_constraints": {
  "include_genres": [
   "Romance"
  ],
  "exclude_genres": [
   "Comedy"
  ],
  "min_year": 1990,
  "max_year": 1999,
  "min_ratings": 3,
  "min_avg_rating": 2.75
 },
 "excluded_already_suggested": 0,
 "recommendations": [
  {
   "movie_id": 1249,
   "title": "La Femme Nikita (1990)",
   "genres": [
    "Action",
    "Crime",
    "Romance",
    "Thriller"
   ],
   "n_ratings": 35,
   "avg_rating": 4.07,
   "tags": [
    "hit men"
   ],
   "because_you_rated": [
    {
     "title": "American Beauty (1999)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.18
    },
    {
     "title": "Star Wars: Episode V - The Empire Strikes Back (1980)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.17
    }
   ],
   "similar_plots_you_liked": [
    {
     "title": "Eastern Promises (2007)",
     "your_rating": 5.0,
     "plot_similarity": 0.46
    },
    {
     "title": "What Women Want (2000)",
     "your_rating": 4.5,
     "plot_similarity": 0.4
    }
   ],
   "similar_users_who_rated_it": {
    "n": 20,
    "avg_rating": 3.9,
    "n_rated_4_or_higher": 13
   },
   "predicted_rating_for_you": 3.8,
   "genre_fit": {
    "Action": {
     "your_avg": 3.26,
     "n": 296
    },
    "Crime": {
     "your_avg": 3.2,
     "n": 135
    },
    "Romance": {
     "your_avg": 3.36,
     "n": 222
    },
    "Thriller": {
     "your_avg": 3.18,
     "n": 247
    }
   },
   "signal_breakdown_z": {
    "user_knn": 5.4,
    "item_knn": 3.5,
    "pure_svd": 0.7
   },
   "evidence_strength": "strong",
   "attributes": {
    "moods": [
     "romantic",
     "quirky"
    ],
    "twist_0_3": 0,
    "violence_0_3": 0
   }
  },
  {
   "movie_id": 28,
   "title": "Persuasion (1995)",
   "genres": [
    "Drama",
    "Romance"
   ],
   "n_ratings": 11,
   "avg_rating": 4.23,
   "tags": [
    "jane austen"
   ],
   "because_you_rated": [
    {
     "title": "Back to the Future Part II (1989)",
     "your_rating": 1.0,
     "co_rating_similarity": -0.06
    },
    {
     "title": "Star Wars: Episode IV - A New Hope (1977)",
     "your_rating": 5.0,
     "co_rating_similarity": 0.05
    }
   ],
   "similar_plots_you_liked": [
    {
     "title": "Pride & Prejudice (2005)",
     "your_rating": 4.0,
     "plot_similarity": 0.67
    },
    {
     "title": "Love Actually (2003)",
     "your_rating": 4.5,
     "plot_similarity": 0.58
    }
   ],
   "similar_users_who_rated_it": {
    "n": 10,
    "avg_rating": 4.15,
    "n_rated_4_or_higher"
```
</details>

**Assistant:**

Here are some romantic movies from the 90s that aren't comedies:

1. **La Femme Nikita (1990)**  
   This film blends action and romance, featuring a strong female lead. You rated *American Beauty (1999)* 5 stars, and many users who liked both films rated *La Femme Nikita* highly as well, averaging 4.07 stars.

2. **Persuasion (1995)**  
   Based on Jane Austen's novel, this drama has a romantic core and has received an average rating of 4.23. It shares plot similarities with *Pride & Prejudice (2005)*, which you rated 4 stars.

3. **Rob Roy (1995)**  
   This historical drama combines romance with action and war elements. You rated *The Fugitive (1993)* 4.5 stars, and similar users also enjoyed *Rob Roy*, giving it an average rating of 3.55.

4. **Hamlet (1996)**  
   A unique adaptation of Shakespeare's classic, this film has a romantic subplot amidst its dramatic themes. It has an average rating of 4.0, and you rated *American Beauty (1999)* 5 stars, showing a connection in taste.

5. **Star Trek: Insurrection (1998)**  
   While primarily a sci-fi film, it features romantic elements and has a decent average rating of 3.15. You rated *U.S. Marshals (1998)* 4 stars, and similar users found enjoyment in this film as well.

Let me know if you'd like more options or details about any of these films!

> PASS · tools_ok=True · golden=None · text_ok=True · memory=[] · issues=none
