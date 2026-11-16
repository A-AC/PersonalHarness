"""Phase 8.0 deployment safety: pre-migration backups, version visibility,
staging surface, and portability guards. Deterministic; no network and no
Docker required."""

from __future__ import annotations

import gc
import sqlite3
import zipfile
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from harness import __version__, git_sha
from harness.db import Database, SCHEMA_VERSION
from harness.server import Server

REPO = Path(__file__).resolve().parents[1]


def _emulate_older_db(path: Path, version: int = 2) -> None:
    """Open a fresh database and rewind it to ``version``: drop what newer
    migrations added and set ``schema_meta`` to the lower value (the same
    pattern as the v2 migration test)."""
    db = Database(path)
    db.execute("DROP TABLE module_tools")
    db.execute("DROP TABLE module_bindings")
    db.execute("DROP TABLE modules")
    db.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    db.meta_set("sentinel", "kept")
    db.close()


def test_pre_migration_backup_written_on_upgrade(tmp_path):
    path = tmp_path / "harness.db"
    _emulate_older_db(path, version=2)

    reopened = Database(path)
    try:
        value = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(value) == SCHEMA_VERSION
        assert reopened.meta_get("sentinel") == "kept"
        pattern = f"pre-migrate-v2-to-v{SCHEMA_VERSION}-*.zip"
        backups = sorted((tmp_path / "backups").glob(pattern))
        assert len(backups) == 1, backups
        with zipfile.ZipFile(backups[0]) as zf:
            assert "harness.db" in zf.namelist()
            with zf.open("harness.db") as src, \
                    open(tmp_path / "snapshot.db", "wb") as out:
                out.write(src.read())
        # The snapshot is pre-migration: the old version and data survive.
        conn = sqlite3.connect(str(tmp_path / "snapshot.db"))
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        conn.close()
        assert int(row[0]) == 2
    finally:
        reopened.close()


def test_fresh_and_current_databases_make_no_backup(tmp_path):
    path = tmp_path / "harness.db"
    Database(path).close()
    assert not (tmp_path / "backups").exists()

    # Already at SCHEMA_VERSION: reopening migrates nothing, backs up
    # nothing.
    db = Database(path)
    db.close()
    backups = tmp_path / "backups"
    assert not backups.is_dir() or list(backups.glob("*.zip")) == []


def test_failed_backup_aborts_migration(tmp_path, monkeypatch):
    path = tmp_path / "harness.db"
    _emulate_older_db(path, version=2)

    def broken_backup(self, target: Path) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Database, "backup_to", broken_backup)

    with pytest.raises(RuntimeError, match="pre-migration backup"):
        Database(path)
    gc.collect()  # finalise the abandoned connection before re-reading

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    conn.close()
    assert int(row[0]) == 2  # not bumped

    backups = tmp_path / "backups"
    assert not backups.is_dir() or list(
        backups.glob("*.zip")
    ) == []  # no partial zip left behind


def test_system_payload_reports_build_info(make_app):
    app = make_app.build(default="ok")
    payload = Server(app)._system_payload()
    assert payload["version"] == __version__
    assert payload["git_sha"] == git_sha()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["data_dir"] == str(Path(app.data_dir).resolve())
    # Every existing key survives the addition.
    for key in ("counts", "working_task", "scheduler", "domains",
                "module_health", "modules", "model", "now",
                "system_timezone"):
        assert key in payload


def test_health_stays_minimal(make_app):
    app = make_app.build(default="ok")
    http = TestClient(Server(app).routes())
    response = http.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status"}


def test_shell_scripts_and_dockerfile_use_lf():
    shell_scripts = sorted(
        path for path in REPO.glob("**/*.sh") if ".git" not in path.parts
    )
    assert shell_scripts, "expected at least one shell script in the repo"
    for path in shell_scripts:
        assert b"\r" not in path.read_bytes(), path
    dockerfile = REPO / "Dockerfile"
    assert b"\r" not in dockerfile.read_bytes()


def test_stage_compose_is_a_separate_surface():
    text = (REPO / "docker-compose.stage.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert "harness-stage" in doc["services"]
    service = doc["services"]["harness-stage"]
    assert service["image"] == "personal-harness:stage"
    volumes = [str(v) for v in service["volumes"]]
    assert "harness-data:/data" not in volumes  # one writer per data dir
    assert "harness-stage-data" in (doc.get("volumes") or {})
    port = str(service["ports"][0])
    assert port.endswith(":8080")
    default = port.split(":-", 1)[1].rstrip("}")
    assert default != "8080"  # SearXNG owns 8080


def test_dockerfile_exposes_test_and_runtime_stages():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM base AS test" in text
    assert "FROM base AS runtime" in text
    assert "ARG GIT_SHA" in text
    assert "HARNESS_GIT_SHA" in text
    test_stage = text.split("FROM base AS test", 1)[1].split(
        "FROM base AS runtime", 1
    )[0]
    assert "COPY tests" in test_stage
    assert "COPY scripts" in test_stage
    assert "pytest" in test_stage
    # tests/ never leaks into the runtime image.
    runtime_stage = text.split("FROM base AS runtime", 1)[1]
    assert "COPY tests" not in runtime_stage
    assert "USER harness" in runtime_stage
    assert 'CMD ["python", "-m", "harness", "serve"]' in runtime_stage
