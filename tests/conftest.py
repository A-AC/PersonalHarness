"""Shared fixtures: isolated data dirs, scripted models, app builders.

Tests never touch the repository or any production data directory: every
test builds its App inside pytest's tmp_path. The scripted model keeps the
suite deterministic and network-free; live-endpoint tests live separately
under the ``live`` marker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.app import App, build_app  # noqa: E402
from harness.model import ScriptedModel  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


class AppFactory:
    """Builds apps on one data dir; supports in-place restarts."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.apps: list[App] = []

    def build(
        self,
        scripts: list | None = None,
        default: str = "",
        model: ScriptedModel | None = None,
        serve: bool = False,
    ) -> App:
        model = model or ScriptedModel(scripts=list(scripts or []), default=default)
        app = build_app(self.data_dir, model=model)
        app.startup(serve=serve)
        self.apps.append(app)
        return app

    def restart(
        self,
        scripts: list | None = None,
        default: str = "",
        model: ScriptedModel | None = None,
    ) -> App:
        """Close every open app on this dir and start a fresh one on the
        same persistent state (the restart-under-test)."""
        self.close_all()
        return self.build(scripts=scripts, default=default, model=model)

    def close_all(self) -> None:
        for app in self.apps:
            try:
                app.shutdown()
            except Exception:
                pass
        self.apps.clear()

    def newest(self) -> App:
        return self.apps[-1]


@pytest.fixture()
def make_app(data_dir: Path):
    factory = AppFactory(data_dir)
    yield factory
    factory.close_all()


@pytest.fixture()
def client():
    """Test HTTP client factory against a live in-process server. Requests
    carry the app's token by default."""

    def _make(app: App):
        from starlette.testclient import TestClient

        from harness.server import Server

        token = str(app.config.get("server.token", ""))
        return TestClient(
            Server(app).routes(),
            headers={"Authorization": f"Bearer {token}"},
        )

    return _make
