"""Domain cards: the bounded text every semantic strategy classifies."""

from __future__ import annotations

from typing import Any


def domain_card(domain: dict[str, Any]) -> dict[str, Any]:
    examples = [str(item) for item in (domain.get("examples") or []) if str(item)]
    summary = str(domain.get("summary") or "").strip()
    text = "\n".join(
        part for part in (
            str(domain.get("name") or ""),
            str(domain.get("description") or ""),
            ("Examples: " + "; ".join(examples)) if examples else "",
            ("Summary: " + summary) if summary else "",
        ) if part.strip()
    )
    return {
        "domain_id": str(domain.get("id") or ""),
        "name": str(domain.get("name") or ""),
        "description": str(domain.get("description") or ""),
        "examples": examples,
        "summary": summary,
        "text": text,
    }


def domain_cards(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        domain_card(domain) for domain in domains
        if str(domain.get("id") or "") != "general"
        and str(domain.get("status") or "active") == "active"
    ]
