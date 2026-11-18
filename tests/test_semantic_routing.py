"""Semantic routing strategies, fallback, shadow, and user-only routing."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import util
from harness.routing.embedding import EmbeddingClient


class _EmbeddingHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        inputs = payload.get("input") or []
        data = [
            {"embedding": [float(index + 1), 0.5, 0.25]}
            for index, _ in enumerate(inputs)
        ]
        body = json.dumps({"data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def test_embedding_client_caches_packed_float32_vectors(make_app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = make_app.build()
        app.config.set("embeddings.enabled", True)
        app.config.set(
            "embeddings.base_url", f"http://127.0.0.1:{server.server_port}"
        )
        client = EmbeddingClient(app.config, app.db)
        first = client.encode(["calories", "protein"])
        second = client.encode(["calories", "protein"])
        assert first == second
        assert _EmbeddingHandler.calls == 1
        rows = list(app.db.query("SELECT * FROM embedding_cache"))
        assert len(rows) == 2
        assert int(rows[0]["dimension"]) == 3
        assert isinstance(rows[0]["vector"], (bytes, bytearray))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _health(app):
    return app.domains.create(
        "dom_health", name="Health", description="nutrition and exercise",
        keywords=["calories"], summary="Diet and fitness work.",
        created_by="test",
    )


def test_domain_card_contains_summary_and_examples(make_app):
    app = make_app.build()
    domain = _health(app)
    from harness.routing.cards import domain_card
    card = domain_card(domain)
    assert "Health" in card["text"]
    assert "nutrition" in card["text"]
    assert "Summary: Diet and fitness work." in card["text"]


def test_embedding_disabled_falls_back_to_lexical(make_app):
    app = make_app.build(scripts=[{"content": "ok"}])
    _health(app)
    app.config.set("routing.strategy", "embedding")
    result = app.runtime.submit_user_message("calories in a big mac")
    assert result.status == "completed"
    events = [event["type"] for event in app.events.list()]
    assert "router.fallback" in events
    routed = [
        event for event in app.events.list()
        if event["type"] == "domain.routed"
    ][-1]
    assert routed["data"]["fallback"] == "lexical"
    assert routed["data"]["strategy"] == "lexical"


def test_llm_strategy_accepts_strict_json_domain(make_app):
    app = make_app.build(scripts=[
        {"content": '{"domain":"dom_health","confidence":"high"}'},
        {"content": "ok"},
    ])
    _health(app)
    app.config.set("routing.strategy", "llm")
    result = app.runtime.submit_user_message("what should I eat tonight")
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == "dom_health"
    routed = [
        event for event in app.events.list()
        if event["type"] == "domain.routed"
    ][-1]
    assert routed["data"]["strategy"] == "llm"
    assert routed["data"]["confidence"] == "high"
    assert routed["data"]["top3"]
    assert routed["data"]["latency_ms"] is not None
    assert routed["data"]["inputs"]["message"]
    routing = app.tasks.meta(result.task_id)["routing"]
    assert routing["strategy"] == "llm"
    assert routing["confidence"] == "high"


def test_llm_invalid_output_routes_general_and_records_fallback(make_app):
    app = make_app.build(scripts=[
        {"content": "not json"},
        {"content": "ok"},
    ])
    _health(app)
    app.config.set("routing.strategy", "llm")
    result = app.runtime.submit_user_message("what should I eat tonight")
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == "general"
    events = [event["type"] for event in app.events.list()]
    assert "router.fallback" in events
    routed = [
        event for event in app.events.list()
        if event["type"] == "domain.routed"
    ][-1]
    assert routed["data"]["fallback"] == "invalid_output"


def test_shadow_strategy_records_without_acting(make_app):
    app = make_app.build(scripts=[
        {"content": '{"domain":"dom_health","confidence":"high"}'},
        {"content": "ok"},
    ])
    _health(app)
    app.config.set("routing.strategy", "lexical")
    app.config.set("routing.shadow_strategies", ["llm"])
    result = app.runtime.submit_user_message("what should I eat tonight")
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == "general"
    routed = [
        event for event in app.events.list()
        if event["type"] == "domain.routed"
    ][-1]
    shadow = routed["data"]["shadow"]
    assert shadow and shadow[0]["strategy"] == "llm"
    assert shadow[0]["domain_id"] == "dom_health"


def test_inbox_task_keeps_configured_domain_and_never_routes(make_app):
    app = make_app.build(scripts=[{"content": "ok"}])
    domain = _health(app)
    app.inbox.create_automation(
        "health inbox", source="mail", kind="message",
        action="task", prompt_template="Handle this health note",
        domain_id=domain["id"],
    )
    app.inbox.receive(
        source="mail", external_id="m1", title="Health note",
        body="protein note", kind="message",
    )
    report = app.inbox.triage(max_runs=1)
    assert report["runs_started"] == 1
    task = app.tasks.list_recent(limit=1)[0]
    assert task["domain_id"] == domain["id"]
    assert not [
        event for event in app.events.list()
        if event["type"] == "domain.routed"
    ]


class _KeywordVectorClient:
    """Fake embeddings: Alpha -> [1,0], Beta -> [0,1], else query [1,0]."""

    model = "fake"

    def encode(self, texts):
        out = []
        for text in texts:
            lowered = text.lower()
            if "beta" in lowered:
                out.append([0.0, 1.0])
            elif "alpha" in lowered:
                out.append([1.0, 0.0])
            else:
                out.append([1.0, 0.0])
        return out


class _CountingLLM:
    def __init__(self, domain):
        self.domain = domain
        self.calls = 0

    def classify(self, text, cards, turns):
        self.calls += 1
        return self.domain, "high", None, ""


def _two_domains(app):
    app.domains.create("dom_alpha", name="Alpha", created_by="test")
    app.domains.create("dom_beta", name="Beta", created_by="test")


def test_hybrid_uses_embedding_when_margin_is_large(make_app):
    app = make_app.build(scripts=[{"content": "ok"}])
    _two_domains(app)
    app.config.set("routing.strategy", "hybrid")
    app.config.set("routing.hybrid_margin", 0.15)
    app.runtime.router.embedding_client = _KeywordVectorClient()
    llm = _CountingLLM("dom_beta")
    app.runtime.router.llm_classifier = llm
    result = app.runtime.submit_user_message("alpha task")
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_alpha"
    assert llm.calls == 0


def test_hybrid_asks_llm_when_margin_is_narrow(make_app):
    app = make_app.build(scripts=[{"content": "ok"}])
    _two_domains(app)

    class _SameVectors:
        model = "fake"
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]

    app.config.set("routing.strategy", "hybrid")
    app.config.set("routing.hybrid_margin", 0.15)
    app.runtime.router.embedding_client = _SameVectors()
    llm = _CountingLLM("dom_beta")
    app.runtime.router.llm_classifier = llm
    result = app.runtime.submit_user_message("alpha task")
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_beta"
    assert llm.calls == 1
