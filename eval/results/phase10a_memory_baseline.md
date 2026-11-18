# Cross-domain memory recall: phase 10A baseline

> Historical artifact: recorded before evidence v2 and soft-memory changes.
> Rerunning the current script produces current-mode numbers.

- Corpus: `eval/scenarios/k_memory_recall.yaml`
- Modes: off, search, boost
- Recall window: top 5
- Budget share denominator: `context.section_budgets.memories` x 4

## Summary

| mode | recall@5 | found | mean context budget share | sibling leaks |
| --- | ---: | ---: | ---: | ---: |
| off | 0.2500 | 2/8 | 0.0092 | 0 |
| search | 0.2500 | 2/8 | 0.0092 | 0 |
| boost | 0.2500 | 2/8 | 0.0092 | 0 |

## Scenarios

| scenario | mode | routed domain | expected found | budget share | sibling leak |
| --- | --- | --- | ---: | ---: | ---: |
| `k_memory_recall.general_allergy` | off | General | no | 0.0040 | no |
| `k_memory_recall.general_allergy` | search | General | no | 0.0040 | no |
| `k_memory_recall.general_allergy` | boost | General | no | 0.0040 | no |
| `k_memory_recall.general_protein` | off | General | no | 0.0040 | no |
| `k_memory_recall.general_protein` | search | General | no | 0.0040 | no |
| `k_memory_recall.general_protein` | boost | General | no | 0.0040 | no |
| `k_memory_recall.health_clothing_preference` | off | Health | no | 0.0160 | no |
| `k_memory_recall.health_clothing_preference` | search | Health | no | 0.0160 | no |
| `k_memory_recall.health_clothing_preference` | boost | Health | no | 0.0160 | no |
| `k_memory_recall.clothing_health_allergy` | off | Clothing | no | 0.0126 | no |
| `k_memory_recall.clothing_health_allergy` | search | Clothing | no | 0.0126 | no |
| `k_memory_recall.clothing_health_allergy` | boost | Clothing | no | 0.0126 | no |
| `k_memory_recall.general_units` | off | General | yes | 0.0040 | no |
| `k_memory_recall.general_units` | search | General | yes | 0.0040 | no |
| `k_memory_recall.general_units` | boost | General | yes | 0.0040 | no |
| `k_memory_recall.clothing_health_protein` | off | Clothing | no | 0.0126 | no |
| `k_memory_recall.clothing_health_protein` | search | Clothing | no | 0.0126 | no |
| `k_memory_recall.clothing_health_protein` | boost | Clothing | no | 0.0126 | no |
| `k_memory_recall.general_shoe_size` | off | General | no | 0.0040 | no |
| `k_memory_recall.general_shoe_size` | search | General | no | 0.0040 | no |
| `k_memory_recall.general_shoe_size` | boost | General | no | 0.0040 | no |
| `k_memory_recall.health_units` | off | Health | yes | 0.0160 | no |
| `k_memory_recall.health_units` | search | Health | yes | 0.0160 | no |
| `k_memory_recall.health_units` | boost | Health | yes | 0.0160 | no |

