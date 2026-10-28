"""HTTP concurrency: a slow model call must not block /health.

The runtime is deliberately synchronous. The HTTP layer moves it to a worker
thread so the event loop can keep answering health checks (and so Docker's
healthcheck does not kill a container during a long tool/model call).
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx

from harness.model import ScriptedModel
from harness.server import Server


class BlockingModel(ScriptedModel):
    def __init__(self, started: threading.Event, release: threading.Event):
        super().__init__(scripts=[{"content": "slow reply"}], default="")
        self.started = started
        self.release = release

    def chat(self, messages, tools, *, max_tokens=None):
        self.started.set()
        self.release.wait(10)
        return super().chat(messages, tools, max_tokens=max_tokens)


def test_slow_message_does_not_block_health(make_app):
    started = threading.Event()
    release = threading.Event()
    app = make_app.build(model=BlockingModel(started, release))
    asgi = Server(app).routes()
    token = str(app.config.get("server.token", ""))
    headers = {"Authorization": f"Bearer {token}"}
    # Safety valve: if the event loop is blocked, this releases the model so
    # the test fails on elapsed time instead of hanging forever.
    watchdog = threading.Timer(5.0, release.set)
    watchdog.start()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            began = time.monotonic()

            async def delayed_health():
                # Give the message handler time to reach the model call. If
                # it blocks the loop, this sleep and the request are delayed.
                await asyncio.sleep(0.2)
                response = await client.get("/health")
                return response, time.monotonic() - began

            health_task = asyncio.create_task(delayed_health())
            message_task = asyncio.create_task(client.post(
                "/api/message", json={"text": "slow work"}, headers=headers
            ))
            try:
                health, elapsed = await health_task
                release.set()
                response = await message_task
            finally:
                release.set()
                if not message_task.done():
                    await message_task
            assert started.is_set()
            assert health.status_code == 200
            assert elapsed < 2.0
            assert response.status_code == 200
            assert response.json()["status"] == "completed"

    try:
        asyncio.run(scenario())
    finally:
        watchdog.cancel()
        release.set()
