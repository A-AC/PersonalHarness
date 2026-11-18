"""Routing-strategy bake-off over the labelled k_routing corpus."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .. import util
from ..app import App
from ..config import Config
from ..model import ModelClient, ScriptedModel
from .fixtures import apply_fixtures
from .models import Scenario
from .routing import routing_metrics
from .runner import load_base_config
from .provenance import source_metadata


class _HashEmbeddingClient:
    """Deterministic plumbing fake: stable vectors, not semantic quality."""

    model = "scripted-fake"

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([
                (digest[index] / 255.0) * 2.0 - 1.0
                for index in range(8)
            ])
        return vectors


class _ScriptedLLM:
    def classify(self, text, cards, turns):
        return "general", "low", "low_confidence", ""


def _uses_embedding(strategy: str) -> bool:
    return strategy in ("embedding", "hybrid")


def _uses_llm(strategy: str) -> bool:
    return strategy in ("llm", "hybrid")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(
        fraction * (len(ordered) - 1)
    ))))
    return round(ordered[index], 2)


def _scenario_split(scenario: Scenario) -> str:
    tags = {str(tag).lower() for tag in scenario.tags}
    return "heldout" if {"heldout", "held-out"} & tags else "base"


def _latest_routed(app: App) -> dict[str, Any] | None:
    row = app.db.query_one(
        "SELECT * FROM event_log WHERE type='domain.routed'"
        " ORDER BY id DESC LIMIT 1"
    )
    if row is None:
        return None
    data = util.loads(row["data"] or "{}", {})
    return {
        "domain_id": str(row["domain_id"] or "general"),
        "data": data if isinstance(data, dict) else {},
    }


def _config_for(
    base: dict[str, Any], strategy: str
) -> Config:
    data = copy.deepcopy(base)
    data.setdefault("routing", {})["strategy"] = strategy
    data.setdefault("routing", {})["shadow_strategies"] = []
    data.setdefault("domains", {})["model_routing"] = "off"
    return Config(data, None)


def _run_strategy(
    scenarios: Sequence[Scenario],
    strategy: str,
    *,
    mode: str,
    base_config: dict[str, Any],
    model_name: str | None,
    base_url: str | None,
    api_key: str | None,
    environ: dict[str, str] | None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    unavailable: str | None = None
    if mode == "live":
        probe_data = copy.deepcopy(base_config)
        probe_model = probe_data.setdefault("model", {})
        probe_model["timeout_s"] = min(
            float(probe_model.get("timeout_s", 5.0) or 5.0), 5.0
        )
        probe_model["connect_retries"] = 0
        probe = ModelClient(Config(probe_data, None)).probe()
        if not probe.get("reachable") or not probe.get("chat_ok"):
            return {
                "strategy": strategy,
                "available": False,
                "reason": (
                    "model endpoint unavailable: "
                    f"{probe.get('detail') or ''}"
                ),
                "splits": {},
            }
    for scenario in scenarios:
        path = Path(tempfile.mkdtemp(prefix="routing_bakeoff_"))
        config = _config_for(base_config, strategy)
        model = (
            ModelClient(config) if mode == "live"
            else ScriptedModel(default="ok")
        )
        app: App | None = None
        try:
            app = App(path, config=config, model=model)
            app.startup()
            if mode == "deterministic":
                app.runtime.router.embedding_client = _HashEmbeddingClient()
                app.runtime.router.llm_classifier = _ScriptedLLM()
            if mode == "live" and _uses_embedding(strategy):
                try:
                    app.runtime.router.embedding_client = None
                    app.runtime.router._router("embedding").client.encode(
                        ["bakeoff probe"]
                    )
                except Exception as exc:
                    unavailable = f"embedding endpoint unavailable: {exc}"
                    break
            apply_fixtures(app, scenario.fixtures)
            for index, step in enumerate(scenario.steps, start=1):
                if step.kind != "turn":
                    continue
                if mode == "deterministic":
                    model.scripts = list(step.script or [])
                app.runtime.submit_user_message(step.input, source="web")
                routed = _latest_routed(app)
                if not (
                    (step.expect.get("routing") or {}).get("domain")
                ):
                    continue
                expected = str(
                    (step.expect.get("routing") or {}).get("domain")
                )
                data = (routed or {}).get("data") or {}
                domain_id = str((routed or {}).get("domain_id") or "general")
                record = app.domains.get(domain_id)
                predicted = (
                    str(record["name"]) if record else domain_id
                )
                records.append({
                    "scenario_id": scenario.id,
                    "step": index,
                    "split": _scenario_split(scenario),
                    "expected": expected,
                    "predicted": predicted,
                    "domain_id": domain_id,
                    "latency_ms": data.get("latency_ms"),
                    "fallback": data.get("fallback"),
                    "confidence": data.get("confidence"),
                    "strategy": data.get("strategy"),
                })
        except Exception as exc:
            records.append({
                "scenario_id": scenario.id,
                "split": _scenario_split(scenario),
                "expected": None,
                "predicted": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            if app is not None:
                try:
                    app.shutdown()
                except Exception:
                    pass
            shutil.rmtree(path, ignore_errors=True)
    if unavailable:
        return {
            "strategy": strategy,
            "available": False,
            "reason": unavailable,
            "splits": {},
        }
    splits: dict[str, Any] = {}
    for split_name in ("base", "heldout", "all"):
        subset = [
            item for item in records
            if item.get("expected") is not None
            and (split_name == "all" or item["split"] == split_name)
        ]
        if not subset and split_name != "all":
            splits[split_name] = {"total": 0}
            continue
        metrics = routing_metrics(
            [item["expected"] for item in subset],
            [item["predicted"] for item in subset],
        )
        latencies = [
            float(item["latency_ms"]) for item in subset
            if item.get("latency_ms") is not None
        ]
        splits[split_name] = {
            **metrics,
            "fallback_count": sum(
                1 for item in subset if item.get("fallback")
            ),
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "errors": [
                item for item in records if item.get("error")
            ][:10],
        }
    return {
        "strategy": strategy,
        "available": True,
        "plumbing_only": mode == "deterministic",
        "splits": splits,
        "records": records,
    }


def run_bakeoff(
    scenarios: Sequence[Scenario],
    strategies: Sequence[str],
    *,
    mode: str = "deterministic",
    config_from: Path | str | None = None,
    results_dir: Path | str | None = None,
    tag: str = "routing_bakeoff",
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    base_config = load_base_config(
        config_from, environ=environ or dict(os.environ),
        model=model, base_url=base_url, api_key=api_key,
    )
    if mode == "live":
        # The bake-off measures routing labels, not reply length; keep the
        # completion budget small so a live sweep is bounded.
        model_config = base_config.setdefault("model", {})
        model_config["max_tokens"] = 128
        model_config["empty_retry_max_tokens"] = 128
        model_config["enable_thinking"] = False
    payload: dict[str, Any] = {
        "experiment": tag,
        "mode": mode,
        "strategies": list(strategies),
        "scenario_count": len(scenarios),
        "source": source_metadata(scenarios),
        "results": [],
    }
    for strategy in strategies:
        payload["results"].append(_run_strategy(
            scenarios, strategy, mode=mode, base_config=base_config,
            model_name=model, base_url=base_url, api_key=api_key,
            environ=environ,
        ))
    out = Path(results_dir or Path("data") / "eval" / "results")
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in tag
    ) or "routing_bakeoff"
    json_path = out / f"{safe}.json"
    md_path = out / f"{safe}.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_bakeoff(payload) + "\n", encoding="utf-8")
    payload["json_path"] = str(json_path)
    payload["md_path"] = str(md_path)
    return payload


def render_bakeoff(payload: dict[str, Any]) -> str:
    lines = [
        f"# Routing bake-off: {payload.get('experiment') or 'routing_bakeoff'}",
        "",
        f"- Mode: {payload.get('mode')}",
        f"- Scenarios: {payload.get('scenario_count')}",
        "",
        "| strategy | split | available | accuracy | wrong-domain | General fallback | fallbacks | p50 ms | p95 ms |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in payload.get("results", []):
        strategy = result.get("strategy")
        if not result.get("available"):
            lines.append(
                f"| {strategy} | - | no | - | - | - | - | - | - |"
            )
            continue
        for split in ("base", "heldout"):
            metrics = (result.get("splits") or {}).get(split) or {}
            if not metrics.get("total"):
                continue
            lines.append(
                f"| {strategy} | {split} | yes "
                f"| {metrics['accuracy']:.4f} "
                f"| {metrics['false_positive_rate']:.4f} "
                f"| {metrics['general_fallback_rate']:.4f} "
                f"| {metrics['fallback_count']} "
                f"| {metrics['latency_p50_ms'] if metrics['latency_p50_ms'] is not None else '-'} "
                f"| {metrics['latency_p95_ms'] if metrics['latency_p95_ms'] is not None else '-'} |"
            )
    lines.extend(["", "## Confusion matrices", ""])
    for result in payload.get("results", []):
        if not result.get("available"):
            continue
        strategy = result.get("strategy")
        for split in ("base", "heldout"):
            metrics = (result.get("splits") or {}).get(split) or {}
            table = metrics.get("confusion") or {}
            labels = table.get("labels") or []
            if not labels:
                continue
            lines.append(f"### {strategy} / {split}")
            lines.append("")
            lines.append(
                "| expected \\ predicted | "
                + " | ".join(labels) + " |"
            )
            lines.append(
                "| --- | " + " | ".join("---:" for _ in labels) + " |"
            )
            for row in labels:
                values = table["rows"].get(row) or {}
                lines.append(
                    f"| {row} | "
                    + " | ".join(str(values.get(col, 0)) for col in labels)
                    + " |"
                )
            lines.append("")
    lines.extend(["## Notes", ""])
    for result in payload.get("results", []):
        if not result.get("available"):
            lines.append(
                f"- `{result['strategy']}` unavailable: {result.get('reason')}"
            )
    if payload.get("mode") == "deterministic":
        lines.append(
            "- Deterministic mode uses scripted embedding/LLM fakes; it tests "
            "plumbing and reporting, not semantic accuracy."
        )
    lines.append(
        "- Wrong-domain means a specific domain that differs from the label; "
        "General fallback is a miss, not a wrong-domain result."
    )
    return "\n".join(lines)
