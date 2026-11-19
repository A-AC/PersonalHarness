"""Web MCP server: self-contained config precedence and tool shape."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "harness" / "modules" / "web" / "server.py"

ENV_NAMES = (
    "WEB_SEARXNG_URL", "WEB_MAX_RESULTS", "WEB_TIMEOUT_S", "WEB_CATEGORIES",
    "WEB_SAFE_SEARCH", "HARNESS_WEB_SEARXNG_URL", "HARNESS_WEB_MAX_RESULTS",
    "HARNESS_WEB_TIMEOUT_S", "HARNESS_WEB_CATEGORIES",
    "HARNESS_WEB_SAFE_SEARCH",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("web_server_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _clear_env(monkeypatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _config(path: Path, **overrides) -> Path:
    data = {
        "searxng_url": "http://127.0.0.1:1",
        "max_results": 3,
        "timeout_s": 4,
        "categories": "general",
        "safe_search": 1,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Configuration precedence
# ---------------------------------------------------------------------------

def test_config_file_supplies_defaults(tmp_path, monkeypatch):
    module = _load_module()
    _clear_env(monkeypatch)
    module.configure(str(_config(tmp_path / "web.json")))

    assert module.base_url() == "http://127.0.0.1:1"
    assert module.default_max_results() == 3
    assert module.timeout_s() == 4
    assert module.default_categories() == "general"
    assert module.default_safe_search() == 1


def test_environment_overrides_config_file(tmp_path, monkeypatch):
    module = _load_module()
    _clear_env(monkeypatch)
    module.configure(str(_config(tmp_path / "web.json")))
    monkeypatch.setenv("WEB_SEARXNG_URL", "http://env.test:9")
    monkeypatch.setenv("WEB_MAX_RESULTS", "7")

    assert module.base_url() == "http://env.test:9"
    assert module.default_max_results() == 7


def test_legacy_environment_names_are_accepted(tmp_path, monkeypatch):
    module = _load_module()
    _clear_env(monkeypatch)
    module.configure(str(_config(tmp_path / "web.json", searxng_url="")))
    monkeypatch.setenv("HARNESS_WEB_SEARXNG_URL", "http://legacy.test:9")
    monkeypatch.setenv("HARNESS_WEB_MAX_RESULTS", "2")

    assert module.base_url() == "http://legacy.test:9"
    assert module.default_max_results() == 2


def test_toml_config_file_is_accepted(tmp_path, monkeypatch):
    module = _load_module()
    _clear_env(monkeypatch)
    path = tmp_path / "web.toml"
    path.write_text(
        'searxng_url = "http://toml.test:9"\nmax_results = 4\n',
        encoding="utf-8",
    )
    module.configure(str(path))

    assert module.base_url() == "http://toml.test:9"
    assert module.default_max_results() == 4


def test_missing_config_file_is_a_clean_error(tmp_path):
    module = _load_module()
    try:
        module.configure(str(tmp_path / "nope.json"))
    except module.WebConfigError as exc:
        assert "cannot read web config file" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("a missing config file must fail")


def test_unconfigured_url_is_a_clean_error(tmp_path, monkeypatch):
    module = _load_module()
    _clear_env(monkeypatch)
    module.configure(str(_config(tmp_path / "web.json", searxng_url="")))

    try:
        module.base_url()
    except module.WebConfigError as exc:
        assert "not configured" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("an empty url must fail")


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_tool_surface_is_bounded_and_untrusted():
    module = _load_module()
    schema = module.tool_schema()

    assert schema["name"] == "web_search"
    assert schema["inputSchema"]["required"] == ["query"]
    assert "untrusted" in schema["description"]
    time_range = schema["inputSchema"]["properties"]["time_range"]
    assert time_range["type"] == "string"
    assert time_range["enum"] == ["day", "week", "month", "year"]

    error = module.handle_call("web_fetch", {"url": "http://x.test/"})
    assert error["isError"] is True
    assert "unknown tool" in error["content"][0]["text"]


class _Response:
    def __init__(self, results):
        self._body = json.dumps({"results": results}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _stub_search(module, monkeypatch, results):
    requests = []
    monkeypatch.setattr(module, "base_url", lambda: "http://searx.test")
    monkeypatch.setattr(module, "default_max_results", lambda: 5)
    monkeypatch.setattr(module, "default_categories", lambda: "general")
    monkeypatch.setattr(module, "default_safe_search", lambda: 1)
    monkeypatch.setattr(module, "timeout_s", lambda: 1)

    def urlopen(request, timeout):
        requests.append(request)
        return _Response(results)

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return requests


def test_time_range_is_forwarded_and_returned_for_audit(monkeypatch):
    module = _load_module()
    requests = _stub_search(module, monkeypatch, [])

    response = module.handle_call("web_search", {
        "query": "current news",
        "time_range": "week",
    })
    payload = json.loads(response["content"][0]["text"])
    params = parse_qs(urlsplit(requests[0].full_url).query)
    assert params["q"] == ["current news"]
    assert params["time_range"] == ["week"]
    assert payload["time_range"] == "week"

    historical = module.search("moon landing 1969", time_range="")
    params = parse_qs(urlsplit(requests[1].full_url).query)
    assert "time_range" not in params
    assert historical["time_range"] == ""
    assert historical["query"] == "moon landing 1969"


def test_invalid_time_range_is_rejected_without_a_request(monkeypatch):
    module = _load_module()
    requests = _stub_search(module, monkeypatch, [])

    with pytest.raises(ValueError, match="time_range"):
        module.search("news", time_range="hour")

    response = module.handle_call("web_search", {
        "query": "news",
        "time_range": "2026",
    })
    assert response["isError"] is True
    assert "time_range" in response["content"][0]["text"]
    assert requests == []


def test_published_metadata_is_normalized_bounded_and_not_inferred(monkeypatch):
    module = _load_module()
    requests = _stub_search(module, monkeypatch, [
        {
            "title": "ISO date",
            "url": "https://example.test/iso",
            "content": "Current news",
            "publishedDate": "  2026-11-18T10:00:00Z\n",
        },
        {
            "title": "Published fallback",
            "url": "https://example.test/fallback",
            "content": "Other news",
            "published": " <b>2026-11-17</b>  ",
        },
        {
            "title": "No metadata",
            "url": "https://example.test/historical",
            "content": "Published in 1969",
        },
        {
            "title": "Long metadata",
            "url": "https://example.test/long",
            "content": "Current news",
            "publishedDate": "2026-11-18" * 20,
        },
    ])

    payload = module.search("news", max_results=4)
    results = payload["results"]
    assert results[0]["published_date"] == "2026-11-18T10:00:00Z"
    assert results[1]["published_date"] == "2026-11-17"
    assert "published_date" not in results[2]
    assert len(results[3]["published_date"]) == module.PUBLISHED_DATE_CHARS
    assert results[3]["published_date"].endswith("…")
    assert len(requests) == 1
