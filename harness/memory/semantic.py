"""Pluggable semantic similarity for memory retrieval.

The baseline is deliberately dependency-free: a configurable concept expansion
plus light stemming and character n-gram similarity. It is not a vector store
and needs no model call. A deployment that has an embedding service can inject
another ``SemanticScorer`` implementation without changing the memory manager.

The core treats a missing scorer as "keyword-only", which keeps the system
fully usable when semantic retrieval is disabled.
"""

from __future__ import annotations

import re

from harness.contracts.memory import SemanticScorer
from harness.contracts.settings import SettingsService
from harness.text import STOPWORDS, word_tokens

IDENTIFIER_RE = re.compile(
    r"(?:\b\d{1,3}(?:\.\d{1,3}){3}\b"  # IPv4
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\b"  # hostname/domain-like
    r"|\b(?:[a-z]:\\|/)[^\s]+"  # path-like
    r"|\b[a-z]+_[a-z0-9_]+\b"  # snake_case id
    r"|\b[a-z]+-\d{2,}\b)",  # dash-number id
    re.IGNORECASE,
)

# A generic, editable concept vocabulary. It is configuration, not a rule
# about any particular user's projects. Each group maps to one canonical
# concept so differently-worded requests can meet the same idea.
DEFAULT_CONCEPTS = (
    "address:address,ip,host,hostname,endpoint,url,port,dns,listen,listens"
    "|server:server,machine,host,box,node,workstation,nas,computer,worker"
    "|storage:storage,backup,disk,drive,array,nas,archive,repository"
    "|location:location,where,place,room,site,floor,basement,garage,rack"
    "|preference:prefer,prefers,like,likes,want,wants,favorite,favourite,style,answer,answers,response,concise,detailed,brief,verbose"
    "|network:network,lan,subnet,vlan,wifi,ethernet,router,switch,firewall"
    "|identity:name,called,named,identity,id,user,person,owner"
    "|schedule:time,schedule,deadline,due,when,date,calendar"
    "|software:service,app,application,program,daemon,process"
    "|work:task,job,work,project"
    "|security:password,credential,credentials,secret,token,key"
    "|power:light,lights,lamp,lamps,lighting,plug,switch,zigbee"
    "|build:build,compile,compiler,cache,artifact,artifacts,toolchain"
)


def _stem(token: str) -> str:
    value = token.lower().strip()
    if len(value) <= 3:
        return value
    for suffix in ("ization", "ations", "ation", "ingly", "edly", "ies", "ied", "ing", "ers", "er", "ed", "es", "s"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            value = value[: -len(suffix)]
            break
    if len(value) > 4 and value.endswith("e"):
        value = value[:-1]
    return value


def _concept_groups(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in (raw or "").split("|"):
        if ":" not in chunk:
            continue
        concept, values = chunk.split(":", 1)
        concept = concept.strip().lower()
        if not concept:
            continue
        for item in values.split(","):
            token = item.strip().lower()
            if token:
                mapping[_stem(token)] = concept
    return mapping


class LexicalSemanticScorer:
    """Dependency-free similarity over concepts, stems, and character n-grams."""

    name = "lexical"

    def __init__(self, settings: SettingsService):
        self._settings = settings
        self._concepts: dict[str, str] | None = None
        self._concept_source = ""

    def _mapping(self) -> dict[str, str]:
        raw = str(self._settings.get("memory.retrieval.semantic_concepts") or "")
        if self._concepts is None or raw != self._concept_source:
            self._concepts = _concept_groups(raw or DEFAULT_CONCEPTS)
            self._concept_source = raw
        return self._concepts

    def _concepts_for(self, text: str) -> set[str]:
        mapping = self._mapping()
        concepts: set[str] = set()
        for token in word_tokens(text):
            if token in STOPWORDS or token.isdigit():
                continue
            stem = _stem(token)
            if len(stem) < 2:
                continue
            concepts.add(mapping.get(token) or mapping.get(stem) or stem)
        lowered = (text or "").lower()
        if IDENTIFIER_RE.search(lowered):
            # An IP/hostname/path is an address-like identifier even when the
            # statement never uses the words "address" or "IP".
            concepts.add("address")
            concepts.add("identifier")
        return concepts

    def score(self, query: str, content: str) -> float:
        left = self._concepts_for(query)
        right = self._concepts_for(content)
        if not left or not right:
            return 0.0
        shared = len(left & right)
        coverage = shared / len(left)
        # Character trigrams help with punctuation and compound-word variants
        # ("home-lab" vs "home lab") without pretending to understand language.
        char = self._char_similarity(query, content)
        return max(0.0, min(1.0, coverage * 0.85 + char * 0.15))

    @staticmethod
    def _char_similarity(left: str, right: str) -> float:
        def grams(value: str) -> set[str]:
            text = " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))
            if len(text) < 3:
                return set()
            return {text[index : index + 3] for index in range(len(text) - 2)}

        a, b = grams(left), grams(right)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


class NoSemanticScorer:
    name = "off"

    def score(self, query: str, content: str) -> float:
        return 0.0


__all__ = ["LexicalSemanticScorer", "NoSemanticScorer", "DEFAULT_CONCEPTS", "STOPWORDS"]
