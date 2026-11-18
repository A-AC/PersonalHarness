# Routing sweep: phase10b_routing

- Corpus: `eval/scenarios/k_routing.yaml`
- Cases: 50
- Thresholds: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0
- Margins: 0.5, 0.75, 1.0
- Best: threshold 1.0, margin 1.0
- Accuracy: 0.9400 (47/50)
- General fallback rate: 0.3400
- False-positive rate: 0.0000
- General-only control accuracy: 0.2800 (14/50)

Tie-break: higher threshold, then lower margin.

## Grid

| threshold | margin | accuracy | general fallback | false positive | correct |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5 | 0.5 | 0.9000 | 0.3000 | 0.0400 | 45/50 |
| 0.5 | 0.75 | 0.9200 | 0.3200 | 0.0200 | 46/50 |
| 0.5 | 1.0 | 0.9400 | 0.3400 | 0.0000 | 47/50 |
| 1.0 | 0.5 | 0.9000 | 0.3000 | 0.0400 | 45/50 |
| 1.0 | 0.75 | 0.9200 | 0.3200 | 0.0200 | 46/50 |
| 1.0 | 1.0 | 0.9400 | 0.3400 | 0.0000 | 47/50 |
| 1.5 | 0.5 | 0.8400 | 0.3600 | 0.0400 | 42/50 |
| 1.5 | 0.75 | 0.8600 | 0.3800 | 0.0200 | 43/50 |
| 1.5 | 1.0 | 0.8800 | 0.4000 | 0.0000 | 44/50 |
| 2.0 | 0.5 | 0.6800 | 0.5200 | 0.0400 | 34/50 |
| 2.0 | 0.75 | 0.7000 | 0.5400 | 0.0200 | 35/50 |
| 2.0 | 1.0 | 0.7200 | 0.5600 | 0.0000 | 36/50 |
| 2.5 | 0.5 | 0.6600 | 0.5800 | 0.0200 | 33/50 |
| 2.5 | 0.75 | 0.6800 | 0.6000 | 0.0000 | 34/50 |
| 2.5 | 1.0 | 0.6800 | 0.6000 | 0.0000 | 34/50 |
| 3.0 | 0.5 | 0.5800 | 0.7000 | 0.0000 | 29/50 |
| 3.0 | 0.75 | 0.5800 | 0.7000 | 0.0000 | 29/50 |
| 3.0 | 1.0 | 0.5800 | 0.7000 | 0.0000 | 29/50 |

## Confusion at the best point

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning | total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| Clothing | 0 | 7 | 0 | 1 | 0 | 0 | 0 | 8 |
| Fermentation | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| General | 0 | 0 | 0 | 14 | 0 | 0 | 0 | 14 |
| Health | 0 | 0 | 0 | 2 | 8 | 0 | 0 | 10 |
| Home automation | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 8 |
| Language learning | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 |

