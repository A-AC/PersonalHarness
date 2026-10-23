#!/usr/bin/env python3
"""Build and smoke-check the container.

Usage:
    python scripts/container_check.py
    python scripts/container_check.py --keep

The check waits for /health, verifies the UI rejects a missing token, and
verifies a request carrying HARNESS_UI_TOKEN is accepted. It never calls the
inference backend.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("HARNESS_UI_TOKEN", "check-token")
PORT = os.environ.get("HARNESS_PORT", "8080")
BASE = f"http://127.0.0.1:{PORT}"


def run(*args: str) -> None:
    print("+", " ".join(args))
    subprocess.run(args, check=True)


def request(path: str, *, token: str | None = None) -> int:
    req = urllib.request.Request(BASE + path)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait_for_health(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if request("/health") == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError(f"container did not become healthy within {timeout:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    env = dict(os.environ)
    env["HARNESS_UI_TOKEN"] = TOKEN
    env.setdefault("HARNESS_PORT", PORT)

    def compose(*command: str) -> None:
        subprocess.run(["docker", "compose", *command], check=True, env=env)

    started = False
    try:
        compose("build")
        compose("up", "-d")
        started = True
        wait_for_health(args.timeout)
        assert request("/health") == 200, "health endpoint failed"
        assert request("/") == 401, "UI accepted a request without a token"
        assert request("/", token=TOKEN) == 200, "UI rejected the configured token"
        print("container check passed")
        return 0
    except (subprocess.CalledProcessError, RuntimeError, AssertionError) as exc:
        print(f"container check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if started and not args.keep:
            try:
                compose("down")
            except subprocess.CalledProcessError as exc:
                print(f"warning: docker compose down failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
