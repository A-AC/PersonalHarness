from __future__ import annotations

from harness.contracts.memory import MemoryCandidate


MEMORIES = [
    ("The NAS is called vault.", "vault"),
    ("The NAS is at 192.168.1.10.", "192.168"),
    ("The user prefers concise answers.", "concise"),
    ("The homelab rack is in the basement.", "basement"),
    ("The user's name is Alex.", "alex"),
    ("The garage lights use Zigbee.", "zigbee"),
]

QUERIES = [
    ("what is my NAS called?", "vault"),
    ("what is my name?", "alex"),
    ("where is the homelab rack?", "basement"),
    ("how do I like my answers?", "concise"),
    ("NAS address", "192.168"),
    ("garage lights protocol", "zigbee"),
]


def _seed(harness) -> None:
    interaction = harness.sessions.record_interaction("seed")
    for content, _ in MEMORIES:
        harness.memory.submit(
            MemoryCandidate(
                content=content,
                default_scope="global",
                provenance={
                    "source_kind": "user_statement",
                    "source_refs": [interaction.id],
                    "evidence_status": "stated",
                },
            )
        )


def test_retrieval_hit_rate_on_a_fixed_set(make_harness) -> None:
    h = make_harness([])
    _seed(h)
    hits = 0
    misses: list[str] = []
    for query, expected in QUERIES:
        ranked = h.memory.retrieve(query, ["global"], 3)
        text = " ".join(item.record.content.lower() for item in ranked[:1])
        if expected in text:
            hits += 1
        else:
            misses.append(f"{query!r} -> {[item.record.content for item in ranked]}")
    rate = hits / len(QUERIES)
    assert rate == 1.0, f"hit rate {rate:.0%}; misses: {misses}"


def test_recency_breaks_equal_scores(make_harness) -> None:
    # Hybrid retrieval adds semantic and recency-decay components by default,
    # which would make these two records score differently. Pin both off so the
    # test still checks the original guarantee: with equal additive scores, the
    # most recently updated memory wins.
    h = make_harness(
        [],
        settings={
            "memory": {
                "retrieval": {"semantic_weight": 0.0, "recency_weight": 0.0}
            }
        },
    )
    source = h.sessions.record_interaction("seed")
    older = h.memory.submit(
        MemoryCandidate(
            content="The NAS is called vault",
            default_scope="global",
            provenance={"source_kind": "user_statement", "source_refs": [source.id], "evidence_status": "stated"},
        )
    ).record
    newer = h.memory.submit(
        MemoryCandidate(
            content="The rack holds storage",
            default_scope="global",
            provenance={"source_kind": "user_statement", "source_refs": [source.id], "evidence_status": "stated"},
        )
    ).record
    assert older is not None and newer is not None
    ranked = h.memory.retrieve("vault storage", ["global"], 2)
    assert {item.record.id for item in ranked} == {older.id, newer.id}
    assert ranked[0].score == ranked[1].score
    assert ranked[0].record.id == newer.id
