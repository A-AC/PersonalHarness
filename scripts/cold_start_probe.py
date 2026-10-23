"""Probe llama-server cold-start, eviction, and queueing behavior.

The probe never mutates harness state. It records:

* whether the model is reported loaded after an idle period (idle eviction),
* first-request-after-idle latency versus back-to-back requests,
* server-side load/eviction hints from the router (``max_instances``,
  ``models_autoload``) and the model status,
* optional forced cold start via the router unload endpoint,
* optional concurrent requests to show queueing behind another client.

Usage:
    python scripts/cold_start_probe.py --idle 180
    python scripts/cold_start_probe.py --idle 0 --concurrency 2
    python scripts/cold_start_probe.py --unload --idle 0

Results are written to experiments/results/cold_start.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT = "http://192.168.50.40:8181/v1/chat/completions"
DEFAULT_MODEL = "qwen3.5-9b"
RESULTS = ROOT / "experiments" / "results" / "cold_start.json"


def base_url(endpoint: str) -> str:
    return endpoint.rsplit("/v1/", 1)[0]


def get_json(url: str, timeout: float = 10.0) -> Any:
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def model_state(endpoint: str, model: str) -> dict[str, Any]:
    try:
        body = get_json(base_url(endpoint) + "/v1/models")
    except httpx.HTTPError as exc:
        return {"error": str(exc)}
    for item in body.get("data") or []:
        if item.get("id") == model:
            return {
                "status": (item.get("status") or {}).get("value"),
                "context_length": item.get("context_length"),
            }
    return {"status": "not-listed"}


def router_props(endpoint: str) -> dict[str, Any]:
    try:
        props = get_json(base_url(endpoint) + "/props")
    except httpx.HTTPError as exc:
        return {"error": str(exc)}
    return {
        "role": props.get("role"),
        "max_instances": props.get("max_instances"),
        "models_autoload": props.get("models_autoload"),
        "build_info": props.get("build_info"),
    }


def try_unload(endpoint: str, model: str) -> dict[str, Any]:
    """Try documented and plausible router unload endpoints without assuming one."""
    attempts: list[dict[str, Any]] = []
    for path in ("/models/unload", "/v1/models/unload", f"/models/{model}/unload"):
        try:
            response = httpx.post(
                base_url(endpoint) + path, json={"model": model}, timeout=60.0
            )
        except httpx.HTTPError as exc:
            attempts.append({"path": path, "error": str(exc)})
            continue
        attempts.append(
            {
                "path": path,
                "status_code": response.status_code,
                "body": response.text[:300],
            }
        )
        if response.status_code < 300:
            break
    return {"attempts": attempts, "state": model_state(endpoint, model)}


def chat_call(
    endpoint: str,
    model: str,
    marker: int,
    *,
    timeout: float = 180.0,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with the single word ok. Request marker {marker}.",
            }
        ],
        "temperature": 0.2,
        "max_tokens": 8,
        "stream": True,
        "stream_options": {"include_usage": True},
        # Keep the cold-start probe comparable to the latest experiments:
        # reasoning is the dominant source of token-level latency.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.monotonic()
    first_chunk: float | None = None
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    with httpx.stream("POST", endpoint, json=payload, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            chunk = json.loads(data)
            if first_chunk is None:
                first_chunk = time.monotonic()
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):
                timings = chunk["timings"]
    finished = time.monotonic()
    return {
        "marker": marker,
        "duration_ms": int((finished - started) * 1000),
        "ttft_ms": int((first_chunk - started) * 1000) if first_chunk else None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "server_prompt_ms": timings.get("prompt_ms"),
        "server_predicted_ms": timings.get("predicted_ms"),
    }


def concurrency_probe(
    endpoint: str, model: str, count: int, base_marker: int
) -> list[dict[str, Any]]:
    """Send ``count`` requests simultaneously to expose per-slot queueing."""
    import concurrent.futures

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(chat_call, endpoint, model, base_marker + index)
            for index in range(count)
        ]
        results = [future.result() for future in futures]
    wall_ms = int((time.monotonic() - started) * 1000)
    for item in results:
        item["wall_ms_with_peers"] = wall_ms
    return results


def _summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [item["duration_ms"] for item in calls]
    ttfts = [item["ttft_ms"] for item in calls if item["ttft_ms"] is not None]
    return {
        "count": len(calls),
        "duration_p50_ms": statistics.median(durations) if durations else None,
        "duration_min_ms": min(durations) if durations else None,
        "duration_max_ms": max(durations) if durations else None,
        "ttft_p50_ms": statistics.median(ttfts) if ttfts else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--idle", type=float, default=180.0, help="Idle seconds before the first call.")
    parser.add_argument("--warm", type=int, default=3, help="Back-to-back calls after the idle call.")
    parser.add_argument("--concurrency", type=int, default=0, help="Optional simultaneous-client probe.")
    parser.add_argument("--unload", action="store_true", help="Try a forced router unload first.")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--out", default=str(RESULTS), help="Result JSON path.")
    args = parser.parse_args()
    out_path = Path(args.out)

    report: dict[str, Any] = {
        "endpoint": args.endpoint,
        "model": args.model,
        "idle_s": args.idle,
        "router": router_props(args.endpoint),
        "state_before_idle": model_state(args.endpoint, args.model),
    }

    if args.unload:
        report["unload"] = try_unload(args.endpoint, args.model)
        report["state_after_unload"] = model_state(args.endpoint, args.model)

    if args.idle > 0:
        time.sleep(args.idle)

    report["state_after_idle"] = model_state(args.endpoint, args.model)
    report["first_after_idle"] = chat_call(
        args.endpoint, args.model, 1, timeout=args.timeout
    )
    report["warm"] = [
        chat_call(args.endpoint, args.model, 2 + index, timeout=args.timeout)
        for index in range(args.warm)
    ]
    report["state_after_warm"] = model_state(args.endpoint, args.model)
    report["summary"] = {
        "first_after_idle": _summary([report["first_after_idle"]]),
        "warm": _summary(report["warm"]),
    }

    if args.concurrency > 1:
        report["concurrency"] = concurrency_probe(
            args.endpoint, args.model, args.concurrency, 100
        )
        report["summary"]["concurrency"] = _summary(report["concurrency"])

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
