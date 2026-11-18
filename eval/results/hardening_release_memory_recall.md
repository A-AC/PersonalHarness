# Cross-domain memory recall: hardening_release_memory_recall

- Corpus: `eval/scenarios/k_memory_recall.yaml`
- Modes: off, search, boost
- Recall window: top 5
- Budget share denominator: `context.section_budgets.memories` x 4
- Selected default: `search`
- Default corpus result: passes (recall@5 1.0000, mean budget share 0.0120, sibling leaks 0)

## Summary

| mode | recall@5 | found | mean context budget share | sibling leaks |
| --- | ---: | ---: | ---: | ---: |
| off | 0.5000 | 4/8 | 0.0092 | 0 |
| search | 1.0000 | 8/8 | 0.0120 | 0 |
| boost | 1.0000 | 8/8 | 0.0120 | 0 |

## Scenarios

| scenario | mode | routed domain | expected found | budget share | sibling leak |
| --- | --- | --- | ---: | ---: | ---: |
| `k_memory_recall.general_allergy` | off | General | no | 0.0040 | no |
| `k_memory_recall.general_allergy` | search | General | yes | 0.0103 | no |
| `k_memory_recall.general_allergy` | boost | General | yes | 0.0103 | no |
| `k_memory_recall.general_protein` | off | Health | yes | 0.0160 | no |
| `k_memory_recall.general_protein` | search | Health | yes | 0.0160 | no |
| `k_memory_recall.general_protein` | boost | Health | yes | 0.0160 | no |
| `k_memory_recall.health_clothing_preference` | off | General | no | 0.0040 | no |
| `k_memory_recall.health_clothing_preference` | search | General | yes | 0.0088 | no |
| `k_memory_recall.health_clothing_preference` | boost | General | yes | 0.0088 | no |
| `k_memory_recall.clothing_health_allergy` | off | Clothing | no | 0.0126 | no |
| `k_memory_recall.clothing_health_allergy` | search | Clothing | yes | 0.0189 | no |
| `k_memory_recall.clothing_health_allergy` | boost | Clothing | yes | 0.0189 | no |
| `k_memory_recall.general_units` | off | General | yes | 0.0040 | no |
| `k_memory_recall.general_units` | search | General | yes | 0.0040 | no |
| `k_memory_recall.general_units` | boost | General | yes | 0.0040 | no |
| `k_memory_recall.clothing_health_protein` | off | General | no | 0.0040 | no |
| `k_memory_recall.clothing_health_protein` | search | General | yes | 0.0097 | no |
| `k_memory_recall.clothing_health_protein` | boost | General | yes | 0.0097 | no |
| `k_memory_recall.general_shoe_size` | off | Clothing | yes | 0.0126 | no |
| `k_memory_recall.general_shoe_size` | search | Clothing | yes | 0.0126 | no |
| `k_memory_recall.general_shoe_size` | boost | Clothing | yes | 0.0126 | no |
| `k_memory_recall.health_units` | off | Health | yes | 0.0160 | no |
| `k_memory_recall.health_units` | search | Health | yes | 0.0160 | no |
| `k_memory_recall.health_units` | boost | Health | yes | 0.0160 | no |

