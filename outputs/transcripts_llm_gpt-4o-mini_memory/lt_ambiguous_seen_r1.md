# lt_ambiguous_seen (user 1)

**User:** I've seen Star Wars, remember that so you don't suggest it.

<details><summary>Tool calls: remember({"kind": "seen", "movie": "Star Wars", "scope": "lasting"})</summary>

`remember` (19 ms) ->
```json
{
 "error": "'Star Wars' is ambiguous or not in this dataset (5,135 movies, 1903-2014; some famous titles are missing). Closest titles below - pick one by movie_id, or tell the user it is absent.",
 "closest_titles": [
  {
   "movie_id": 260,
   "title": "Star Wars: Episode IV - A New Hope (1977)",
   "score": 87.3
  },
  {
   "movie_id": 1196,
   "title": "Star Wars: Episode V - The Empire Strikes Back (1980)",
   "score": 87.3
  },
  {
   "movie_id": 1210,
   "title": "Star Wars: Episode VI - Return of the Jedi (1983)",
   "score": 87.3
  },
  {
   "movie_id": 61160,
   "title": "Star Wars: The Clone Wars (2008)",
   "score": 87.3
  },
  {
   "movie_id": 135216,
   "title": "The Star Wars Holiday Special (1978)",
   "score": 87.3
  }
 ]
}
```
</details>

**Assistant:**

It seems "Star Wars" is ambiguous. Could you specify which one you've seen? Here are some options:

1. **Star Wars: Episode IV - A New Hope (1977)**
2. **Star Wars: Episode V - The Empire Strikes Back (1980)**
3. **Star Wars: Episode VI - Return of the Jedi (1983)**
4. **Star Wars: The Clone Wars (2008)**
5. **The Star Wars Holiday Special (1978)**

Please let me know which one to remember!

> PASS · tools_ok=True · golden=None · text_ok=True · memory=[] · issues=none
