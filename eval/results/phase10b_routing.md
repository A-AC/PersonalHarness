# Routing sweep: phase10b_routing

- Corpus: `eval/scenarios/k_routing.yaml`
- Cases: 61
- Thresholds: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0
- Margins: 0.5, 0.75, 1.0
- Best: threshold 1.0, margin 1.0
- Accuracy: 0.8525 (52/61)
- General fallback rate: 0.4262
- False-positive rate: 0.0000
- General-only control accuracy: 0.2787 (17/61)

Tie-break: higher threshold, then lower margin.

## Grid

| threshold | margin | accuracy | general fallback | false positive | correct |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5 | 0.5 | 0.8197 | 0.3770 | 0.0492 | 50/61 |
| 0.5 | 0.75 | 0.8361 | 0.3934 | 0.0328 | 51/61 |
| 0.5 | 1.0 | 0.8525 | 0.4098 | 0.0164 | 52/61 |
| 1.0 | 0.5 | 0.8197 | 0.3934 | 0.0328 | 50/61 |
| 1.0 | 0.75 | 0.8361 | 0.4098 | 0.0164 | 51/61 |
| 1.0 | 1.0 | 0.8525 | 0.4262 | 0.0000 | 52/61 |
| 1.5 | 0.5 | 0.7705 | 0.4426 | 0.0328 | 47/61 |
| 1.5 | 0.75 | 0.7869 | 0.4590 | 0.0164 | 48/61 |
| 1.5 | 1.0 | 0.8033 | 0.4754 | 0.0000 | 49/61 |
| 2.0 | 0.5 | 0.6393 | 0.5738 | 0.0328 | 39/61 |
| 2.0 | 0.75 | 0.6557 | 0.5902 | 0.0164 | 40/61 |
| 2.0 | 1.0 | 0.6721 | 0.6066 | 0.0000 | 41/61 |
| 2.5 | 0.5 | 0.6230 | 0.6230 | 0.0164 | 38/61 |
| 2.5 | 0.75 | 0.6393 | 0.6393 | 0.0000 | 39/61 |
| 2.5 | 1.0 | 0.6393 | 0.6393 | 0.0000 | 39/61 |
| 3.0 | 0.5 | 0.5574 | 0.7213 | 0.0000 | 34/61 |
| 3.0 | 0.75 | 0.5574 | 0.7213 | 0.0000 | 34/61 |
| 3.0 | 1.0 | 0.5574 | 0.7213 | 0.0000 | 34/61 |

## Confusion at the best point

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning | total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| Clothing | 0 | 8 | 0 | 2 | 0 | 0 | 0 | 10 |
| Fermentation | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| General | 0 | 0 | 0 | 17 | 0 | 0 | 0 | 17 |
| Health | 0 | 0 | 0 | 5 | 8 | 0 | 0 | 13 |
| Home automation | 0 | 0 | 0 | 1 | 0 | 9 | 0 | 10 |
| Language learning | 0 | 0 | 0 | 1 | 0 | 0 | 8 | 9 |

