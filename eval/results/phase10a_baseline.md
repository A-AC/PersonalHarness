# Routing sweep: phase 10A baseline

> Historical artifact: this baseline was recorded before evidence v2 (10B).
> Rerunning the current script produces v2 numbers, so keep this file as the
> recorded pre-change measurement rather than regenerating it.

- Corpus: `eval/scenarios/k_routing.yaml`
- Cases: 46
- General-only control accuracy: 0.2609 (12/46)
- Thresholds: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0
- Margins: 0.5, 0.75, 1.0
- Best: threshold 0.5, margin 0.5
- Accuracy: 0.7174 (33/46)
- General fallback rate: 0.4565
- False-positive rate: 0.0435

Tie-break: higher threshold, then lower margin.

## Grid

| threshold | margin | accuracy | general fallback | false positive | correct |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5 | 0.5 | 0.7174 | 0.4565 | 0.0435 | 33/46 |
| 0.5 | 0.75 | 0.7174 | 0.4565 | 0.0435 | 33/46 |
| 0.5 | 1.0 | 0.7174 | 0.4565 | 0.0435 | 33/46 |
| 1.0 | 0.5 | 0.4348 | 0.7826 | 0.0217 | 20/46 |
| 1.0 | 0.75 | 0.4348 | 0.7826 | 0.0217 | 20/46 |
| 1.0 | 1.0 | 0.4348 | 0.7826 | 0.0217 | 20/46 |
| 1.5 | 0.5 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 1.5 | 0.75 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 1.5 | 1.0 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 2.0 | 0.5 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 2.0 | 0.75 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 2.0 | 1.0 | 0.3696 | 0.8913 | 0.0000 | 17/46 |
| 2.5 | 0.5 | 0.3478 | 0.9130 | 0.0000 | 16/46 |
| 2.5 | 0.75 | 0.3478 | 0.9130 | 0.0000 | 16/46 |
| 2.5 | 1.0 | 0.3478 | 0.9130 | 0.0000 | 16/46 |
| 3.0 | 0.5 | 0.3043 | 0.9565 | 0.0000 | 14/46 |
| 3.0 | 0.75 | 0.3043 | 0.9565 | 0.0000 | 14/46 |
| 3.0 | 1.0 | 0.3043 | 0.9565 | 0.0000 | 14/46 |

## Confusion at the best point

| expected \ predicted | Clothing | General | Health | Home automation | Language learning | total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Clothing | 5 | 3 | 0 | 0 | 0 | 8 |
| General | 1 | 10 | 0 | 0 | 1 | 12 |
| Health | 0 | 4 | 6 | 0 | 0 | 10 |
| Home automation | 0 | 3 | 0 | 5 | 0 | 8 |
| Language learning | 0 | 1 | 0 | 0 | 7 | 8 |

