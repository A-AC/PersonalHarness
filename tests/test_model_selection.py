"""Model/provider selection: validation, catalogue, and live config changes."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from harness import providers, util
from harness.app import build_app
from harness.config import (
    EDITABLE_KEYS,
    ConfigError,
    load_config,
    validate_allowed_models,
    validate_group_update,
)
from harness.model import ModelClient, ScriptedModel


def _chat_body(content: str = "ok") -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


def _write_config(path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_default_provider_comes_from_model_endpoint(tmp_path):
    config = load_config(tmp_path)
    config.set("model.base_url", "http://model.test")
    config.set("model.api_key", "secret")

    records = providers.provider_records(config)

    assert len(records) == 1
    assert records[0].id == "default"
    assert records[0].base_url == "http://model.test"
    assert records[0].api_key == "secret"
    assert records[0].builtin is True
    assert config.get("model.provider") == ""


def test_providers_config_validation(tmp_path):
    invalid_configs = [
        {"providers": [{"id": "fast", "base_url": "http://fast", "extra": 1}]},
        {"providers": [{"id": "fast"}]},
        {
            "providers": [
                {"id": "fast", "base_url": "http://fast"},
                {"id": "fast", "base_url": "http://other"},
            ],
        },
        {"providers": [{"id": "fast", "base_url": "http://fast", "models": 1}]},
    ]
    for index, value in enumerate(invalid_configs):
        config_dir = tmp_path / f"invalid-{index}"
        config_dir.mkdir()
        _write_config(config_dir / "config.yaml", value)
        with pytest.raises(ConfigError):
            load_config(config_dir)

    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    _write_config(valid_dir / "config.yaml", {
        "providers": [{
            "id": "fast",
            "base_url": "http://fast",
            "api_key_env": "FAST_MODEL_KEY",
            "models": ["mini"],
        }],
    })
    config = load_config(valid_dir)
    assert config.get("providers")[0]["id"] == "fast"

    normalised = validate_group_update(config, "providers", [{
        "id": "lan",
        "base_url": "http://lan",
    }])
    assert normalised == [{
        "id": "lan",
        "base_url": "http://lan",
        "api_key_env": "",
        "models": [],
    }]
    assert "providers" in EDITABLE_KEYS


def test_model_allowed_validation():
    assert validate_allowed_models(["a", "b"]) == ["a", "b"]
    assert validate_allowed_models("a") == ["a"]
    assert validate_allowed_models([" a ", "a", "b"]) == ["a", "b"]
    with pytest.raises(ConfigError):
        validate_allowed_models([1])
    with pytest.raises(ConfigError):
        validate_allowed_models([""])


def test_models_catalogue_intersects_allowlist(tmp_path):
    config = load_config(tmp_path)
    config.set("model.allowed", ["b", "c"])
    config.set("model.name", "qwen3.5-9b")
    model = ScriptedModel(models=["a", "b", "c"])

    catalogue = providers.provider_catalogue(config, model.list_models)

    names = [entry["name"] for entry in catalogue["models"]]
    assert "b" in names
    assert "c" in names
    assert "qwen3.5-9b" in names
    current = next(
        entry for entry in catalogue["models"]
        if entry["name"] == "qwen3.5-9b"
    )
    assert current["source"] == "configured"
    assert catalogue["providers"][0]["reachable"] is True


def test_model_client_reads_name_per_call(tmp_path):
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_body())

    config = load_config(tmp_path)
    config.set("model.base_url", "http://model.test")
    client = ModelClient(config, transport=httpx.MockTransport(handler))

    client.chat([{"role": "user", "content": "first"}], tools=[])
    config.set("model.name", "other")
    client.chat([{"role": "user", "content": "second"}], tools=[])
    client.chat(
        [{"role": "user", "content": "third"}], tools=[], model="override",
    )

    assert [payload["model"] for payload in payloads] == [
        "qwen3.5-9b", "other", "override",
    ]
    client.close()


def test_model_client_uses_selected_provider(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "mini"}]})
        return httpx.Response(200, json=_chat_body())

    config = load_config(tmp_path)
    config.set("providers", [{
        "id": "fast",
        "base_url": "http://fast.test",
        "models": ["mini"],
    }])
    client = ModelClient(config, transport=httpx.MockTransport(handler))

    client.chat(
        [{"role": "user", "content": "hello"}],
        tools=[],
        model="mini",
        provider="fast",
    )
    result = client.list_models("fast")

    assert str(seen[0].url) == "http://fast.test/v1/chat/completions"
    assert json.loads(seen[0].content)["model"] == "mini"
    assert result["provider"] == "fast"
    assert result["models"] == ["mini"]
    client.close()


def test_list_models_reports_unreachable_cleanly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    config = load_config(tmp_path)
    config.set("model.base_url", "http://model.test")
    client = ModelClient(config, transport=httpx.MockTransport(handler))

    result = client.list_models()

    assert result["reachable"] is False
    assert result["error"]
    client.close()


def test_set_model_switches_model_and_records_event(tmp_path):
    model = ScriptedModel(models=["a", "b"])
    app = build_app(tmp_path, model=model)

    result = app.set_model("b")

    assert app.config.get("model.name") == "b"
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["name"] == "b"
    event = next(
        event for event in app.events.list(limit=200)
        if event["type"] == "model.changed"
    )
    assert event["data"]["from"] == "qwen3.5-9b"
    assert event["data"]["to"] == "b"
    assert result["restart_required"] is False
    assert result["verified"] is True


def test_set_model_refuses_unknown_name_and_provider(tmp_path):
    app = build_app(tmp_path, model=ScriptedModel(models=["a", "b"]))
    before = app.config.as_dict()

    with pytest.raises(providers.ModelSelectionError) as name_error:
        app.set_model("unknown")
    assert name_error.value.status == 400
    assert app.config.as_dict() == before

    with pytest.raises(providers.ModelSelectionError) as provider_error:
        app.set_model("a", "nope")
    assert provider_error.value.status == 400
    assert app.config.as_dict() == before


def test_unreachable_provider_reports_cleanly_and_uses_static_allowlist(
    tmp_path,
):
    app = build_app(
        tmp_path,
        model=ScriptedModel(models=["a"], reachable=False),
    )
    app.config.set("providers", [{
        "id": "lan",
        "base_url": "http://lan.test",
        "models": ["a"],
    }])
    app.config.set("model.provider", "lan")

    catalogue = app.model_catalogue()
    provider = next(
        item for item in catalogue["providers"] if item["id"] == "lan"
    )
    assert provider["reachable"] is False
    assert provider["error"]

    result = app.set_model("a")
    assert result["verified"] is False

    with pytest.raises(providers.ModelSelectionError) as error:
        app.set_model("zzz")
    assert error.value.status == 503
    assert "lan" in str(error.value)


def test_run_meta_helper_and_configured_provider_id(tmp_path):
    config = load_config(tmp_path)
    assert providers.configured_provider_id(config) == "default"

    config.set("model.provider", "fast")
    assert providers.configured_provider_id(config) == "fast"


def test_models_and_model_api_contract(make_app, client):
    app = make_app.build(model=ScriptedModel(models=["a", "b"]))
    http = client(app)

    catalogue = http.get("/api/models")
    assert catalogue.status_code == 200
    payload = catalogue.json()
    assert payload["current"]["provider"] == "default"
    assert payload["current"]["name"] == app.config.get("model.name")
    assert isinstance(payload["current"]["context_window"], int)
    assert payload["providers"]
    assert {"a", "b"} <= {
        entry["name"] for entry in payload["models"]
    }
    assert payload["restart_required"] is False

    changed = http.post("/api/model", json={"name": "b"})
    assert changed.status_code == 200
    result = changed.json()
    assert result["changed"] is True
    assert result["verified"] is True
    assert result["restart_required"] is False
    assert app.config.get("model.name") == "b"
    assert any(
        event["type"] == "model.changed"
        for event in app.events.list(limit=200)
    )

    summary = http.get("/api/summary")
    assert summary.status_code == 200
    assert summary.json()["model"]["name"] == "b"


def test_model_catalogue_handles_missing_configured_provider(make_app, client):
    app = make_app.build(model=ScriptedModel(models=["a"]))
    app.config.set("model.provider", "gone")
    configured_name = app.config.get("model.name")

    catalogue = app.model_catalogue()

    assert catalogue["current"]["provider"] == "gone"
    assert "gone" in catalogue["current"]["error"]
    current_entries = [
        entry for entry in catalogue["models"] if entry["current"]
    ]
    assert [entry["name"] for entry in current_entries] == [configured_name]

    response = client(app).get("/api/models")
    assert response.status_code == 200
    assert response.json()["current"]["provider"] == "gone"


def test_model_api_requires_auth(make_app):
    from starlette.testclient import TestClient

    from harness.server import Server

    app = make_app.build(model=ScriptedModel(models=["a", "b"]))
    http = TestClient(Server(app).routes())
    assert http.get("/api/models").status_code == 401
    assert http.post("/api/model", json={"name": "b"}).status_code == 401


def test_model_api_refuses_invalid_and_unknown(make_app, client):
    app = make_app.build(model=ScriptedModel(models=["a", "b"]))
    http = client(app)
    original = app.config.get("model.name")

    unknown = http.post("/api/model", json={"name": "zzz"})
    assert unknown.status_code == 400
    assert app.config.get("model.name") == original

    unknown_provider = http.post(
        "/api/model", json={"name": "a", "provider": "nope"}
    )
    assert unknown_provider.status_code == 400
    assert app.config.get("model.name") == original

    missing = http.post("/api/model", json={})
    assert missing.status_code == 400
    assert app.config.get("model.name") == original


def test_model_api_reports_unreachable_provider_cleanly(make_app, client):
    app = make_app.build(model=ScriptedModel(models=[], reachable=False))
    http = client(app)
    original = app.config.get("model.name")

    catalogue = http.get("/api/models")
    assert catalogue.status_code == 200
    provider = catalogue.json()["providers"][0]
    assert provider["reachable"] is False
    assert provider["error"]

    changed = http.post("/api/model", json={"name": "anything"})
    assert changed.status_code == 503
    assert provider["id"] in changed.json()["error"]
    assert "Traceback" not in changed.json()["error"]
    assert app.config.get("model.name") == original


def test_run_records_model_and_provider(make_app, client):
    app = make_app.build(scripts=[{"content": "done"}])
    result = app.runtime.submit_user_message("record the model", source="web")
    assert result.status == "completed"

    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )
    meta = util.loads(run["meta"] or "{}", {})
    assert meta["model"] == app.config.get("model.name")
    assert meta["provider"] == "default"

    detail = client(app).get(f"/api/tasks/{result.task_id}")
    assert detail.status_code == 200
    run_payload = detail.json()["runs"][0]
    assert run_payload["model"] == meta["model"]
    assert run_payload["provider"] == meta["provider"]


def test_run_in_flight_keeps_its_snapshot_model(make_app):
    class SwitchingModel(ScriptedModel):
        def __init__(self):
            super().__init__(scripts=[
                {"tool_calls": [{
                    "name": "search_knowledge",
                    "arguments": {"query": "rack"},
                }]},
                {"content": "done"},
            ])
            self.seen: list[str | None] = []
            self.config = None

        def chat(self, messages, tools, *, model=None, **kwargs):
            self.seen.append(model)
            if len(self.seen) == 1 and self.config is not None:
                self.config.set("model.name", "second")
            return super().chat(messages, tools, model=model, **kwargs)

    model = SwitchingModel()
    app = make_app.build(model=model)
    app.config.set("model.name", "original")
    model.config = app.config

    result = app.runtime.submit_user_message("check the rack", source="web")
    assert result.status == "completed"
    assert model.seen == ["original", "original"]
    assert app.config.get("model.name") == "second"

    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )
    meta = util.loads(run["meta"] or "{}", {})
    assert meta["model"] == "original"
    assert meta["provider"] == "default"


def test_resume_uses_new_selection_and_records_the_change(
    make_app, client
):
    ask = "<" + "ask_user" + "/>"
    model = ScriptedModel(
        scripts=[
            {"content": f"Which rack? {ask}"},
            {"content": "Got it."},
        ],
        models=["a", "b"],
    )
    app = make_app.build(model=model)
    http = client(app)
    original = app.config.get("model.name")

    first = app.runtime.submit_user_message("check the rack", source="web")
    assert first.status == "waiting_user"

    changed = http.post("/api/model", json={"name": "b"})
    assert changed.status_code == 200
    assert changed.json()["changed"] is True

    resumed = app.runtime.submit_user_message("the network rack", source="cli")
    assert resumed.task_id == first.task_id
    assert resumed.run_id == first.run_id
    assert resumed.status == "completed"
    assert model.calls[-1]["model"] == "b"

    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (first.run_id,)
    )
    meta = util.loads(run["meta"] or "{}", {})
    assert meta["model"] == "b"
    assert meta["provider"] == "default"

    events = app.events.list(limit=200)
    resume_event = next(
        event for event in events
        if event["type"] == "model.changed"
        and event["data"].get("source") == "resume"
    )
    assert resume_event["data"]["from"] == original
    assert resume_event["data"]["to"] == "b"
    model_call = next(
        event for event in events
        if event["type"] == "model.call"
        and event["data"].get("model") == "b"
    )
    assert model_call["data"]["provider"] == "default"


def test_model_picker_is_wired_into_the_shell(make_app, client):
    app = make_app.build()
    http = client(app)

    page = http.get("/")
    assert page.status_code == 200
    assert 'id="modelpick"' in page.text
    assert 'data-tab="settings"' in page.text

    script = http.get("/assets/app.js").text
    assert "/api/models" in script
    assert "/api/model" in script
    assert "renderModelPicker" in script
    assert "modelCard" in script
    assert "modelOptionsHtml" in script
    assert "modelselect" in script
    assert "/api/guard" not in script

    style = http.get("/assets/style.css").text
    assert "#modelpick" in style
    assert "modelselect" in style
