"""Deterministic and live scenario execution."""

from __future__ import annotations

import copy
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .. import util
from ..app import App
from ..config import DEFAULTS, Config
from ..model import ModelClient, ScriptedModel
from ..runtime import RunResult
from .assertions import Observation, evaluate
from .fixtures import apply_fixtures
from .models import (
    AssertionResult,
    EvaluationRun,
    Scenario,
    ScenarioResult,
    ScenarioStep,
    StepResult,
)
from .quality import evaluate_quality
from .safety import prepare_sandbox, scenario_dir

# Settings applied to every scenario unless it overrides them. They remove
# background noise (auto-drafts, scheduled consolidation, web discovery) so a
# failure names the behavior under test, not an unrelated tick.
BASELINE: dict[str, Any] = {
    "web": {"enabled": False},
    "skills": {"auto_draft": False},
    "scheduler": {
        "enabled": False,
        "tick_interval_s": 3600.0,
        "max_runs_per_tick": 1,
        "approval_notify_s": 1_000_000.0,
        "paused_notify_s": 1_000_000.0,
    },
    "consolidation": {"schedule_interval_s": 0.0},
    "memory": {"lifetime": {"enabled": False}},
    "model": {"connect_retries": 1, "retry_backoff_s": 0.2},
}

DETERMINISTIC_DEFAULT_REPLY = "Done."


class EvaluationError(RuntimeError):
    pass


class EndpointUnavailable(EvaluationError):
    pass


def run_evaluation(
    scenarios: list[Scenario],
    *,
    mode: str = "deterministic",
    repeats: int = 1,
    sandbox: Path | str | None = None,
    config_from: Path | str | None = None,
    results_dir: Path | str | None = None,
    tag: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    allow_unlisted_model: bool = False,
    keep_sandbox: bool = False,
    environ: dict[str, str] | None = None,
) -> EvaluationRun:
    evaluator = Evaluator(
        scenarios,
        mode=mode,
        repeats=repeats,
        sandbox=sandbox,
        config_from=config_from,
        results_dir=results_dir,
        tag=tag,
        model=model,
        base_url=base_url,
        api_key=api_key,
        allow_unlisted_model=allow_unlisted_model,
        keep_sandbox=keep_sandbox,
        environ=environ,
    )
    return evaluator.run()


def _read_overlay(config_from: Path | str | None) -> dict[str, Any]:
    source = config_from
    if source is None:
        candidate = Path("data") / "config.yaml"
        if not candidate.is_file():
            return {}
        path = candidate
    else:
        path = Path(source)
        if path.is_dir():
            path = path / "config.yaml"
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise EvaluationError(f"cannot read {path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def load_base_config(
    config_from: Path | str | None = None,
    *,
    environ: dict[str, str] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Defaults + deployed config + environment/model overrides.

    Precedence (low to high): built-in defaults, ``config.yaml``,
    ``HARNESS_MODEL_*`` environment variables, explicit arguments.
    """
    data = copy.deepcopy(DEFAULTS)
    _deep_merge(data, _read_overlay(config_from))
    env = dict(environ if environ is not None else os.environ)
    values = data.setdefault("model", {})
    for env_name, key in (
        ("HARNESS_MODEL_BASE_URL", "base_url"),
        ("HARNESS_MODEL_NAME", "name"),
        ("HARNESS_MODEL_API_KEY", "api_key"),
    ):
        value = env.get(env_name)
        if value:
            values[key] = value
    if base_url:
        values["base_url"] = base_url
    if model:
        values["name"] = model
    if api_key is not None:
        values["api_key"] = api_key
    return data


class Evaluator:
    def __init__(
        self,
        scenarios: list[Scenario],
        *,
        mode: str = "deterministic",
        repeats: int = 1,
        sandbox: Path | str | None = None,
        config_from: Path | str | None = None,
        results_dir: Path | str | None = None,
        tag: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        allow_unlisted_model: bool = False,
        keep_sandbox: bool = False,
        environ: dict[str, str] | None = None,
    ):
        if mode not in ("deterministic", "live"):
            raise EvaluationError(f"unknown mode: {mode}")
        if not scenarios:
            raise EvaluationError("no scenarios selected")
        self.scenarios = scenarios
        self.mode = mode
        self.repeats = max(1, int(repeats))
        self.config_from = config_from
        self.results_dir = Path(results_dir or Path("data") / "eval" / "results")
        self.environ = dict(environ if environ is not None else os.environ)
        self.model_name = model
        self.base_url = base_url
        self.api_key = api_key
        self.allow_unlisted_model = allow_unlisted_model
        self.keep_sandbox = keep_sandbox
        self.sandbox = prepare_sandbox(
            sandbox, repo_root=self._repo_root(), data_dir=config_from
        )
        stamp = util.now_utc().strftime("%Y%m%dT%H%M%SZ")
        self.tag = tag or f"{mode}_{stamp}"
        suffix = util.new_id("run").split("_")[-1][:6]
        self.run_id = f"eval_{stamp}_{suffix}"
        self.endpoint: dict[str, Any] = {}

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _scenario_config(self, scenario: Scenario) -> dict[str, Any]:
        data = load_base_config(
            self.config_from,
            environ=self.environ,
            model=self.model_name,
            base_url=self.base_url,
            api_key=self.api_key,
        )
        _deep_merge(data, copy.deepcopy(BASELINE))
        _deep_merge(data, copy.deepcopy(scenario.settings))
        return data

    @staticmethod
    def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
        model = dict(data.get("model") or {})
        if model.get("api_key"):
            model["api_key"] = "***"
        return {
            "model": model,
            "limits": data.get("limits"),
            "context": data.get("context"),
            "scheduler": data.get("scheduler"),
            "memory": data.get("memory"),
            "modules": data.get("modules"),
            "skills": data.get("skills"),
            "subagents": data.get("subagents"),
            "approvals": data.get("approvals"),
        }

    # -- endpoint --------------------------------------------------------

    def _probe_endpoint(self, config_data: dict[str, Any]) -> ModelClient:
        config = Config(config_data, None)
        client = ModelClient(config)
        probe = client.probe()
        self.endpoint = probe
        name = config_data.get("model", {}).get("name")
        if not probe.get("reachable"):
            client.close()
            raise EndpointUnavailable(
                f"model endpoint is unreachable: {probe.get('detail') or ''}"
            )
        if not probe.get("chat_ok"):
            client.close()
            raise EndpointUnavailable(
                f"model endpoint did not complete a chat request: "
                f"{probe.get('detail') or ''}"
            )
        if probe.get("model_listed") is False and not self.allow_unlisted_model:
            available = probe.get("models") or []
            client.close()
            raise EndpointUnavailable(
                f"model {name!r} is not advertised by /v1/models "
                f"(available: {', '.join(str(item) for item in available) or 'none'}). "
                "Use --model NAME to pick one, or --allow-unlisted-model if the "
                "server is known to serve it without listing it."
            )
        return client

    # -- run -------------------------------------------------------------

    def run(self) -> EvaluationRun:
        first_config = self._scenario_config(self.scenarios[0])
        if self.mode == "live":
            client = self._probe_endpoint(first_config)
            client.close()
        run = EvaluationRun(
            run_id=self.run_id,
            mode=self.mode,
            tag=self.tag,
            started_at=util.iso(),
            model=self._model_descriptor(first_config),
            config=self._snapshot(first_config),
            endpoint=self.endpoint,
        )
        run.notes.append(
            "deterministic mode uses ScriptedModel; behavior and persistence "
            "are real, language quality is not measured"
            if self.mode == "deterministic"
            else "live mode calls the configured endpoint through the real runtime"
        )
        for scenario in self.scenarios:
            for repeat in range(1, self.repeats + 1):
                run.scenarios.append(
                    self._run_scenario(run.run_id, scenario, repeat)
                )
        run.finished_at = util.iso()

        from .report import write_reports

        json_path, md_path = write_reports(run, self.results_dir)
        run.results_path = str(json_path)
        run.report_path = str(md_path)
        # Persist the paths in the JSON too (the first write predates them).
        write_reports(run, self.results_dir)
        return run

    @staticmethod
    def _model_descriptor(config_data: dict[str, Any]) -> dict[str, Any]:
        model = config_data.get("model") or {}
        return {
            "name": model.get("name"),
            "base_url": model.get("base_url"),
            "temperature": model.get("temperature"),
            "max_tokens": model.get("max_tokens"),
            "thinking": model.get("enable_thinking"),
            "context_window": model.get("context_window"),
        }

    def _run_scenario(
        self, run_id: str, scenario: Scenario, repeat: int
    ) -> ScenarioResult:
        result = ScenarioResult(
            scenario_id=scenario.id,
            name=scenario.name,
            category=scenario.category,
            severity=scenario.severity,
            mode=self.mode,
            repeat=repeat,
            run_id=run_id,
        )
        if not scenario.supports(self.mode):
            result.status = "skipped"
            result.error = f"not enabled for {self.mode} mode"
            return result
        started = time.monotonic()
        data_dir = scenario_dir(self.sandbox, run_id, scenario.id, repeat)
        result.data_dir = str(data_dir)
        config_data = self._scenario_config(scenario)
        config = Config(config_data, None)
        model: Any
        if self.mode == "live":
            model = ModelClient(config)
        else:
            model = ScriptedModel(default=DETERMINISTIC_DEFAULT_REPLY)
        app: App | None = None
        try:
            app = self._build_app(data_dir, config, model)
            fixtures = apply_fixtures(app, scenario.fixtures)
            if fixture_errors := fixtures.get("errors"):
                raise EvaluationError(f"fixture setup failed: {fixture_errors}")
            for index, step in enumerate(scenario.steps, start=1):
                step_result, app = self._run_step(
                    app, config, model, scenario, step, index
                )
                result.steps.append(step_result)
                if step_result.error:
                    result.error = step_result.error
                    break
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = "error"
        finally:
            if app is not None:
                try:
                    app.shutdown()
                except Exception:
                    pass
            if hasattr(model, "close"):
                try:
                    model.close()
                except Exception:
                    pass
            if not self.keep_sandbox and scenario.cleanup != "retain":
                shutil.rmtree(data_dir.parent, ignore_errors=True)
        if result.status != "error":
            failed = any(
                assertion.status == "failed"
                for step in result.steps for assertion in step.assertions
            )
            result.status = "failed" if failed or result.error else "passed"
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _build_app(self, data_dir: Path, config: Config, model: Any) -> App:
        app = App(data_dir, config=config, model=model)
        app.startup()
        return app

    # -- steps -----------------------------------------------------------

    def _run_step(
        self,
        app: App,
        config: Config,
        model: Any,
        scenario: Scenario,
        step: ScenarioStep,
        index: int,
    ) -> tuple[StepResult, App]:
        step_result = StepResult(
            index=index,
            kind=step.kind,
            name=step.name or f"{step.kind} #{index}",
            input=step.input,
            started_at=util.iso(),
        )
        started = time.monotonic()
        observation = Observation(
            app=app, step=step, step_index=index,
            started_at=step_result.started_at, severity=scenario.severity,
        )
        try:
            app = self._execute(app, config, model, scenario, step,
                                step_result, observation)
            observation.app = app
        except Exception as exc:
            step_result.error = f"{type(exc).__name__}: {exc}"
            observation.evidence["error"] = step_result.error
        step_result.finished_at = util.iso()
        step_result.duration_ms = int((time.monotonic() - started) * 1000)
        step_result.evidence = observation.evidence
        try:
            assertions = evaluate(observation)
        except Exception as exc:
            assertions = [AssertionResult(
                id=f"s{index}.assertions.error",
                check="assertions",
                status="failed",
                expected="assertions to evaluate",
                observed=f"{type(exc).__name__}: {exc}",
                message="assertion evaluation failed",
                severity=scenario.severity,
            )]
        if step_result.error:
            assertions.insert(0, AssertionResult(
                id=f"s{index}.step.error",
                check="step",
                status="failed",
                expected="step to complete",
                observed=step_result.error,
                message=step_result.error,
                severity=scenario.severity,
            ))
        step_result.assertions = assertions
        try:
            step_result.quality = evaluate_quality(observation)
        except Exception as exc:
            step_result.quality = {
                "error": f"{type(exc).__name__}: {exc}",
                "score": None,
                "criteria": [],
            }
        step_result.status = (
            "failed" if any(a.status == "failed" for a in assertions) else "passed"
        )
        return step_result, app

    def _execute(
        self,
        app: App,
        config: Config,
        model: Any,
        scenario: Scenario,
        step: ScenarioStep,
        step_result: StepResult,
        observation: Observation,
    ) -> App:
        kind = step.kind
        if self.mode == "deterministic":
            # A script belongs to this step only. Every step resets the queue,
            # including tick/inbox/approval steps, because those paths can also
            # call the model (a promoted task, a resumed run, a classifier).
            model.scripts = list(step.script or [])
        if kind == "turn":
            if not step.input.strip():
                raise EvaluationError("turn step needs input")
            result = app.runtime.submit_user_message(step.input, source="web")
            self._record_result(step_result, observation, result)
        elif kind == "inspect":
            observation.evidence["tasks"] = len(app.db.query(
                "SELECT id FROM tasks"
            ))
        elif kind == "tick":
            observation.evidence["heartbeat"] = app.scheduler.heartbeat()
        elif kind == "inbox":
            payload = step.option("event") or {}
            if not isinstance(payload, dict):
                raise EvaluationError("inbox step needs an event mapping")
            observation.evidence["inbox"] = app.inbox.receive(
                source=str(payload.get("source") or "eval"),
                external_id=str(payload.get("external_id") or util.new_id("ext")),
                title=str(payload.get("title") or "Evaluation event"),
                body=str(payload.get("body") or ""),
                kind=str(payload.get("kind") or "event"),
                severity=str(payload.get("severity") or "info"),
                meta=payload.get("meta"),
            )
            if step.option("triage", False):
                max_runs = step.option("max_runs")
                observation.evidence["triage"] = app.inbox.triage(
                    max_runs=int(max_runs) if max_runs is not None else None
                )
                self._record_inbox_run(
                    app, step_result, observation,
                    str(observation.evidence["inbox"].get("id") or ""),
                )
        elif kind in ("approve", "reject", "cancel"):
            approval = self._select_approval(app, step)
            result = app.runtime.decide_approval(
                approval["id"], kind, source="eval"
            )
            self._record_result(step_result, observation, result)
        elif kind == "restart":
            app.shutdown()
            app = self._build_app(Path(app.data_dir), config, model)
            observation.evidence["restarted"] = True
        elif kind == "schedule_due":
            seconds = float(step.option("in_seconds", 0.0) or 0.0)
            now = util.now_utc() + timedelta(seconds=seconds)
            observation.evidence["submitted"] = app.scheduler.submit_due(now=now)
        elif kind == "sweep_memory":
            observation.evidence["sweep"] = app.memory.sweep_lifetime()
        elif kind == "sleep":
            time.sleep(float(step.option("seconds", 0.0) or 0.0))
        elif kind == "set_config":
            values = step.option("values") or {}
            if not isinstance(values, dict):
                raise EvaluationError("set_config needs a 'values' mapping")
            for key, value in values.items():
                app.config.set(str(key), value)
            observation.evidence["config"] = values
        elif kind == "create_task":
            objective = str(
                step.option("objective") or step.input or ""
            ).strip()
            if not objective:
                raise EvaluationError("create_task needs an objective")
            task = app.tasks.create(
                objective=objective,
                domain_id=self._domain_id(app, step.option("domain")),
                origin=str(step.option("origin") or "eval"),
                role=str(step.option("role") or "user"),
                parent_task_id=step.option("parent_task_id"),
                meta=dict(step.option("meta") or {}),
            )
            status = step.option("status")
            if status:
                app.tasks.set_status(
                    task["id"], str(status),
                    reason=str(step.option("reason") or "evaluation step"),
                )
            observation.evidence["task"] = task
        elif kind == "create_schedule":
            schedule_kind = str(step.option("schedule_kind") or "once")
            at_ts = step.option("at_ts")
            if schedule_kind == "once" and at_ts is None:
                seconds = float(step.option("at_in_seconds", 0.0) or 0.0)
                at_ts = util.iso(util.now_utc() + timedelta(seconds=seconds))
            schedule = app.scheduler.create_schedule(
                name=str(
                    step.option("schedule_name") or step.option("name")
                    or step.input or "eval schedule"
                ),
                prompt=str(step.option("prompt") or step.input or "eval prompt"),
                kind=schedule_kind,
                at_ts=at_ts,
                interval_s=step.option("interval_s"),
                time_of_day=step.option("time_of_day"),
                weekday=step.option("weekday"),
                tz=step.option("tz"),
                domain_id=self._domain_id(app, step.option("domain")),
            )
            observation.evidence["schedule"] = schedule
        elif kind == "create_memory":
            content = str(step.option("content") or step.input or "").strip()
            if not content:
                raise EvaluationError("create_memory needs content")
            observation.evidence["memory"] = app.memory.remember(
                content,
                kind=step.option("kind"),
                scope=step.option("scope"),
                domain_id=self._domain_id(app, step.option("domain")),
                origin=str(step.option("origin") or "eval"),
                provenance={"source": "eval_step"},
                tags=list(step.option("tags") or []),
                domains=app.domains,
                artifact_body=step.option("detail"),
            )
        elif kind in ("archive_memory", "restore_memory"):
            memory = self._select_memory(app, step)
            if kind == "archive_memory":
                observation.evidence["memory"] = app.memory.archive(
                    memory["id"],
                    changed_by="eval",
                    reason=str(step.option("reason") or "evaluation step"),
                )
            else:
                observation.evidence["memory"] = app.memory.restore(
                    memory["id"],
                    changed_by="eval",
                    reason=str(step.option("reason") or "evaluation step"),
                )
        elif kind == "module_status":
            module_id = str(
                step.option("id") or step.option("module") or ""
            ).strip()
            action = str(step.option("action") or "enable").strip().lower()
            if not module_id:
                raise EvaluationError("module_status needs a module id")
            if action in ("activate", "enable"):
                module = app.modules.set_enabled(module_id, True)
            elif action in ("disable", "deactivate"):
                module = app.modules.set_enabled(module_id, False)
            elif action == "reload":
                observation.evidence["reload"] = app.modules.reload(module_id)
                module = app.modules.get(module_id)
            else:
                raise EvaluationError(f"unknown module action: {action}")
            observation.evidence["module"] = {
                "id": getattr(module, "id", module_id),
                "status": getattr(module, "status", None),
            }
        else:  # loader validates kinds; this is a defensive error
            raise EvaluationError(f"unsupported step kind: {kind}")
        return app

    @staticmethod
    def _record_result(step_result: StepResult, observation: Observation,
                       result: Any) -> None:
        observation.result = result
        step_result.run_status = getattr(result, "status", "")
        step_result.task_id = getattr(result, "task_id", "")
        step_result.run_id = getattr(result, "run_id", "")
        step_result.response = getattr(result, "message", "") or ""

    @staticmethod
    def _record_inbox_run(app: App, step_result: StepResult,
                          observation: Observation, event_id: str) -> None:
        """Expose the promoted task's run as this step's result.

        ``inbox.triage`` returns counters, not a ``RunResult``. Inbox scenarios
        still need the transcript outcome, so read the run it just started.
        """
        if not event_id:
            return
        row = app.db.query_one(
            "SELECT r.id, r.task_id, r.status FROM runs r"
            " JOIN tasks t ON t.id=r.task_id"
            " WHERE t.origin LIKE 'inbox:%' AND t.meta LIKE ?"
            " ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1",
            (f"%{event_id}%",),
        )
        if row is None:
            return
        message = app.db.query_one(
            "SELECT content FROM messages WHERE run_id=? AND role='assistant'"
            " AND visible=1 AND content<>'' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        )
        step_result.run_status = str(row["status"])
        step_result.task_id = str(row["task_id"])
        step_result.run_id = str(row["id"])
        step_result.response = str(message["content"]) if message else ""
        observation.result = RunResult(
            status=step_result.run_status,
            run_id=step_result.run_id,
            task_id=step_result.task_id,
            message=step_result.response,
        )
        observation.evidence["inbox_run"] = {
            "status": step_result.run_status,
            "task_id": step_result.task_id,
            "run_id": step_result.run_id,
            "response": step_result.response,
        }

    @staticmethod
    def _domain_id(app: App, value: Any) -> str | None:
        if value in (None, ""):
            return None
        wanted = str(value)
        domain = app.domains.get(wanted) or app.domains.by_name(wanted)
        if domain is None:
            raise EvaluationError(f"unknown domain: {wanted}")
        return domain["id"]

    @staticmethod
    def _select_memory(app: App, step: ScenarioStep) -> dict[str, Any]:
        selector = step.option("memory") or {}
        if isinstance(selector, str):
            selector = {"content_contains": selector}
        if not isinstance(selector, dict):
            raise EvaluationError("memory selector must be a mapping or string")
        memories = app.memory.list(status="any", limit=1000)
        for memory in memories:
            if selector.get("id") and memory["id"] != selector["id"]:
                continue
            if selector.get("content_contains") and selector[
                "content_contains"
            ].lower() not in (memory.get("content") or "").lower():
                continue
            if selector.get("kind") and memory.get("kind") != selector["kind"]:
                continue
            if selector.get("scope") and memory.get("scope") != selector["scope"]:
                continue
            if selector.get("status") and memory.get("status") != selector["status"]:
                continue
            return memory
        raise EvaluationError("no memory matched the step selector")

    @staticmethod
    def _select_approval(app: App, step: ScenarioStep) -> dict[str, Any]:
        pending = app.approvals.pending()
        if not pending:
            raise EvaluationError("no pending approval to decide")
        selector = step.option("approval") or {}
        if isinstance(selector, str):
            selector = {"id": selector}
        for approval in pending:
            if selector.get("id") and approval["id"] != selector["id"]:
                continue
            if selector.get("tool") and approval["tool"] != selector["tool"]:
                continue
            if selector.get("arguments_contains"):
                if selector["arguments_contains"] not in util.dumps(
                    approval.get("arguments") or {}
                ):
                    continue
            return approval
        raise EvaluationError(
            "no pending approval matched the step selector"
        )


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target
