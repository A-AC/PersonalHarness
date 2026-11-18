# Routing bake-off: hardening_final_routing_qwen

- Mode: live
- Scenarios: 65

| strategy | split | available | accuracy | wrong-domain | General fallback | fallbacks | p50 ms | p95 ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical | base | yes | 0.9273 | 0.0000 | 0.3818 | 0 | 0.57 | 0.62 |
| lexical | heldout | yes | 0.2000 | 0.0000 | 0.8000 | 0 | 0.5 | 0.61 |
| llm | base | yes | 0.9273 | 0.0545 | 0.2727 | 4 | 999.62 | 1276.8 |
| llm | heldout | yes | 0.9000 | 0.0000 | 0.1000 | 1 | 1021.72 | 1264.81 |

## Confusion matrices

### lexical / base

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Clothing | 0 | 8 | 0 | 1 | 0 | 0 | 0 |
| Fermentation | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| General | 0 | 0 | 0 | 17 | 0 | 0 | 0 |
| Health | 0 | 0 | 0 | 3 | 8 | 0 | 0 |
| Home automation | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| Language learning | 0 | 0 | 0 | 0 | 0 | 0 | 8 |

### lexical / heldout

| expected \ predicted | Clothing | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clothing | 0 | 2 | 0 | 0 | 0 |
| General | 0 | 0 | 0 | 0 | 0 |
| Health | 0 | 3 | 0 | 0 | 0 |
| Home automation | 0 | 1 | 0 | 2 | 0 |
| Language learning | 0 | 2 | 0 | 0 | 0 |

### llm / base

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Clothing | 0 | 9 | 0 | 0 | 0 | 0 | 0 |
| Fermentation | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| General | 1 | 0 | 1 | 14 | 0 | 1 | 0 |
| Health | 0 | 0 | 0 | 0 | 11 | 0 | 0 |
| Home automation | 0 | 0 | 0 | 1 | 0 | 7 | 0 |
| Language learning | 0 | 0 | 0 | 0 | 0 | 0 | 8 |

### llm / heldout

| expected \ predicted | Clothing | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clothing | 1 | 1 | 0 | 0 | 0 |
| General | 0 | 0 | 0 | 0 | 0 |
| Health | 0 | 0 | 3 | 0 | 0 |
| Home automation | 0 | 0 | 0 | 3 | 0 |
| Language learning | 0 | 0 | 0 | 0 | 2 |

## Notes

- Wrong-domain means a specific domain that differs from the label; General fallback is a miss, not a wrong-domain result.
