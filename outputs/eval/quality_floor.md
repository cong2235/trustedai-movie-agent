# Quality floor: cost in ranking accuracy

Temporal split, tuned blend, 583 users. Floor = drop movies with >= 3 training ratings and a raw mean below it. Shipped: 2.75.

| floor   |   NDCG@10 |   HR@10 |   mean_train_rating |
|:--------|----------:|--------:|--------------------:|
| None    |    0.1292 |   0.482 |              4.0893 |
| 2.5     |    0.1292 |   0.482 |              4.0893 |
| 2.75    |    0.1292 |   0.482 |              4.0899 |
| 3.0     |    0.1292 |   0.482 |              4.0912 |

Paired bootstrap vs no floor (NDCG@10): 2.5: +0.0000 [+0.0000, +0.0000]; 2.75: +0.0000 [+0.0000, +0.0000]; 3.0: +0.0000 [+0.0000, +0.0000]

Share of judged movies (>= min count) removed: 2.5: 10.7%, 2.75: 18.5%, 3.0: 27.6%

## NDCG@10 by history size

| bucket               |   None |    2.5 |   2.75 |    3.0 |
|:---------------------|-------:|-------:|-------:|-------:|
| d) 150+              | 0.2251 | 0.2251 | 0.2251 | 0.2251 |
| a) <20 train ratings | 0.1149 | 0.1149 | 0.1149 | 0.1149 |
| c) 50-149            | 0.1097 | 0.1097 | 0.1097 | 0.1097 |
| b) 20-49             | 0.1071 | 0.1071 | 0.1071 | 0.1071 |