"""Phase 10F: constrained model-assisted domain tie-breaking."""

from __future__ import annotations

import copy
import time
from typing import Any

import pytest

from harness.model import ModelReply, ScriptedModel


WEAK_MESSAGE = "Please help me decide what to do next."


def _health_domain(app) -> dict[str, Any]:
    return app.domains.create(
        "dom_health",
        name="Health",
        description="nutrition and exercise planning",
        keywords=["nutrition", "protein"],
        created_by="test",
    )


def _routing_event(app) -> dict[str, Any]:
    events = [
        event for event in app.events.list(limit=100)
        if event["type"] == "domain.routed"
    ]
    assert events
    return events[-1]


def test_on_mode_uses_model_choice_for_weak_evidence(make_app):
    model = ScriptedModel(scripts=[
        {"content": "dom_health"},
        {"content": "done"},
    ])
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)

    result = app.runtime.submit_user_message(WEAK_MESSAGE)

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_health"
    assert len(model.calls) == 2
    event = _routing_event(app)
    assert event["domain_id"] == "dom_health"
    assert event["data"]["model_choice"] == "dom_health"
    assert event["data"]["model_route"] == "on"
    assert "model tie-break" in event["data"]["reason"]
    routing = app.tasks.meta(result.task_id)["routing"]
    assert routing["model_choice"] == "dom_health"
    assert routing["model_route"] == "on"


def test_shadow_mode_records_choice_without_changing_task_domain(make_app):
    model = ScriptedModel(scripts=[{"content": "dom_health"}], default="done")
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "shadow")
    _health_domain(app)

    result = app.runtime.submit_user_message(WEAK_MESSAGE)

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "general"
    event = _routing_event(app)
    assert event["domain_id"] == "general"
    assert event["data"]["model_choice"] == "dom_health"
    assert event["data"]["model_route"] == "shadow"
    assert event["data"]["reason"] == "no domain evidence above threshold"
    assert not any(
        item["type"] == "task.domain" for item in app.events.list(limit=100)
    )


def test_off_mode_never_calls_classifier(make_app):
    model = ScriptedModel(default="done")
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "off")
    _health_domain(app)

    result = app.runtime.submit_user_message(WEAK_MESSAGE)

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "general"
    assert len(model.calls) == 1
    event = _routing_event(app)
    assert event["data"]["model_choice"] is None
    assert event["data"]["model_route"] == "off"


def test_clear_deterministic_winner_does_not_call_classifier(make_app):
    model = ScriptedModel(scripts=[{"content": "dom_health"}], default="done")
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)

    result = app.runtime.submit_user_message("Show the nutrition label")

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_health"
    assert len(model.calls) == 1
    event = _routing_event(app)
    assert event["data"]["model_choice"] is None
    assert event["data"]["model_route"] == "off"


@pytest.mark.parametrize("reply", ["banana", "dom_unknown", ""])
def test_invalid_unknown_or_empty_model_output_falls_back_to_general(
    make_app, reply: str,
):
    model = ScriptedModel(scripts=[
        {"content": reply},
        {"content": "done"},
    ])
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)

    result = app.runtime.submit_user_message(WEAK_MESSAGE)

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "general"
    event = _routing_event(app)
    assert event["domain_id"] == "general"
    assert event["data"]["model_choice"] is None
    assert event["data"]["model_route"] == "on"
    assert "invalid or unavailable" in event["data"]["reason"]


class _SlowClassifierModel:
    """Fake model whose constrained classifier exceeds the routing budget."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        thinking: bool | None = None,
        recovery: bool = True,
        model: str | None = None,
        provider: str | None = None,
    ) -> ModelReply:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "thinking": thinking,
            "recovery": recovery,
        })
        if not tools and int(max_tokens or 0) <= 16:
            time.sleep(1.0)
            return ModelReply(content="dom_health")
        return ModelReply(content="done")

    def probe(self) -> dict[str, Any]:
        return {"reachable": True, "model_listed": True, "chat_ok": True}

    def list_models(self, provider_id: str | None = None) -> dict[str, Any]:
        return {"provider": provider_id or "default", "models": []}


def test_classifier_timeout_returns_general_promptly(make_app):
    model = _SlowClassifierModel()
    app = make_app.build(model=model)  # type: ignore[arg-type]
    app.config.set("domains.model_routing", "on")
    app.config.set("domains.model_routing_timeout_s", 0.03)
    _health_domain(app)

    started = time.monotonic()
    result = app.runtime.submit_user_message(WEAK_MESSAGE)
    elapsed = time.monotonic() - started

    assert result.status == "completed"
    assert elapsed < 0.5
    assert app.tasks.get(result.task_id)["domain_id"] == "general"
    event = _routing_event(app)
    assert event["data"]["model_choice"] is None
    assert event["data"]["model_route"] == "on"


def test_untrusted_origin_and_tainted_metadata_never_call_classifier(make_app):
    model = ScriptedModel(default="done")
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)

    result = app.runtime.submit_user_message(WEAK_MESSAGE, source="inbox")

    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "general"
    assert len(model.calls) == 1  # only the normal task reply

    before = len(model.calls)
    routing = app.runtime._route_domain(WEAK_MESSAGE, trusted=False)
    assert routing["domain_id"] == "general"
    assert len(model.calls) == before

    routing = app.runtime._route_domain(
        WEAK_MESSAGE,
        task_meta={"tainted_sources": [{"origin": "inbox:mail"}]},
    )
    assert routing["domain_id"] == "general"
    assert len(model.calls) == before

    routing = app.runtime._route_domain(
        WEAK_MESSAGE,
        trigger={"kind": "inbox", "tainted_sources": [{"origin": "mail"}]},
    )
    assert routing["domain_id"] == "general"
    assert len(model.calls) == before


def test_preview_route_stays_deterministic(make_app):
    model = ScriptedModel(default="done")
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)
    before_events = len(app.events.list(limit=100))

    routing = app.runtime.preview_route(WEAK_MESSAGE)

    assert routing["domain_id"] == "general"
    assert len(model.calls) == 0
    assert len(app.events.list(limit=100)) == before_events


def test_classifier_prompt_is_compact_constrained_and_tool_free(make_app):
    model = ScriptedModel(scripts=[
        {"content": "dom_health"},
        {"content": "done"},
    ])
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)

    app.runtime.submit_user_message(WEAK_MESSAGE)

    assert len(model.calls) == 2
    classifier = model.calls[0]
    assert classifier["tools"] == []
    assert int(classifier["max_tokens"] or 0) <= 32
    assert classifier["thinking"] is False
    prompt = "\n".join(
        str(message.get("content") or "") for message in classifier["messages"]
    )
    assert (
        "Health (dom_health): nutrition and exercise planning | keywords: "
        "nutrition, protein"
    ) in prompt
    assert WEAK_MESSAGE in prompt
    assert "exactly one domain id or general" in prompt
    assert "parameters" not in prompt
    assert "properties" not in prompt


def test_classifier_prompt_is_capped_for_many_domains(make_app):
    app = make_app.build(default="ok")
    ids = []
    for index in range(15):
        domain = app.domains.create(
            f"dom_many_{index}", name=f"Domain {index:02d}",
            description="long description " * 20,
            keywords=[f"term{index}", f"other{index}"],
            created_by="test",
        )
        ids.append(domain["id"])
    active = [
        item for item in app.domains.list_active()
        if item["id"] != "general"
    ]
    routing = {"candidates": [{"domain_id": ids[14]}]}
    selected = app.runtime._classifier_domains(routing, active)
    assert len(selected) == 8
    assert selected[0]["id"] == ids[14]
    prompt = app.runtime._model_routing_prompt("weak evidence", selected)
    assert len(prompt) <= 4000
    assert "Domain 14" in prompt
    assert "Domain 07" not in prompt


def test_classifier_cannot_change_domains_permissions_or_approvals(make_app):
    model = ScriptedModel(scripts=[
        {"content": "dom_health"},
        {"content": "done"},
    ])
    app = make_app.build(model=model)
    app.config.set("domains.model_routing", "on")
    _health_domain(app)
    domains_before = copy.deepcopy(app.domains.list_all())
    overrides_before = copy.deepcopy(app.config.get("tools.risk_overrides", {}))

    result = app.runtime.submit_user_message(WEAK_MESSAGE)

    assert result.status == "completed"
    assert app.domains.list_all() == domains_before
    assert app.config.get("tools.risk_overrides", {}) == overrides_before
    assert app.approvals.pending() == []
