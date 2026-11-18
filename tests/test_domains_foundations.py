"""Phase 10 foundations: schema v9, stemming, and domain evidence fields."""

from __future__ import annotations

import copy

import pytest
import yaml

from harness import util
from harness.config import (
    DEFAULTS,
    Config,
    ConfigError,
    load_config,
    validate_group_update,
)
from harness.db import SCHEMA_VERSION, Database


# ---------------------------------------------------------------------------
# Schema v9 migration
# ---------------------------------------------------------------------------

def test_schema_migration_adds_columns_tables_and_keeps_rows(tmp_path):
    path = tmp_path / "harness.db"
    db = Database(path)
    try:
        db.execute(
            "INSERT INTO domains(id, name, description, status, created_at,"
            " updated_at, meta) VALUES ('dom_old', 'Old', 'kept', 'active',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')"
        )
        db.execute(
            "INSERT INTO tasks(id, domain_id, objective, status, origin,"
            " created_at, updated_at, last_activity_at)"
            " VALUES ('task_old', NULL, 'keep me', 'active', 'user',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO memories(id, scope, kind, content, status, origin,"
            " provenance, created_at, updated_at)"
            " VALUES ('mem_old', 'global', 'fact', 'keep me', 'active',"
            " 'user', '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        # Make this a genuine v8-shaped file: remove every v9 column and both
        # v9 tables before stamping the old version. The ALTER guards must then
        # actually add something on reopen (not be no-ops).
        for table in (
            "messages", "tasks", "memories", "domains", "schedules",
            "approvals", "inbox_events",
        ):
            db.execute(f"ALTER TABLE {table} DROP COLUMN user_id")
        for column in ("keywords", "examples", "instructions"):
            db.execute(f"ALTER TABLE domains DROP COLUMN {column}")
        db.execute("DROP TABLE routing_feedback")
        db.execute("DROP TABLE domain_suggestions")
        db.execute("UPDATE schema_meta SET value='8' WHERE key='schema_version'")
    finally:
        db.close()

    reopened = Database(path)
    try:
        version = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 10

        for table in (
            "messages", "tasks", "memories", "domains", "schedules",
            "approvals", "inbox_events",
        ):
            columns = {
                row["name"] for row in reopened.query(
                    f"PRAGMA table_info({table})"
                )
            }
            assert "user_id" in columns, table

        domain_columns = {
            row["name"] for row in reopened.query("PRAGMA table_info(domains)")
        }
        assert {
            "keywords", "examples", "instructions", "summary",
            "summary_updated_at",
        } <= domain_columns

        tables = {
            row["name"] for row in reopened.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"routing_feedback", "domain_suggestions"} <= tables

        domain = reopened.query_one("SELECT * FROM domains WHERE id='dom_old'")
        assert domain is not None
        assert domain["name"] == "Old"
        assert domain["keywords"] == "[]"
        assert domain["examples"] == "[]"
        assert domain["instructions"] == ""
        assert domain["user_id"] == "owner"

        task = reopened.query_one("SELECT * FROM tasks WHERE id='task_old'")
        assert task["objective"] == "keep me"
        assert task["user_id"] == "owner"
        memory = reopened.query_one("SELECT * FROM memories WHERE id='mem_old'")
        assert memory["content"] == "keep me"
        assert memory["user_id"] == "owner"

        assert list((tmp_path / "backups").glob(
            f"pre-migrate-v8-to-v{SCHEMA_VERSION}-*.zip"
        ))
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Deterministic stemming
# ---------------------------------------------------------------------------

def test_v9_to_v10_adds_summary_and_embedding_cache(tmp_path):
    path = tmp_path / "harness.db"
    db = Database(path)
    try:
        db.execute("ALTER TABLE domains DROP COLUMN summary_updated_at")
        db.execute("ALTER TABLE domains DROP COLUMN summary")
        db.execute("DROP TABLE embedding_cache")
        db.execute("UPDATE schema_meta SET value='9' WHERE key='schema_version'")
    finally:
        db.close()
    reopened = Database(path)
    try:
        version = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 10
        columns = {
            row["name"] for row in reopened.query("PRAGMA table_info(domains)")
        }
        assert {"summary", "summary_updated_at"} <= columns
        tables = {
            row["name"] for row in reopened.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "embedding_cache" in tables
    finally:
        reopened.close()


def test_domain_summary_is_bounded_and_editable(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "dom_sum", name="Summary", summary="initial summary",
        created_by="test",
    )
    assert domain["summary"] == "initial summary"
    assert domain["summary_updated_at"]
    updated = app.domains.update("dom_sum", summary="later summary")
    assert updated["summary"] == "later summary"
    long_text = "x" * 5000
    clipped = app.domains.update("dom_sum", summary=long_text)
    assert len(clipped["summary"]) <= int(
        app.config.get("routing.card_summary_chars", 800)
    )


@pytest.mark.parametrize("word,expected", [
    ("nutritional", "nutrition"),
    ("calories", "calorie"),
    ("workouts", "workout"),
    ("running", "run"),
    ("planned", "plan"),
    ("stories", "story"),
    ("fitness", "fitness"),
    ("sleep", "sleep"),
    ("boxes", "box"),
    ("quickly", "quick"),
    ("this", "this"),
])
def test_stem_fixed_table(word, expected):
    assert util.stem(word) == expected


def test_stem_is_deterministic_and_only_letters():
    assert util.stem("Nutritional") == "nutrition"
    assert util.stem("  Running ") == "run"
    assert util.stem("don't") == "don't"
    assert util.stem("") == ""


def test_stemmed_keywords_dedupes_after_stemming():
    assert util.stemmed_keywords(
        "Running workouts and running plans"
    ) == ["run", "workout", "plan"]


# ---------------------------------------------------------------------------
# Domain evidence fields
# ---------------------------------------------------------------------------

def test_domain_evidence_persists_through_create_and_update(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "health",
        name="Health",
        description="nutrition and training",
        created_by="test",
        keywords=["nutrition", "calories"],
        examples=["what should I eat"],
        instructions="Prefer whole foods first.",
    )
    assert domain["keywords"] == ["nutrition", "calories"]
    assert domain["examples"] == ["what should I eat"]
    assert domain["instructions"] == "Prefer whole foods first."

    fetched = app.domains.get("health")
    assert fetched["keywords"] == ["nutrition", "calories"]
    assert app.domains.by_name("Health")["examples"] == ["what should I eat"]
    listed = {item["id"]: item for item in app.domains.list_active()}
    assert listed["health"]["keywords"] == ["nutrition", "calories"]
    assert listed["health"]["instructions"] == "Prefer whole foods first."

    updated = app.domains.update(
        "health",
        keywords=["protein"],
        examples=[],
        instructions="Be brief.",
    )
    assert updated["keywords"] == ["protein"]
    assert updated["examples"] == []
    assert updated["instructions"] == "Be brief."
    assert updated["description"] == "nutrition and training"


def test_domain_instructions_are_clipped(make_app):
    app = make_app.build(default="ok")
    long_text = "rule " * 600
    domain = app.domains.create(
        "health", name="Health", created_by="test",
        instructions=long_text,
    )
    limit = app.config.get("domains.instructions_max_chars")
    assert len(domain["instructions"]) <= limit
    assert domain["instructions"].endswith("[trimmed]")


@pytest.mark.parametrize("kwargs", [
    {"keywords": [f"k{index}" for index in range(25)]},
    {"keywords": ["x" * 49]},
    {"keywords": [""]},
    {"examples": [f"e{index}" for index in range(25)]},
    {"examples": ["x" * 161]},
    {"examples": [""]},
    {"instructions": "bad\x00value"},
])
def test_domain_evidence_bounds_raise_value_error(make_app, kwargs):
    app = make_app.build(default="ok")
    with pytest.raises(ValueError):
        app.domains.create("bad", name="Bad", created_by="test", **kwargs)


def test_fixture_domains_carry_keywords_examples_and_instructions(make_app):
    from harness.evaluation.fixtures import apply_fixtures

    app = make_app.build(default="ok")
    created = apply_fixtures(app, {"domains": [{
        "name": "Health",
        "description": "nutrition and training",
        "keywords": ["nutrition", "calories"],
        "examples": ["calorie check"],
        "instructions": "Whole foods first.",
    }]})
    domain = app.domains.by_name("Health")
    assert created["domains"]["Health"] == domain["id"]
    assert domain["keywords"] == ["nutrition", "calories"]
    assert domain["examples"] == ["calorie check"]
    assert domain["instructions"] == "Whole foods first."


# ---------------------------------------------------------------------------
# Config additions
# ---------------------------------------------------------------------------

def _config() -> Config:
    return Config(copy.deepcopy(DEFAULTS), None)


def test_phase10_config_defaults_present():
    config = _config()
    assert config.get("domains.weights.keyword") == 1.0
    assert config.get("domains.weights.learned_term") == 0.5
    assert config.get("domains.weights.learned_cap") == 2.0
    assert config.get("domains.stemming") is True
    assert config.get("domains.model_routing") == "off"
    assert config.get("domains.suggestions.min_tasks") == 6
    assert config.get("memory.cross_domain") == "boost"
    assert config.get("memory.cross_domain_max") == 3


def test_semantic_routing_config_defaults_and_validation(tmp_path):
    config = load_config(tmp_path)
    assert config.get("routing.strategy") == "lexical"
    assert config.get("routing.shadow_strategies") == []
    assert config.get("embeddings.enabled") is False
    assert config.get("embeddings.path") == "/v1/embeddings"

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "config.yaml").write_text(
        "routing:\n  strategy: magic\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(bad)
    with pytest.raises(ConfigError):
        load_config(bad, create=False)


def test_config_enum_validation_and_yaml_boolean_compat():
    config = _config()
    merged = validate_group_update(config, "domains", {"model_routing": "shadow"})
    assert merged["model_routing"] == "shadow"
    # PyYAML parses bare ``off`` as False; accept the documented spelling.
    merged = validate_group_update(config, "domains", {"model_routing": False})
    assert merged["model_routing"] == "off"
    with pytest.raises(ConfigError):
        validate_group_update(config, "domains", {"model_routing": "auto"})

    merged = validate_group_update(config, "memory", {"cross_domain": "search"})
    assert merged["cross_domain"] == "search"
    with pytest.raises(ConfigError):
        validate_group_update(config, "memory", {"cross_domain": "on"})


@pytest.mark.parametrize("group,key,value,message", [
    (
        "domains", "model_routing", "auto",
        "configuration key domains.model_routing must be off, shadow, or on",
    ),
    (
        "memory", "cross_domain", "typo",
        "configuration key memory.cross_domain must be off, boost, or search",
    ),
])
def test_config_load_rejects_invalid_enums_in_create_and_read_only_modes(
    tmp_path, group, key, value, message,
):
    data_dir = tmp_path / group
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({group: {key: value}}), encoding="utf-8"
    )

    for create in (True, False):
        with pytest.raises(ConfigError) as exc_info:
            load_config(data_dir, create=create)
        assert str(exc_info.value) == message


# ---------------------------------------------------------------------------
# Phase 10 routing-default migration
# ---------------------------------------------------------------------------

def test_pre_phase10_routing_defaults_migrate_and_persist(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({
            "domains": {
                "routing_threshold": 2.0,
                "ambiguity_margin": 0.75,
            },
        }),
        encoding="utf-8",
    )

    config = load_config(data_dir)

    assert config.get("domains.routing_threshold") == 1.0
    assert config.get("domains.ambiguity_margin") == 1.0
    on_disk = yaml.safe_load(
        (data_dir / "config.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["domains"] == {
        "routing_threshold": 1.0,
        "ambiguity_margin": 1.0,
    }


def test_explicit_phase10_weights_preserve_legacy_routing_values(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original = (
        "domains:\n"
        "  routing_threshold: 2.0\n"
        "  ambiguity_margin: 0.75\n"
        "  weights:\n"
        "    name: 1.5\n"
    )
    config_file = data_dir / "config.yaml"
    config_file.write_text(original, encoding="utf-8")

    config = load_config(data_dir)

    assert config.get("domains.routing_threshold") == 2.0
    assert config.get("domains.ambiguity_margin") == 0.75
    assert config_file.read_text(encoding="utf-8") == original


def test_pre_phase10_config_without_domains_uses_new_defaults(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_file = data_dir / "config.yaml"
    original = "timezone: UTC\n"
    config_file.write_text(original, encoding="utf-8")

    config = load_config(data_dir)

    assert config.get("domains.routing_threshold") == 1.0
    assert config.get("domains.ambiguity_margin") == 1.0
    assert config_file.read_text(encoding="utf-8") == original
    # The normalized defaults remain usable on a subsequent load.
    assert load_config(data_dir).get("domains.routing_threshold") == 1.0


def test_routing_migration_does_not_write_in_read_only_mode(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_file = data_dir / "config.yaml"
    original = (
        "domains:\n"
        "  routing_threshold: 2.0\n"
        "  ambiguity_margin: 0.75\n"
    )
    config_file.write_text(original, encoding="utf-8")

    config = load_config(data_dir, create=False)

    assert config.get("domains.routing_threshold") == 1.0
    assert config.get("domains.ambiguity_margin") == 1.0
    assert config_file.read_text(encoding="utf-8") == original


def test_routing_migration_combines_with_timezone_and_retired_groups(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    web_group = {"enabled": True, "searxng_url": "http://127.0.0.1:9"}
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({
            "timezone": "America/Mexico_City",
            "domains": {
                "routing_threshold": 2.0,
                "ambiguity_margin": 0.75,
            },
            "web": web_group,
        }),
        encoding="utf-8",
    )

    config = load_config(
        data_dir,
        environ={"HARNESS_MODEL_BASE_URL": "http://env-only.test"},
    )

    assert config.get("timezone") == "system"
    assert config.get("domains.routing_threshold") == 1.0
    assert config.get("domains.ambiguity_margin") == 1.0
    assert config.get("model.base_url") == "http://env-only.test"
    on_disk = yaml.safe_load(
        (data_dir / "config.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["timezone"] == "system"
    assert on_disk["domains"]["routing_threshold"] == 1.0
    assert on_disk["domains"]["ambiguity_margin"] == 1.0
    assert on_disk["web"] == web_group
    assert "model" not in on_disk


def test_legacy_settings_yaml_migration_still_works(tmp_path):
    legacy = tmp_path / "settings.yaml"
    legacy.write_text(
        "timezone: UTC\n"
        "models:\n"
        "  interactive:\n"
        "    endpoint: http://legacy.test/v1/chat/completions\n"
        "  background:\n"
        "    max_tokens: 512\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.get("timezone") == "UTC"
    assert config.get("model.base_url") == "http://legacy.test"
    assert config.get("model.max_tokens") == 512
    assert config.get("domains.routing_threshold") == 1.0
    assert config.get("domains.ambiguity_margin") == 1.0
    assert (tmp_path / "config.yaml").is_file()
    assert not legacy.exists()
    assert legacy.with_name("settings.yaml.migrated").is_file()


def test_domain_summary_api_round_trip(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/domains", json={
        "name": "Health",
        "description": "nutrition",
        "keywords": ["calorie"],
        "examples": ["how many calories"],
        "summary": "Diet and fitness notes.",
    })
    assert created.status_code == 200
    domain = created.json()["domain"]
    assert domain["summary"] == "Diet and fitness notes."
    assert domain["summary_updated_at"]
    updated = http.patch(f"/api/domains/{domain['id']}", json={
        "summary": "Updated diet notes.",
    })
    assert updated.status_code == 200
    assert updated.json()["domain"]["summary"] == "Updated diet notes."
