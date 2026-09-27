# Movie attributes vs user tags

4927 of 5135 movies labelled (the rest have an unreliable plot). Tags were never shown to the extractor. Recall = share of tagged movies that got the attribute; lift = recall / base rate. Precision is not measurable (a missing tag does not mean a missing attribute).

| Attribute | Tagged movies (labelled) | Recall on tagged | Base rate | Lift |
|---|---|---|---|---|
| funny | 11 | 0.55 | 0.22 | 2.5x |
| dark-comedy | 18 | 0.56 | 0.05 | 10.3x |
| dark | 6 | 0.67 | 0.26 | 2.5x |
| tense | 19 | 0.53 | 0.17 | 3.1x |
| atmospheric | 19 | 0.21 | 0.03 | 7.6x |
| emotional | 10 | 0.90 | 0.35 | 2.6x |
| inspiring | 5 | 0.40 | 0.05 | 7.6x |
| thought-provoking | 18 | 0.61 | 0.13 | 4.6x |
| surreal | 16 | 0.12 | 0.01 | 11.2x |
| quirky | 12 | 0.42 | 0.10 | 4.1x |
| satirical | 14 | 0.07 | 0.01 | 8.0x |
| disturbing | 12 | 0.17 | 0.04 | 4.6x |
| mind-bending | 11 | 0.18 | 0.00 | 47.1x |
| action-packed | 8 | 0.62 | 0.15 | 4.2x |
| family-friendly | 9 | 0.22 | 0.07 | 3.1x |
| twist >= 1 | 14 | 1.00 | 0.65 | 1.5x |
| twist >= 2 | 14 | 0.93 | 0.34 | 2.7x |
| twist >= 3 | 14 | 0.57 | 0.06 | 10.2x |
| violence >= 1 | 5 | 1.00 | 0.61 | 1.6x |
| violence >= 2 | 5 | 1.00 | 0.38 | 2.7x |
| violence >= 3 | 5 | 0.60 | 0.09 | 6.4x |

Mean twist grade: tagged 2.50 vs all 1.05. Mean violence grade: tagged 2.60 vs all 1.08.