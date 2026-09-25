# u1_blind_spots (user 1)

**User:** What's my blind spot? What genres am I missing?

<details><summary>Tool calls: genre_blind_spots({})</summary>

`genre_blind_spots` (79 ms) ->
```json
{
 "user_id": 1,
 "n_similar_users_used": 30,
 "blind_spots": [
  {
   "genre": "Drama",
   "your_n_rated": 54,
   "your_share": 0.284,
   "population_share": 0.431,
   "exposure_lift": 0.66,
   "your_avg_in_genre": 4.52,
   "similar_users_relative_liking": 0.15,
   "score": 0.222,
   "entry_points_liked_by_similar_users": [
    {
     "title": "It's a Wonderful Life (1946)",
     "similar_users_avg": 5.0,
     "n_similar_users": 3
    },
    {
     "title": "Rosencrantz and Guildenstern Are Dead (1990)",
     "similar_users_avg": 5.0,
     "n_similar_users": 3
    },
    {
     "title": "Broadcast News (1987)",
     "similar_users_avg": 5.0,
     "n_similar_users": 3
    }
   ]
  },
  {
   "genre": "Romance",
   "your_n_rated": 24,
   "your_share": 0.126,
   "population_share": 0.2,
   "exposure_lift": 0.63,
   "your_avg_in_genre": 4.33,
   "similar_users_relative_liking": -0.0,
   "score": 0.184,
   "entry_points_liked_by_similar_users": [
    {
     "title": "It's a Wonderful Life (1946)",
     "similar_users_avg": 5.0,
     "n_similar_users": 3
    },
    {
     "title": "Broadcast News (1987)",
     "similar_users_avg": 5.0,
     "n_similar_users": 3
    },
    {
     "title": "Tangled (2010)",
     "similar_users_avg": 5.0,
     "n_similar_users": 2
    }
   ]
  }
 ],
 "method": "genres where your share of ratings is <80% of the population's, ranked by exposure gap x how much your most similar users like the genre relative to their own average"
}
```
</details>

**Assistant:**

Your blind spots:
- **Drama**: 54 of your ratings (28% vs 43% for the average user); similar users rate it +0.15 vs their norm. Try: It's a Wonderful Life (1946), Rosencrantz and Guildenstern Are Dead (1990)
- **Romance**: 24 of your ratings (13% vs 20% for the average user); similar users rate it -0.00 vs their norm. Try: It's a Wonderful Life (1946), Broadcast News (1987)

> PASS · tools_ok=True · golden=None · text_ok=True · issues=none
