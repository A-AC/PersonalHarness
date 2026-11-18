# Routing bake-off: routing_bakeoff_live

- Mode: live
- Scenarios: 5

| strategy | split | available | accuracy | wrong-domain | General fallback | fallbacks | p50 ms | p95 ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical | base | yes | 1.0000 | 0.0000 | 0.0000 | 0 | 0.47 | 0.47 |
| lexical | heldout | yes | 0.0000 | 0.0000 | 1.0000 | 0 | 0.45 | 0.45 |
| embedding | - | no | - | - | - | - | - | - |
| llm | base | yes | 1.0000 | 0.0000 | 0.0000 | 0 | 1158.15 | 1199.51 |
| llm | heldout | yes | 1.0000 | 0.0000 | 0.0000 | 0 | 1017.91 | 1017.91 |
| hybrid | - | no | - | - | - | - | - | - |

## Confusion matrices

### lexical / base

| expected \ predicted | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: |
| Health | 2 | 0 | 0 |
| Home automation | 0 | 1 | 0 |
| Language learning | 0 | 0 | 1 |

### lexical / heldout

| expected \ predicted | General | Health |
| --- | ---: | ---: |
| General | 0 | 0 |
| Health | 1 | 0 |

### llm / base

| expected \ predicted | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: |
| Health | 2 | 0 | 0 |
| Home automation | 0 | 1 | 0 |
| Language learning | 0 | 0 | 1 |

### llm / heldout

| expected \ predicted | Health |
| --- | ---: |
| Health | 1 |

## Notes

- `embedding` unavailable: embedding endpoint unavailable: embeddings are disabled
- `hybrid` unavailable: embedding endpoint unavailable: embeddings are disabled
- Wrong-domain means a specific domain that differs from the label; General fallback is a miss, not a wrong-domain result.
