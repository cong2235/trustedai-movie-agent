# Graph / knowledge-graph experiment

Same temporal split as the main eval; RP3beta params tuned on validation. Long tail = < 5 train ratings. Graph fit time ≈ 0.8 s each.

Validation-tuned params: kg_weight=0.0: alpha=0.8, beta=0.3 (val NDCG 0.0884), kg_weight=0.5: alpha=0.8, beta=0.3 (val NDCG 0.0893), kg_weight=2.0: alpha=0.8, beta=0.3 (val NDCG 0.0882)

## Test results

| model                                   |   NDCG@10 |   HR@10 |   R@10 |   rec_mean_log_pop |   rec_tail_share |   tail_recall |
|:----------------------------------------|----------:|--------:|-------:|-------------------:|-----------------:|--------------:|
| ItemKNN                                 |    0.1076 |  0.4099 | 0.0919 |             4.7658 |           0.0067 |             0 |
| PureSVD (k=50)                          |    0.1093 |  0.4528 | 0.1094 |             4.6182 |           0      |             0 |
| RP3beta (rating graph)                  |    0.0996 |  0.3979 | 0.0892 |             5.139  |           0.0015 |             0 |
| RP3beta + KG nodes (genres/tags, w=0.5) |    0.1005 |  0.4014 | 0.0904 |             5.1429 |           0.0009 |             0 |
| Hybrid (shipped)                        |    0.1292 |  0.482  | 0.1171 |             4.9487 |           0.0002 |             0 |
| Hybrid + RP3beta (w=0.0)                |    0.1292 |  0.482  | 0.1171 |             4.9487 |           0.0002 |             0 |
| Hybrid + RP3beta-KG (w=0.25)            |    0.1299 |  0.4854 | 0.1186 |             4.9966 |           0.0002 |             0 |

## Difference vs shipped hybrid (NDCG@10, paired bootstrap 95% CI)

|                                         |   mean_diff_vs_hybrid | ci95               |
|:----------------------------------------|----------------------:|:-------------------|
| ItemKNN                                 |               -0.0216 | [-0.0296, -0.0141] |
| PureSVD (k=50)                          |               -0.0199 | [-0.031, -0.0093]  |
| RP3beta (rating graph)                  |               -0.0296 | [-0.0408, -0.0182] |
| RP3beta + KG nodes (genres/tags, w=0.5) |               -0.0287 | [-0.0405, -0.0172] |
| Hybrid + RP3beta (w=0.0)                |                0      | [0.0, 0.0]         |
| Hybrid + RP3beta-KG (w=0.25)            |                0.0007 | [-0.0023, 0.0037]  |

## NDCG@10 by user history

| bucket               |   ItemKNN |   PureSVD (k=50) |   RP3beta (rating graph) |   RP3beta + KG nodes (genres/tags, w=0.5) |   Hybrid (shipped) |   Hybrid + RP3beta (w=0.0) |   Hybrid + RP3beta-KG (w=0.25) |
|:---------------------|----------:|-----------------:|-------------------------:|------------------------------------------:|-------------------:|---------------------------:|-------------------------------:|
| a) <20 train ratings |    0.0995 |           0.1221 |                   0.093  |                                    0.0942 |             0.1149 |                     0.1149 |                         0.1138 |
| b) 20-49             |    0.0766 |           0.1067 |                   0.0847 |                                    0.086  |             0.1071 |                     0.1071 |                         0.1125 |
| c) 50-149            |    0.0951 |           0.0835 |                   0.0758 |                                    0.0761 |             0.1097 |                     0.1097 |                         0.1059 |
| d) 150+              |    0.2009 |           0.1498 |                   0.1813 |                                    0.1826 |             0.2251 |                     0.2251 |                         0.2273 |
