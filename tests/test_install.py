"""Phase 12A: Docker-first install, first-run setup, reset, and doctor.

Deterministic and network-free: the only HTTP server used is a loopback stub
for the model endpoint and the port-in-use check.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from harness import util
from harness.app import App, build_app
from harness.config import load_config, save_config
from harness.doctor import run_doctor
from harness.model import ScriptedModel
from harness.server import Server

REPO = Path(__file__).resolve().parents[1]


class _StubHandler(BaseHTTPRequestHandler):
    """Loopback model endpoint: advertises one model and 404s anything else."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/v1/models":
            raw = json.dumps({"data": [{"id": "stub-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def stub_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _write_config(data_dir: Path, **values) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump(values), encoding="utf-8"
    )


def _unconfigured_app(data_dir: Path):
    """Build an app without the test helper's configured shortcut."""
    config = load_config(data_dir)
    assert config.get("setup.complete") is False
    app = App(data_dir, config=config, model=ScriptedModel(default="ok"))
    app.startup()
    return app


# ---------------------------------------------------------------------------
# Config: first run vs an existing install
# ---------------------------------------------------------------------------

def test_fresh_config_requires_setup_existing_config_does_not(tmp_path):
    fresh = tmp_path / "fresh"
    config = load_config(fresh)
    assert (fresh / "config.yaml").is_file()
    assert config.get("setup.complete") is False
    assert config.get("setup.completed_at") == ""

    legacy = tmp_path / "legacy"
    _write_config(legacy, server={"token": "legacy-token"})
    migrated = load_config(legacy)
    # A config that predates the wizard is already configured.
    assert migrated.get("setup.complete") is True
    assert migrated.get("server.token") == "legacy-token"


def test_load_config_can_be_read_only(tmp_path):
    missing = tmp_path / "not-created"
    config = load_config(missing, create=False)
    assert config.get("setup.complete") is False
    assert not missing.exists()


def test_build_app_treats_tests_as_configured(tmp_path):
    app = build_app(tmp_path / "data")
    try:
        assert app.config.get("setup.complete") is True
    finally:
        app.shutdown()


# ---------------------------------------------------------------------------
# harness init
# ---------------------------------------------------------------------------

def _init_env(monkeypatch, stub_endpoint: str) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "init-token")
    monkeypatch.setenv("HARNESS_MODEL_BASE_URL", stub_endpoint)
    monkeypatch.setenv("HARNESS_MODEL_NAME", "stub-model")
    monkeypatch.setenv("HARNESS_TIMEZONE", "UTC")


def test_init_non_interactive_bootstraps_from_environment(
    tmp_path, monkeypatch, stub_endpoint, capsys
):
    from harness.__main__ import main

    data_dir = tmp_path / "data"
    _init_env(monkeypatch, stub_endpoint)

    # No probe by default in the non-interactive path: bootstrap must not
    # depend on the endpoint being up.
    def explode(*_args, **_kwargs):
        raise AssertionError("init --non-interactive must not probe by default")

    monkeypatch.setattr("harness.setup.probe_model_endpoint", explode)

    main(["init", "--data-dir", str(data_dir), "--non-interactive"])
    out = capsys.readouterr().out
    assert "configured" in out.lower()

    config = load_config(data_dir)
    assert config.get("setup.complete") is True
    assert config.get("setup.completed_at")
    assert config.get("server.token") == "init-token"
    assert config.get("model.base_url") == stub_endpoint
    assert config.get("model.name") == "stub-model"
    assert config.get("timezone") == "UTC"
    on_disk = yaml.safe_load((data_dir / "config.yaml").read_text("utf-8"))
    assert on_disk["setup"]["complete"] is True

    # Refuses to clobber without --force; --force rewrites.
    with pytest.raises(SystemExit) as excinfo:
        main(["init", "--data-dir", str(data_dir), "--non-interactive"])
    assert "already exists" in str(excinfo.value)
    monkeypatch.setenv("HARNESS_TOKEN", "second-token")
    main(["init", "--data-dir", str(data_dir), "--non-interactive", "--force"])
    assert load_config(data_dir).get("server.token") == "second-token"


def test_init_non_interactive_requires_the_environment(tmp_path, monkeypatch):
    from harness.__main__ import main

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("HARNESS_MODEL_BASE_URL", raising=False)
    data_dir = tmp_path / "data"

    with pytest.raises(SystemExit) as excinfo:
        main(["init", "--data-dir", str(data_dir), "--non-interactive"])
    assert "HARNESS_TOKEN" in str(excinfo.value)
    assert not (data_dir / "config.yaml").exists()


# ---------------------------------------------------------------------------
# First-run web setup
# ---------------------------------------------------------------------------

def test_first_run_serves_the_wizard_and_configure_closes_it(
    tmp_path, monkeypatch, stub_endpoint
):
    data_dir = tmp_path / "data"
    app = _unconfigured_app(data_dir)
    http = TestClient(Server(app).routes())

    page = http.get("/")
    assert page.status_code == 200
    assert "First-run setup" in page.text
    assert 'name="token"' in page.text

    state = http.get("/api/setup")
    assert state.status_code == 200
    payload = state.json()
    assert payload["required"] is True
    assert payload["model_base_url"] == app.config.get("model.base_url")
    assert {item["id"] for item in payload["templates"]} >= {"web", "calendar"}
    assert "init-token" not in state.text
    assert str(app.config.get("server.token")) not in state.text

    probe = http.post("/api/setup/probe", json={"base_url": stub_endpoint})
    assert probe.status_code == 200
    assert probe.json()["reachable"] is True
    assert probe.json()["models"] == ["stub-model"]

    installed: list[dict] = []

    def fake_install(name, values, *, confirm=False, **_kwargs):
        installed.append({"id": name, "values": values, "confirm": confirm})
        return None

    monkeypatch.setattr(app.modules.loader, "install_template", fake_install)

    configured = http.post("/api/setup", json={
        "token": "wizard-token",
        "model_base_url": stub_endpoint,
        "model_name": "stub-model",
        "timezone": "UTC",
        "modules": [
            {"id": "web", "values": {"searxng_url": "http://127.0.0.1:8088"}},
        ],
    })
    assert configured.status_code == 200, configured.text
    assert app.config.get("setup.complete") is True
    assert app.config.get("server.token") == "wizard-token"
    assert load_config(data_dir).get("server.token") == "wizard-token"
    assert installed == [{
        "id": "web",
        "values": {"searxng_url": "http://127.0.0.1:8088"},
        "confirm": True,
    }]

    # The response authenticated the browser, and the wizard is gone.
    assert http.get("/").status_code == 200
    assert 'data-tab="chat"' in http.get("/").text
    assert http.get("/api/setup").status_code == 409
    assert http.post("/api/setup/probe", json={
        "base_url": stub_endpoint,
    }).status_code == 409
    app.shutdown()


def test_setup_rejects_invalid_values(tmp_path):
    app = _unconfigured_app(tmp_path / "data")
    http = TestClient(Server(app).routes())

    bad_url = http.post("/api/setup", json={
        "token": "good-token", "model_base_url": "ftp://model", "model_name": "m",
    })
    assert bad_url.status_code == 400
    bad_zone = http.post("/api/setup", json={
        "token": "good-token", "model_base_url": "http://model.test",
        "model_name": "m", "timezone": "Nowhere/Nothing",
    })
    assert bad_zone.status_code == 400
    no_token = http.post("/api/setup", json={
        "token": "", "model_base_url": "http://model.test", "model_name": "m",
    })
    assert no_token.status_code == 400
    assert app.config.get("setup.complete") is False
    app.shutdown()


def test_setup_probe_reports_an_unreachable_endpoint_cleanly(tmp_path):
    app = _unconfigured_app(tmp_path / "data")
    http = TestClient(Server(app).routes())
    port = _free_port()
    response = http.post("/api/setup/probe", json={
        "base_url": f"http://127.0.0.1:{port}",
    })
    assert response.status_code == 200
    assert response.json()["reachable"] is False
    assert response.json()["error"]
    app.shutdown()


def test_setup_state_warns_about_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKEN", "env-token")
    app = _unconfigured_app(tmp_path / "data")
    http = TestClient(Server(app).routes())
    payload = http.get("/api/setup").json()
    assert "HARNESS_TOKEN" in payload["env_overrides"]
    app.shutdown()


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset_backs_up_then_wipes_and_requires_confirmation(
    tmp_path, client
):
    data_dir = tmp_path / "data"
    app = build_app(data_dir)
    app.startup()
    save_config(app.config)  # make the on-disk config match a configured app
    app.runtime.submit_user_message("hello", source="web")
    (app.artifacts_dir / "memories").mkdir(parents=True, exist_ok=True)
    (app.artifacts_dir / "memories" / "note.md").write_text("x", "utf-8")
    (app.modules.root / "mcp").mkdir(parents=True, exist_ok=True)
    (app.modules.root / "mcp" / "web.json").write_text("{}", "utf-8")

    http = client(app)
    assert http.post("/api/reset", json={}).status_code == 400
    response = http.post("/api/reset", json={"confirm": True})
    assert response.status_code == 200, response.text
    outcome = response.json()
    backup = Path(outcome["backup"])
    assert backup.is_file()
    assert backup.parent == data_dir / "backups"
    assert not (data_dir / "harness.db").exists()
    assert not (data_dir / "config.yaml").exists()
    assert list((data_dir / "artifacts").rglob("*")) == []
    assert list((data_dir / "modules").rglob("*")) == []

    # A fresh start is a fresh install again.
    assert load_config(data_dir).get("setup.complete") is False
    app.shutdown()


def test_reset_keep_config_keeps_configuration(tmp_path):
    data_dir = tmp_path / "data"
    app = build_app(data_dir)
    app.startup()
    save_config(app.config)
    outcome = app.reset(keep_config=True)
    assert outcome["keep_config"] is True
    assert outcome["reset"] is True
    assert (data_dir / "config.yaml").is_file()
    assert load_config(data_dir).get("setup.complete") is True
    assert not (data_dir / "harness.db").exists()
    app.shutdown()


def test_reset_cli_requires_yes(tmp_path):
    from harness.__main__ import main

    with pytest.raises(SystemExit) as excinfo:
        main(["reset", "--data-dir", str(tmp_path / "data")])
    assert "--yes" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def _configured_data_dir(tmp_path, stub_endpoint: str, port: int) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_config(
        data_dir,
        server={"host": "127.0.0.1", "port": port},
        model={"base_url": stub_endpoint, "name": "stub-model"},
    )
    return data_dir


def test_doctor_reports_every_check(tmp_path, stub_endpoint):
    data_dir = _configured_data_dir(tmp_path, stub_endpoint, _free_port())
    report = run_doctor(data_dir)
    names = {check["name"] for check in report["checks"]}
    assert names == {
        "python", "data_writable", "endpoint", "port",
        "writer_lock", "modules", "disk",
    }
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["python"]["status"] == "ok"
    assert by_name["data_writable"]["status"] == "ok"
    assert by_name["endpoint"]["status"] == "ok"
    assert by_name["port"]["status"] == "ok"
    assert by_name["writer_lock"]["status"] == "ok"
    assert by_name["modules"]["status"] == "ok"
    assert by_name["disk"]["status"] == "ok"
    assert report["ok"] is True
    assert report["version"]


def test_doctor_fails_on_an_unreachable_endpoint(tmp_path):
    data_dir = _configured_data_dir(
        tmp_path, f"http://127.0.0.1:{_free_port()}", _free_port()
    )
    report = run_doctor(data_dir)
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["endpoint"]["status"] == "fail"
    assert report["ok"] is False


def test_doctor_warns_on_foreign_port_and_stale_lock(
    tmp_path, stub_endpoint
):
    # The stub answers /v1/models but 404s /health, so it looks like another
    # service on the harness port.
    port = int(stub_endpoint.rsplit(":", 1)[1])
    data_dir = _configured_data_dir(tmp_path, stub_endpoint, port)
    (data_dir / "process.lock").write_text(json.dumps({
        "pid": 999999,
        "ts": util.iso(util.now_utc().replace(year=2000)),
    }), "utf-8")
    report = run_doctor(data_dir)
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["port"]["status"] == "warn"
    assert by_name["writer_lock"]["status"] == "warn"
    assert report["ok"] is True  # warnings are not failures


def test_doctor_reports_module_errors(tmp_path, stub_endpoint):
    data_dir = _configured_data_dir(tmp_path, stub_endpoint, _free_port())
    app = App(data_dir, config=load_config(data_dir), model=ScriptedModel())
    app.db.execute(
        "INSERT INTO modules(id, kind, name, status, scope, manifest, origin,"
        " provenance, created_by, created_at, updated_at, last_error)"
        " VALUES ('mcp:broken','mcp','Broken','error','global','{}','import',"
        " '{}','test',?,?,?)",
        (util.iso(), util.iso(), "server did not start"),
    )
    app.db.close()
    report = run_doctor(data_dir)
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["modules"]["status"] == "warn"
    assert "mcp:broken" in by_name["modules"]["detail"]


def test_doctor_does_not_create_a_config(tmp_path, monkeypatch):
    # Keep the default LAN endpoint out of the deterministic suite.
    monkeypatch.setattr(
        "harness.doctor._check_endpoint",
        lambda _config: {"name": "endpoint", "status": "fail",
                         "detail": "stubbed"},
    )
    data_dir = tmp_path / "data"
    report = run_doctor(data_dir)
    assert data_dir.is_dir()  # the writability check may create it
    assert not (data_dir / "config.yaml").exists()
    assert report["ok"] is False


def test_doctor_cli_json_reports_and_exits(tmp_path, stub_endpoint, capsys):
    from harness.__main__ import main

    data_dir = _configured_data_dir(tmp_path, stub_endpoint, _free_port())
    with pytest.raises(SystemExit) as excinfo:
        main(["doctor", "--data-dir", str(data_dir), "--json"])
    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]}


# ---------------------------------------------------------------------------
# Docker-first install surface
# ---------------------------------------------------------------------------

def test_docker_entrypoint_bootstraps_from_environment():
    entrypoint = REPO / "scripts" / "docker-entrypoint.sh"
    assert entrypoint.is_file()
    text = entrypoint.read_text(encoding="utf-8")
    assert "harness init --non-interactive" in text
    assert "config.yaml" in text
    assert "exec python -m harness" in text
    assert "\r" not in text

    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    assert "docker-entrypoint.sh" in dockerfile.split(
        "FROM base AS runtime", 1
    )[1]


def test_reset_refuses_a_modules_dir_outside_the_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", "utf-8")
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({"modules": {"dir": str(outside)}}), encoding="utf-8"
    )

    app = App(data_dir, config=load_config(data_dir), model=ScriptedModel())
    app.startup()
    outcome = app.reset(keep_config=True)
    assert (outside / "keep.txt").is_file()
    assert any("outside the data directory" in w for w in outcome["warnings"])
    app.shutdown()


def test_fresh_install_smoke_from_init_to_reset(
    tmp_path, monkeypatch, stub_endpoint, capsys
):
    """The 12A acceptance smoke: fresh dir -> init -> serve -> one message ->
    reset. The server is driven in-process; the reset endpoint does not kill
    the test process because no writer lock is held."""
    from harness.__main__ import main

    data_dir = tmp_path / "data"
    _init_env(monkeypatch, stub_endpoint)
    main(["init", "--data-dir", str(data_dir), "--non-interactive"])
    capsys.readouterr()

    config = load_config(data_dir)
    token = config.get("server.token")
    assert token == "init-token"
    assert config.get("setup.complete") is True
    app = App(data_dir, config=config, model=ScriptedModel(default="ok"))
    app.startup()
    http = TestClient(
        Server(app).routes(),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert http.get("/health").json() == {"status": "ok"}
    assert http.get("/").status_code == 200
    turn = http.post("/api/message", json={"text": "hello"})
    assert turn.status_code == 200, turn.text
    assert turn.json()["status"] == "completed"

    reset = http.post("/api/reset", json={"confirm": True})
    assert reset.status_code == 200, reset.text
    assert Path(reset.json()["backup"]).is_file()
    assert not (data_dir / "harness.db").exists()
    assert not (data_dir / "config.yaml").exists()

    # A restart is a genuine fresh install: the wizard is required again.
    assert load_config(data_dir).get("setup.complete") is False
    app.shutdown()
