"""Container smoke check: build the image, start the stack, verify health,
auth, and the API, then tear it down. Needs Docker and a local Python.

    HARNESS_TOKEN=check-token HARNESS_PORT=18080 python scripts/container_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("HARNESS_TOKEN", "check-token")
PORT = os.environ.get("HARNESS_PORT", "18080")
BASE = f"http://127.0.0.1:{PORT}"


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True,
        encoding="utf-8", errors="replace",
    )


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(f"container check failed at: {name}")


def http(method: str, path: str, token: str | None = None):
    request = urllib.request.Request(BASE + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def main() -> None:
    env = dict(os.environ, HARNESS_TOKEN=TOKEN, HARNESS_PORT=PORT)
    for command in (["docker", "compose", "down", "-v"], ["docker", "compose", "build"]):
        result = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace",
            env=env,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"'{' '.join(command)}' failed:\n{result.stdout}\n{result.stderr}"
            )
    subprocess.run(["docker", "compose", "up", "-d"], capture_output=True,
                   encoding="utf-8", errors="replace", text=False, env=env)
    try:
        healthy = False
        for _ in range(30):
            try:
                status, body = http("GET", "/health")
                if status == 200:
                    healthy = True
                    break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(2)
        check("health endpoint", healthy)
        status, _ = http("GET", "/api/state")
        check("unauthenticated API rejected", status == 401)
        status, body = http("GET", "/api/state", token=TOKEN)
        check("authenticated API accepted", status == 200)
        state = json.loads(body)
        check("state shape", "messages" in state and "schedules" in state)
        status, body = http("GET", "/api/endpoint-probe", token=TOKEN)
        probe = json.loads(body)
        print(f"[info] model endpoint probe: reachable={probe.get('reachable')} "
              f"model_listed={probe.get('model_listed')} "
              f"chat_ok={probe.get('chat_ok')} (external service; "
              "failures here are environment, not the container)")
    finally:
        subprocess.run(["docker", "compose", "down", "-v"],
                       capture_output=True, text=True, env=env)
    print("container check complete")


if __name__ == "__main__":
    main()
