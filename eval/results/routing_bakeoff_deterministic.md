# Routing bake-off: routing_bakeoff_deterministic

- Mode: deterministic
- Scenarios: 65

| strategy | split | available | accuracy | wrong-domain | General fallback | fallbacks | p50 ms | p95 ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical | base | yes | 0.9273 | 0.0000 | 0.3818 | 0 | 0.43 | 0.47 |
| lexical | heldout | yes | 0.2000 | 0.0000 | 0.8000 | 0 | 0.43 | 0.5 |
| embedding | base | yes | 0.2364 | 0.6727 | 0.1455 | 0 | 0.09 | 0.12 |
| embedding | heldout | yes | 0.1000 | 0.5000 | 0.4000 | 0 | 0.09 | 0.2 |
| llm | base | yes | 0.3091 | 0.0000 | 1.0000 | 55 | 0.0 | 0.0 |
| llm | heldout | yes | 0.0000 | 0.0000 | 1.0000 | 10 | 0.0 | 0.0 |
| hybrid | base | yes | 0.2727 | 0.4545 | 0.4182 | 22 | 0.11 | 0.13 |
| hybrid | heldout | yes | 0.1000 | 0.4000 | 0.5000 | 5 | 0.1 | 0.12 |

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

### embedding / base

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| Clothing | 0 | 1 | 0 | 1 | 2 | 3 | 2 |
| Fermentation | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| General | 0 | 5 | 0 | 3 | 2 | 3 | 4 |
| Health | 0 | 3 | 0 | 0 | 4 | 3 | 1 |
| Home automation | 0 | 2 | 0 | 1 | 1 | 2 | 2 |
| Language learning | 0 | 2 | 0 | 1 | 1 | 1 | 3 |

### embedding / heldout

| expected \ predicted | Clothing | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clothing | 0 | 1 | 0 | 0 | 1 |
| General | 0 | 0 | 0 | 0 | 0 |
| Health | 0 | 1 | 1 | 0 | 1 |
| Home automation | 2 | 1 | 0 | 0 | 0 |
| Language learning | 0 | 1 | 1 | 0 | 0 |

### llm / base

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| Clothing | 0 | 0 | 0 | 9 | 0 | 0 | 0 |
| Fermentation | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| General | 0 | 0 | 0 | 17 | 0 | 0 | 0 |
| Health | 0 | 0 | 0 | 11 | 0 | 0 | 0 |
| Home automation | 0 | 0 | 0 | 8 | 0 | 0 | 0 |
| Language learning | 0 | 0 | 0 | 8 | 0 | 0 | 0 |

### llm / heldout

| expected \ predicted | Clothing | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clothing | 0 | 2 | 0 | 0 | 0 |
| General | 0 | 0 | 0 | 0 | 0 |
| Health | 0 | 3 | 0 | 0 | 0 |
| Home automation | 0 | 3 | 0 | 0 | 0 |
| Language learning | 0 | 2 | 0 | 0 | 0 |

### hybrid / base

| expected \ predicted | Astronomy | Clothing | Fermentation | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Astronomy | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| Clothing | 0 | 1 | 0 | 3 | 2 | 1 | 2 |
| Fermentation | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| General | 0 | 2 | 0 | 8 | 2 | 3 | 2 |
| Health | 0 | 2 | 0 | 4 | 2 | 2 | 1 |
| Home automation | 0 | 2 | 0 | 3 | 1 | 2 | 0 |
| Language learning | 0 | 2 | 0 | 3 | 1 | 0 | 2 |

### hybrid / heldout

| expected \ predicted | Clothing | General | Health | Home automation | Language learning |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clothing | 0 | 1 | 0 | 0 | 1 |
| General | 0 | 0 | 0 | 0 | 0 |
| Health | 0 | 2 | 1 | 0 | 0 |
| Home automation | 2 | 1 | 0 | 0 | 0 |
| Language learning | 0 | 1 | 1 | 0 | 0 |

## Notes

- Deterministic mode uses scripted embedding/LLM fakes; it tests plumbing and reporting, not semantic accuracy.
- Wrong-domain means a specific domain that differs from the label; General fallback is a miss, not a wrong-domain result.
