# Content search evaluation (tags as silver labels)

18 topic + 12 tone/structure queries. Tags hidden from the lexical index and re-rankers. Precision is a lower bound (tags are sparse).

|                                                       |   NDCG@10 tone |   NDCG@10 topic |   P@10 tone |   P@10 topic |
|:------------------------------------------------------|---------------:|----------------:|------------:|-------------:|
| ('bge-small', 'dense only')                           |          0     |           0.167 |       0     |        0.161 |
| ('bge-small', 'stage 1 (dense+lexical+quality)')      |          0.019 |           0.232 |       0.025 |        0.217 |
| ('bge-small', 'stage 1 + cross rerank')               |          0.017 |           0.282 |       0.017 |        0.244 |
| ('bge-small', 'stage 1 + llm rerank')                 |          0.034 |           0.372 |       0.033 |        0.322 |
| ('openai-3-small', 'dense only')                      |          0.009 |           0.215 |       0.008 |        0.211 |
| ('openai-3-small', 'stage 1 (dense+lexical+quality)') |          0.026 |           0.282 |       0.025 |        0.25  |
| ('openai-3-small', 'stage 1 + cross rerank')          |          0.022 |           0.289 |       0.017 |        0.261 |
| ('openai-3-small', 'stage 1 + llm rerank')            |          0.116 |           0.423 |       0.108 |        0.356 |

Median re-rank latency (ms): stage 1 + cross rerank: 1442, stage 1 + llm rerank: 2284

## P@10 per tone query

| query                                                         |   ('bge-small', 'dense only') |   ('bge-small', 'stage 1 (dense+lexical+quality)') |   ('bge-small', 'stage 1 + cross rerank') |   ('bge-small', 'stage 1 + llm rerank') |   ('openai-3-small', 'dense only') |   ('openai-3-small', 'stage 1 (dense+lexical+quality)') |   ('openai-3-small', 'stage 1 + cross rerank') |   ('openai-3-small', 'stage 1 + llm rerank') |
|:--------------------------------------------------------------|------------------------------:|---------------------------------------------------:|------------------------------------------:|----------------------------------------:|-----------------------------------:|--------------------------------------------------------:|-----------------------------------------------:|---------------------------------------------:|
| a deeply emotional tearjerker                                 |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0.1 |                                            0.1 |                                          0.1 |
| a disturbing, unsettling film                                 |                             0 |                                                0   |                                       0   |                                     0.1 |                                0   |                                                     0   |                                            0   |                                          0.1 |
| a mind-bending psychological movie that messes with your head |                             0 |                                                0.1 |                                       0   |                                     0.1 |                                0   |                                                     0.1 |                                            0   |                                          0.4 |
| a moody, atmospheric film                                     |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0   |
| a pitch-black dark comedy                                     |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0   |
| a quirky, offbeat movie                                       |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0   |
| a really funny movie that makes you laugh out loud            |                             0 |                                                0.1 |                                       0.1 |                                     0   |                                0   |                                                     0   |                                            0   |                                          0.1 |
| a satire that mocks society                                   |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0   |
| a surreal, dreamlike film                                     |                             0 |                                                0.1 |                                       0.1 |                                     0.1 |                                0.1 |                                                     0.1 |                                            0.1 |                                          0.2 |
| a tense, suspenseful film                                     |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0.1 |
| a thought-provoking film that makes you think                 |                             0 |                                                0   |                                       0   |                                     0   |                                0   |                                                     0   |                                            0   |                                          0.1 |
| a thriller with a shocking twist ending                       |                             0 |                                                0   |                                       0   |                                     0.1 |                                0   |                                                     0   |                                            0   |                                          0.2 |

## Tone queries: top-5 before / after re-ranking (last backend; ✓ = tagged)

**a thriller with a shocking twist ending**

| stage 1 | + re-rank |
|---|---|
|   Dead of Night (1945) | ✓ The Usual Suspects (1995) |
|   Murder by Death (1976) |   North by Northwest (1959) |
|   Clue (1985) |   Rear Window (1954) |
|   Vertigo (1958) |   Vertigo (1958) |
|   North by Northwest (1959) |   Clue (1985) |

**a pitch-black dark comedy**

| stage 1 | + re-rank |
|---|---|
|   Nowhere (1997) |   Nowhere (1997) |
|   Pitch Black (2000) |   Miss Nobody (2010) |
|   Miss Nobody (2010) |   Shallow Grave (1994) |
|   Night on Earth (1991) |   Waydowntown (2000) |
|   Something Wicked This Way Comes (1983) |   Evil Aliens (2005) |

**a surreal, dreamlike film**

| stage 1 | + re-rank |
|---|---|
|   Waking Life (2001) |   Waking Life (2001) |
| ✓ Inception (2010) |   Meshes of the Afternoon (1943) |
|   Dolls (2002) |   Brazil (1985) |
|   Brazil (1985) | ✓ Inception (2010) |
|   Underground (1995) |   Holy Motors (2012) |

**a moody, atmospheric film**

| stage 1 | + re-rank |
|---|---|
|   My Bodyguard (1980) |   Holy Motors (2012) |
|   Holy Motors (2012) |   Persona (1966) |
|   Dolls (2002) |   Mouchette (1967) |
|   Argo (2012) |   Sunset Blvd. (1950) |
|   Mystic Pizza (1988) |   Spiral (2007) |

**a really funny movie that makes you laugh out loud**

| stage 1 | + re-rank |
|---|---|
|   Silent Movie (1976) |   Waiting for Guffman (1996) |
|   Waiting for Guffman (1996) |   Monty Python's The Meaning of Life (1983) |
|   Singin' in the Rain (1952) |   Silent Movie (1976) |
|   Lucky Break (2001) |   Animal Crackers (1930) |
|   Animal Crackers (1930) |   Singin' in the Rain (1952) |

**a quirky, offbeat movie**

| stage 1 | + re-rank |
|---|---|
|   Something Wild (1986) |   Rubber (2010) |
|   Animal Crackers (1930) |   Animal Crackers (1930) |
|   Zero Effect (1998) |   Something Wild (1986) |
|   Rubber (2010) |   Waiting for Guffman (1996) |
|   Cherish (2002) |   Holy Motors (2012) |

**a thought-provoking film that makes you think**

| stage 1 | + re-rank |
|---|---|
|   Sympathy for the Devil (1968) |   Waking Life (2001) |
|   Mortal Thoughts (1991) |   Proof (2005) |
|   Waking Life (2001) | ✓ Inception (2010) |
|   Pretty Persuasion (2005) |   Enter the Void (2009) |
|   Powaqqatsi (1988) |   Holy Motors (2012) |

**a tense, suspenseful film**

| stage 1 | + re-rank |
|---|---|
|   Argo (2012) |   Argo (2012) |
|   Survive Style 5+ (2004) |   Saw (2003) |
|   Trapped (2002) | ✓ The Usual Suspects (1995) |
|   Missing (1982) |   Trapped (2002) |
|   Living in Oblivion (1995) |   Peeping Tom (1960) |

**a deeply emotional tearjerker**

| stage 1 | + re-rank |
|---|---|
|   Cry-Baby (1990) |   Truly, Madly, Deeply (1991) |
|   Truly, Madly, Deeply (1991) |   Beautiful Boy (2010) |
|   Red Corner (1997) |   Reign Over Me (2007) |
|   Bed of Roses (1996) |   Ikiru (1952) |
|   Falling Angels (2003) |   Dancer in the Dark (2000) |

**a disturbing, unsettling film**

| stage 1 | + re-rank |
|---|---|
|   Gummo (1997) |   Sinister (2012) |
|   Sinister (2012) |   Gummo (1997) |
|   Peeping Tom (1960) |   Peeping Tom (1960) |
|   Repulsion (1965) | ✓ Eraserhead (1977) |
|   V/H/S (2012) |   Repulsion (1965) |

**a mind-bending psychological movie that messes with your head**

| stage 1 | + re-rank |
|---|---|
|   Altered States (1980) | ✓ Inception (2010) |
|   Spiral (2007) |   Altered States (1980) |
|   Psycho (1960) |   Mulholland Drive (2001) |
|   Spirits of the Dead (1968) |   A Clockwork Orange (1971) |
| ✓ Inception (2010) | ✓ Memento (2000) |

**a satire that mocks society**

| stage 1 | + re-rank |
|---|---|
|   99 francs (2007) |   99 francs (2007) |
|   Meet John Doe (1941) |   Monty Python's The Meaning of Life (1983) |
|   Animal Crackers (1930) |   Idiocracy (2006) |
|   Ridicule (1996) |   Network (1976) |
|   Modern Times (1936) |   Meet John Doe (1941) |
