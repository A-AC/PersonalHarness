from __future__ import annotations

import asyncio
import json
import threading
from html import escape
from urllib.parse import parse_qs, quote

from jinja2 import Template
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from harness.app import Harness
from harness.contracts.chat import TurnEvent
from harness.contracts.profile import PersonaDraft
from harness.contracts.settings import SettingsError
from harness.domains.inspect import inspect_domain
from harness.measure import learning_report
from harness.storage.atomic import new_id


LAYOUT = Template(
    """<!doctype html>
<html><head><meta charset="utf-8"><title>Harness</title>
<style>
body{font-family:sans-serif;margin:0;background:#111;color:#eee}
nav{background:#222;padding:8px 16px;display:flex;gap:12px;flex-wrap:wrap}
nav a{color:#9cf;text-decoration:none}
main{padding:16px;max-width:1200px}
table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #333;padding:6px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;background:#1b1b1b;padding:12px;overflow:auto}
.err{color:#f66}
.ok{color:#6f6}
.muted{color:#999}
textarea{width:100%;min-height:5em;background:#1b1b1b;color:#eee}
input{background:#1b1b1b;color:#eee}
.msg{border-left:3px solid #333;padding:8px 12px;margin:10px 0}
.msg.user{border-color:#48a}
.msg.assistant{border-color:#484}
.msg .role{color:#999;font-size:.85em;margin-bottom:4px}
.msg .text{white-space:pre-wrap}
.msg .meta{margin-top:8px;font-size:.9em}
.msg .meta ul{margin:4px 0;padding-left:20px}
.env{padding:1px 5px;border:1px solid #555;border-radius:3px}
.env.ok{border-color:#484}
.env.err{border-color:#844;color:#f66}
body.chat-page{height:100vh;overflow:hidden;display:flex;flex-direction:column}
body.chat-page nav{flex:0 0 auto}
body.chat-page main{flex:1 1 auto;min-height:0;max-width:none;padding:0;overflow:hidden;display:flex;width:100%}
.chat-panel{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;width:100%;max-width:1200px;margin:0 auto;padding:12px 16px 0;box-sizing:border-box}
.chat-head{flex:0 0 auto}
.chat-head h1{margin:0 0 4px}
.chat-domain{margin:0 0 6px}
.chat-domain select{background:#1b1b1b;color:#eee}
#chat-log{flex:1 1 auto;min-height:0;overflow-y:auto;padding:4px 8px 4px 0}
#live{flex:0 0 auto;max-height:32vh;overflow:auto;border-top:1px solid #333;padding:6px 0}
#live summary{cursor:pointer;color:#9cf}
#live-stages{max-height:12em;overflow:auto}
#live-tokens{max-height:12em;overflow:auto;margin:0}
#chat-composer{flex:0 0 auto;padding:8px 0 12px;border-top:1px solid #333}
#chat-text{resize:none;max-height:12em;overflow-y:auto;box-sizing:border-box}
</style></head>
<body{% if body_class %} class="{{ body_class }}"{% endif %}>
<nav>
<a href="/chat">chat</a><a href="/">traces</a><a href="/memory">memory</a><a href="/persona">persona</a>
<a href="/tasks">tasks</a><a href="/domains">domains</a><a href="/history">history</a><a href="/capabilities">capabilities</a>
<a href="/consolidation">consolidation</a><a href="/settings">settings</a>
<a href="/events">events</a><a href="/learning">learning</a><a href="/reset">reset</a>
</nav>
<main{% if main_class %} class="{{ main_class }}"{% endif %}>{{ body|safe }}</main></body></html>"""
)


def _page(body: str, *, body_class: str = "", main_class: str = "") -> HTMLResponse:
    return HTMLResponse(LAYOUT.render(body=body, body_class=body_class, main_class=main_class))


_CHAT_JS = """
<script>
(function () {
  var form = document.getElementById('chat-form');
  var input = document.getElementById('chat-text');
  var send = document.getElementById('chat-send');
  var log = document.getElementById('chat-log');
  var live = document.getElementById('live');
  var status = document.getElementById('live-status');
  var stages = document.getElementById('live-stages');
  var tokens = document.getElementById('live-tokens');
  var error = document.getElementById('live-error');
  var streamTokens = __STREAM_TOKENS__;
  var labels = {
    'session': 'session',
    'structure_check': 'structure check',
    'plan.prompt_build': 'task creation: prompt',
    'plan.heuristic': 'task creation: heuristic',
    'plan.model_call': 'task creation: model',
    'plan.parse_validation': 'task creation: validation',
    'plan.task_create': 'task creation: create tasks',
    'domain_suggestion': 'structure suggestion',
    'prompt_build': 'decision: prompt',
    'model_call': 'decision: model',
    'parse_validation': 'decision: validation',
    'execution': 'execution',
    'response': 'response',
    'turn.response': 'response',
    'error': 'error'
  };
  function label(name) { return labels[name] || name; }
  function scrollLog() { log.scrollTop = log.scrollHeight; }
  function growInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 192) + 'px';
  }
  function addStage(data) {
    if (data.running) {
      status.textContent = 'running: ' + label(data.stage);
      return;
    }
    var li = document.createElement('li');
    var text = label(data.stage) + ' - ' + data.duration_ms + ' ms';
    if (data.start_ms !== undefined) text += ' (start ' + data.start_ms + ' ms)';
    if (data.detail) {
      try { text += ' ' + JSON.stringify(data.detail); } catch (ignore) {}
    }
    li.textContent = text;
    stages.appendChild(li);
    stages.scrollTop = stages.scrollHeight;
  }
  function fail(message) {
    status.textContent = 'failed';
    error.textContent = 'Turn failed: ' + message;
    live.open = true;
    send.disabled = false;
  }
  function appendInteraction(interactionId) {
    return fetch('/chat/interactions/' + encodeURIComponent(interactionId), {
      headers: {'accept': 'text/html'}
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.text();
    }).then(function (html) {
      var empty = log.querySelector('[data-empty]');
      if (empty) empty.parentNode.removeChild(empty);
      var holder = document.createElement('div');
      holder.innerHTML = html;
      while (holder.firstChild) log.appendChild(holder.firstChild);
      scrollLog();
    });
  }
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    live.hidden = false;
    live.open = true;
    status.textContent = 'starting...';
    stages.textContent = '';
    tokens.textContent = '';
    error.textContent = '';
    fetch('/chat/turn', {
      method: 'POST',
      headers: {'content-type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({text: text})
    }).then(function (response) {
      return response.text().then(function (body) {
        var data;
        try { data = JSON.parse(body); } catch (ignore) { data = {error: body || ('HTTP ' + response.status)}; }
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
      });
    }).then(function (data) {
      status.textContent = 'running...';
      var source = new EventSource('/chat/turns/' + encodeURIComponent(data.job_id) + '/events');
      source.addEventListener('turn.started', function (event) {
        var started = JSON.parse(event.data);
        status.textContent = 'running ' + started.interaction_id;
      });
      source.addEventListener('stage', function (event) { addStage(JSON.parse(event.data)); });
      source.addEventListener('token', function (event) {
        if (!streamTokens) return;
        var data = JSON.parse(event.data);
        tokens.textContent += data.text;
        tokens.scrollTop = tokens.scrollHeight;
      });
      source.addEventListener('turn.completed', function (event) {
        source.close();
        var data = JSON.parse(event.data);
        status.textContent = 'complete';
        send.disabled = false;
        input.value = '';
        growInput();
        if (!data.interaction_id) return;
        appendInteraction(data.interaction_id).catch(function (err) {
          status.textContent = 'complete; could not append reply: ' + (err.message || err);
        });
      });
      source.addEventListener('turn.failed', function (event) {
        source.close();
        var data = JSON.parse(event.data);
        fail(data.error || 'unknown error');
        if (data.interaction_id) {
          appendInteraction(data.interaction_id).catch(function (err) {
            error.textContent += ' (could not append turn: ' + (err.message || err) + ')';
          });
        }
      });
      source.onerror = function () {
        if (source.readyState === EventSource.CLOSED) {
          fail('event stream closed before the turn finished');
        } else {
          status.textContent = 'stream interrupted; reconnecting...';
        }
      };
    }).catch(function (err) { fail(err.message || String(err)); });
  });
  input.addEventListener('input', growInput);
  growInput();
  scrollLog();
})();
</script>
"""


def _error_page(message: str, status_code: int) -> HTMLResponse:
    response = _page(f"<h1>Error</h1><p class='err'>{_esc(message)}</p>")
    response.status_code = status_code
    return response


def _esc(value: object) -> str:
    return escape("" if value is None else str(value))


def _authorized(request: Request, harness: Harness) -> bool:
    token = str(harness.settings.get("ui.access_token"))
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer ") and header.split(" ", 1)[1] == token:
        return True
    if request.query_params.get("token") == token:
        return True
    return request.cookies.get("harness_token") == token


def _form(request_body: bytes) -> dict[str, str]:
    parsed = parse_qs(request_body.decode())
    return {key: values[0] for key, values in parsed.items() if values}


def _form_all(request_body: bytes) -> dict[str, list[str]]:
    return parse_qs(request_body.decode())


def _sse(index: int, event: TurnEvent) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    return f"id: {index}\nevent: {event.kind}\ndata: {payload}\n\n"


class _TurnJob:
    """In-memory buffer for one running/completed turn streamed over SSE."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[TurnEvent] = []
        self.done = False
        self.subscribers: list[asyncio.Queue] = []


def create_app(harness: Harness) -> Starlette:
    async def guard(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not _authorized(request, harness):
            return Response("unauthorized", status_code=401)
        if (
            harness.reset.busy
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path != "/reset"
        ):
            return JSONResponse({"error": "reset in progress"}, status_code=409)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Never let an unhandled handler error become an opaque
            # connection reset for the browser.
            return _error_page(f"Internal error: {type(exc).__name__}: {exc}", 500)
        if request.query_params.get("token") == str(harness.settings.get("ui.access_token")):
            response.set_cookie("harness_token", str(harness.settings.get("ui.access_token")), httponly=True, samesite="strict")
        return response

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def traces(request: Request) -> HTMLResponse:
        limit = int(harness.settings.get("ui.page_limit"))
        items = harness.traces.list_for(limit=limit)
        rows = "".join(
            f'<tr><td><a href="/traces/{_esc(item.id)}">{_esc(item.id)}</a></td>'
            f"<td>{_esc(item.role)}</td><td>{_esc(item.task_id)}</td><td>{_esc(item.validation)}</td>"
            f"<td>{_esc(item.duration_ms or item.latency_ms)}</td>"
            f"<td>{_esc(item.finish_reason)}</td><td>{_esc(item.ts)}</td></tr>"
            for item in reversed(items)
        )
        return _page(
            "<h1>Traces</h1><table><tr><th>id</th><th>role</th><th>task</th><th>validation</th>"
            "<th>duration ms</th><th>finish</th><th>ts</th></tr>"
            + rows
            + "</table>"
        )

    async def trace_detail(request: Request) -> HTMLResponse:
        item = harness.traces.get(request.path_params["trace_id"])
        if item is None:
            return _error_page("trace not found", 404)
        def rows(kind: str, values: list[dict]) -> str:
            return "".join(
                f"<li>{kind} <b>{_esc(row.get('section'))}</b> {_esc(row.get('item'))}"
                f" <span class='muted'>{_esc(row.get('reason'))}</span></li>"
                for row in values
            )

        included = rows("include", item.included)
        excluded = rows("exclude", item.excluded)
        prompt = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in item.messages)
        timeline_rows = "".join(
            f"<tr><td>{_esc(row.get('stage'))}</td><td>{_esc(row.get('start_ms'))}</td>"
            f"<td>{_esc(row.get('duration_ms'))}</td><td><pre>{_esc(row.get('detail'))}</pre></td></tr>"
            for row in item.timeline
        )
        timeline = (
            "<h2>Turn timeline</h2><table><tr><th>stage</th><th>start ms</th>"
            "<th>duration ms</th><th>detail</th></tr>" + timeline_rows + "</table>"
            if timeline_rows
            else "<h2>Turn timeline</h2><p class='muted'>none</p>"
        )
        envelope = (item.validation or {}).get("envelope")
        envelope_block = (
            f"<h2>Execution envelope</h2><pre>{_esc(envelope)}</pre>" if envelope else ""
        )
        duration = item.duration_ms if item.duration_ms is not None else item.latency_ms
        interaction = (
            f'<a href="/chat?interaction={quote(item.interaction_id)}#msg-{quote(item.interaction_id)}">'
            f"{_esc(item.interaction_id)}</a>"
            if item.interaction_id
            else ""
        )
        return _page(
            f"<h1>{_esc(item.id)}</h1><p>role {_esc(item.role)} task {_esc(item.task_id)} "
            f"interaction {interaction}</p>"
            f"<p class='muted'>duration {_esc(duration)} ms, time to first token "
            f"{_esc(item.time_to_first_token_ms)} ms, tokens {_esc(item.token_counts)}, "
            f"thinking {_esc(item.thinking_produced)}, finish {_esc(item.finish_reason)}</p>"
            f"{timeline}"
            f"{envelope_block}"
            f"<h2>Included / excluded</h2><ul>{included}{excluded}</ul>"
            f"<h2>Prompt</h2><pre>{_esc(prompt)}</pre>"
            f"<h2>Raw output</h2><pre>{_esc(item.raw_output)}</pre>"
            f"<h2>Parsed / validation</h2><pre>{_esc(item.parsed)}\n{_esc(item.validation)}</pre>"
        )

    def _trace_links(detail) -> str:
        if not detail.traces:
            return "<span class='muted'>none</span>"
        bits = []
        for trace in detail.traces:
            validation = trace.validation or {}
            status = "ok" if validation.get("ok") else "invalid"
            if validation.get("error"):
                status = "error"
            bits.append(
                f'<a href="/traces/{_esc(trace.id)}">{_esc(trace.id)}</a>'
                f"<span class='muted'> ({_esc(trace.role)} {_esc(status)})</span>"
            )
        return " ".join(bits)

    def _execution_html(detail) -> str:
        lines = []
        for trace in detail.traces:
            envelope = (trace.validation or {}).get("envelope")
            if not isinstance(envelope, dict):
                continue
            status = str(envelope.get("status") or "")
            css = "ok" if status == "succeeded" else "err"
            category = (
                f" ({_esc(envelope.get('error_category'))})"
                if envelope.get("error_category")
                else ""
            )
            lines.append(
                f'<li><span class="env {css}">{_esc(envelope.get("capability"))}.'
                f'{_esc(envelope.get("operation"))}: {_esc(status)}</span>{category} '
                f'{_esc(envelope.get("message"))} '
                f'<a href="/traces/{_esc(trace.id)}">trace</a></li>'
            )
        if not lines:
            return ""
        return f"<div class='meta'><b>executions</b><ul>{''.join(lines)}</ul></div>"

    def _memory_html(item, detail) -> str:
        memories = "".join(
            f"<li><span class='muted'>{_esc(use.id)} {_esc(use.type)} {_esc(use.scope)}</span> "
            f"{_esc(use.content)} <span class='muted'>({_esc(use.reason)})</span></li>"
            for use in detail.memories
        )
        memories = (
            f"<div class='meta'><b>memories retrieved</b><ul>{memories}</ul></div>"
            if memories
            else ""
        )

        def write_row(write) -> str:
            restores = "".join(
                f'<input type="hidden" name="restore_id" value="{_esc(rid)}">'
                for rid in write.restore_ids
            )
            return (
                f"<li><span class='muted'>{_esc(write.operation)}</span> "
                f"{_esc(write.memory_id)} {_esc(write.message)} "
                '<form method="post" action="/chat/memory/undo" style="display:inline">'
                f'<input type="hidden" name="memory_id" value="{_esc(write.memory_id)}">'
                f'<input type="hidden" name="mode" value="{_esc(write.undo)}">'
                f"{restores}"
                f'<input type="hidden" name="interaction" value="{_esc(item.id)}">'
                "<button>undo</button></form></li>"
            )

        writes = "".join(write_row(item2) for item2 in detail.writes)
        if writes:
            writes = f"<div class='meta'><b>memory writes</b><ul>{writes}</ul></div>"
        return memories + writes

    def _render_interaction(item, detail) -> str:
        reply = (
            _esc(item.assistant_text)
            if item.assistant_text
            else "<span class='muted'>(no reply stored)</span>"
        )
        error = (
            f"<p class='err'>Turn failed: {_esc(detail.error)}</p>"
            if detail.error
            else ""
        )
        return (
            f'<div id="msg-{_esc(item.id)}">'
            f'<div class="msg user"><div class="role">you · {_esc(item.ts)}</div>'
            f'<div class="text">{_esc(item.user_text)}</div></div>'
            f'<div class="msg assistant"><div class="role">assistant · '
            f'{_esc(item.assistant_ts or "")}</div>'
            f'<div class="text">{reply}</div>{error}'
            f"{_execution_html(detail)}{_memory_html(item, detail)}"
            f'<div class="meta">traces: {_trace_links(detail)}</div></div></div>'
        )

    jobs: dict[str, _TurnJob] = {}
    jobs_lock = threading.Lock()

    def _prune_jobs() -> None:
        retention = int(harness.settings.get("ui.page_limit"))
        with jobs_lock:
            if len(jobs) <= retention:
                return
            for key, job in list(jobs.items()):
                if job.done:
                    jobs.pop(key, None)
                if len(jobs) <= retention:
                    break

    def _start_turn(text: str) -> str:
        job = _TurnJob()
        job_id = new_id("job")
        with jobs_lock:
            jobs[job_id] = job
        loop = asyncio.get_running_loop()

        def listener(event: TurnEvent) -> None:
            with job.lock:
                index = len(job.events)
                job.events.append(event)
                queues = list(job.subscribers)
            for queue in queues:
                loop.call_soon_threadsafe(queue.put_nowait, (index, event))

        def worker() -> None:
            try:
                harness.chat.submit(text, listener)
            except Exception as exc:
                event = TurnEvent(
                    kind="turn.failed",
                    data={"error": f"{type(exc).__name__}: {exc}", "interaction_id": ""},
                )
                with job.lock:
                    index = len(job.events)
                    job.events.append(event)
                    queues = list(job.subscribers)
                for queue in queues:
                    loop.call_soon_threadsafe(queue.put_nowait, (index, event))
            finally:
                with job.lock:
                    job.done = True
                    queues = list(job.subscribers)
                for queue in queues:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True, name=f"turn-{job_id}").start()
        _prune_jobs()
        return job_id

    async def chat(request: Request) -> HTMLResponse:
        focus = request.query_params.get("interaction") or ""
        limit = int(harness.settings.get("ui.page_limit"))
        items = harness.sessions.list_interactions(limit=limit)
        if focus and all(item.id != focus for item in items):
            extra = harness.sessions.get_interaction(focus)
            if extra is not None:
                items = [extra, *items]
        details = harness.chat.detail_many([item.id for item in items])
        blocks = "".join(
            _render_interaction(item, details[item.id]) for item in items
        )
        if not blocks:
            blocks = "<p class='muted' data-empty>No messages yet.</p>"
        focus_note = (
            f"<p><a href='/chat'>show recent only</a> · focused on {_esc(focus)}</p>"
            if focus
            else ""
        )
        active_id = harness.sessions.active_domain()
        active_name = None
        if active_id:
            try:
                active_name = harness.domains.get_domain(active_id).name
            except KeyError:
                active_id = None
        domain_options = "".join(
            f"<option value='{_esc(item.id)}'"
            f"{' selected' if item.id == active_id else ''}>{_esc(item.name)}</option>"
            for item in harness.domains.list_domains()
        )
        domain_bar = (
            "<div class='chat-domain'>"
            f"<span class='muted'>Active domain: {_esc(active_name) if active_name else 'none'}</span> "
            "<form method='post' action='/chat/domain' style='display:inline'>"
            "<select name='domain_id'><option value=''>none</option>"
            f"{domain_options}</select><button>switch</button></form></div>"
        )
        stream_tokens = bool(harness.settings.get("ui.chat_stream_tokens"))
        return _page(
            "<div class='chat-panel'>"
            "<div class='chat-head'><h1>Chat</h1>"
            f"{domain_bar}"
            "<p class='muted'>One turn service shared with <code>harness turn</code>. "
            "Stage progress and raw model tokens stream while the turn runs.</p>"
            f"{focus_note}</div>"
            f"<div id='chat-log'>{blocks}</div>"
            "<details id='live' hidden><summary>Live turn · "
            "<span id='live-status' class='muted'>starting...</span></summary>"
            "<ol id='live-stages'></ol><h3>Raw model stream</h3>"
            "<pre id='live-tokens'></pre><p id='live-error' class='err'></p></details>"
            "<div id='chat-composer'><form id='chat-form'>"
            "<textarea id='chat-text' placeholder='message'></textarea>"
            "<p><button id='chat-send'>send</button></p></form></div></div>"
            + _CHAT_JS.replace("__STREAM_TOKENS__", "true" if stream_tokens else "false"),
            body_class="chat-page",
            main_class="chat-main",
        )

    async def chat_interaction(request: Request) -> Response:
        interaction_id = request.path_params["interaction_id"]
        item = harness.sessions.get_interaction(interaction_id)
        if item is None:
            return _error_page("interaction not found", 404)
        detail = harness.chat.detail(interaction_id)
        return HTMLResponse(_render_interaction(item, detail))

    async def chat_domain(request: Request) -> Response:
        form = _form(await request.body())
        domain_id = (form.get("domain_id") or "").strip()
        if not domain_id:
            harness.sessions.set_active_domain(None)
            return RedirectResponse("/chat", status_code=303)
        try:
            domain = harness.domains.get_domain(domain_id)
        except KeyError:
            return _error_page("domain not found", 404)
        if domain.archived:
            return _error_page("domain is archived", 400)
        harness.sessions.set_active_domain(domain.id)
        return RedirectResponse("/chat", status_code=303)

    async def chat_turn(request: Request) -> JSONResponse:
        form = _form(await request.body())
        text = (form.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "message text is required"}, status_code=400)
        return JSONResponse({"job_id": _start_turn(text), "status": "started"}, status_code=202)

    async def chat_events(request: Request) -> Response:
        with jobs_lock:
            job = jobs.get(request.path_params["job_id"])
        if job is None:
            return _error_page("turn job not found", 404)
        last = request.headers.get("last-event-id")
        start = 0
        if last:
            try:
                start = int(last) + 1
            except ValueError:
                start = 0
        queue: asyncio.Queue = asyncio.Queue()
        with job.lock:
            backlog = list(enumerate(job.events))
            done = job.done
            if not done:
                job.subscribers.append(queue)

        async def stream():
            try:
                for index, event in backlog:
                    if index >= start:
                        yield _sse(index, event)
                if done:
                    return
                heartbeat = float(harness.settings.get("ui.sse_heartbeat_s"))
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        return
                    index, event = item
                    yield _sse(index, event)
            finally:
                with job.lock:
                    if queue in job.subscribers:
                        job.subscribers.remove(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def chat_memory_undo(request: Request) -> Response:
        values = _form_all(await request.body())
        memory_id = (values.get("memory_id") or [""])[0]
        interaction_id = (values.get("interaction") or [""])[0]
        mode = (values.get("mode") or ["retract"])[0]
        if not memory_id:
            return _error_page("missing memory id", 400)
        if mode not in {"retract", "restore"}:
            return _error_page(f"unknown undo mode: {mode}", 400)
        try:
            if mode == "restore":
                harness.memory.restore(memory_id, "user")
            else:
                harness.memory.retract(memory_id, "user")
        except KeyError as exc:
            return _error_page(f"memory not found: {exc}", 404)
        except ValueError as exc:
            return _error_page(str(exc), 400)
        for restore_id in values.get("restore_id") or []:
            try:
                harness.memory.restore(restore_id, "user")
            except KeyError:
                # The superseded record may already be gone; the retraction is
                # still the important part of the undo.
                continue
        if interaction_id:
            target = f"/chat?interaction={quote(interaction_id)}#msg-{quote(interaction_id)}"
        else:
            target = "/chat"
        return RedirectResponse(target, status_code=303)

    async def memory(request: Request) -> HTMLResponse:
        if request.method == "POST":
            form = _form(await request.body())
            action = form.get("action", "")
            mem_id = form.get("id", "")
            try:
                if action == "retract":
                    if not mem_id:
                        return _error_page("missing memory id", 400)
                    harness.memory.retract(mem_id, "user")
                elif action == "rollback_run":
                    run_id = form.get("run_id", "")
                    if not run_id:
                        return _error_page("missing run id", 400)
                    harness.consolidation.rollback(run_id, "user")
                else:
                    return _error_page(f"unknown memory action: {action}", 400)
            except KeyError as exc:
                return _error_page(f"memory not found: {exc}", 404)
            except ValueError as exc:
                return _error_page(str(exc), 400)
            return RedirectResponse("/memory", status_code=303)
        rows = []
        for item in harness.memory.list():
            actions = ""
            if item.status == "active":
                actions = '<button name="action" value="retract">retract</button>'
            rows.append(
                f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.type)}</td><td>{_esc(item.origin)}</td>"
                f"<td>{_esc(item.status)}</td><td>{_esc(item.scope)}</td><td>{_esc(item.content)}</td>"
                f"<td>{_esc(item.hint_notes)}</td><td>{_esc(item.provenance)}</td>"
                f'<td><form method="post"><input type="hidden" name="id" value="{_esc(item.id)}">'
                f'{actions}</form></td></tr>'
            )
        learned = "".join(
            f'<li>{_esc(item.id)} ({_esc(item.type)}, {_esc(item.scope)}): {_esc(item.content)} '
            f'<form method="post" style="display:inline"><input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="retract">undo</button></form>'
            + (
                f'<form method="post" style="display:inline"><input type="hidden" name="run_id" value="{_esc(item.run_id)}">'
                f'<button name="action" value="rollback_run">undo run</button></form>'
                if item.run_id
                else ""
            )
            + "</li>"
            for item in harness.memory.recently_learned(limit=int(harness.settings.get("memory.recent_limit")))
        )
        return _page(
            "<h1>Memory</h1><h2>Recently learned</h2><ul>"
            + (learned or "<li class='muted'>none</li>")
            + "</ul><h2>All records</h2>"
            "<table><tr><th>id</th><th>type</th><th>origin</th><th>status</th><th>scope</th>"
            "<th>content</th><th>hint notes</th><th>provenance</th><th></th></tr>"
            + "".join(rows)
            + "</table>"
        )

    async def persona(request: Request) -> HTMLResponse:
        if request.method == "POST":
            form = _form(await request.body())
            action = form.get("action", "")
            ident = form.get("id", "")
            if action == "approve":
                harness.profile.approve(ident, "user")
            elif action == "reject":
                harness.profile.reject(ident, "user")
            elif action == "edit":
                draft = PersonaDraft(
                    name=form.get("name", ""),
                    purpose=form.get("purpose", ""),
                    communication_style=form.get("communication_style", ""),
                    operating_principles=[
                        line.strip()
                        for line in form.get("operating_principles", "").splitlines()
                        if line.strip()
                    ],
                    reason="user edit",
                )
                proposal = harness.profile.propose_edit(draft, "user")
                harness.profile.approve(proposal.id, "user")
            return RedirectResponse("/persona", status_code=303)
        current = harness.profile.current()
        pending = "".join(
            f'<li>{_esc(item.id)} {_esc(item.draft)} <form method="post" style="display:inline">'
            f'<input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="approve">approve</button>'
            f'<button name="action" value="reject">reject</button></form></li>'
            for item in harness.profile.pending()
        )
        history = "".join(
            f"<li>v{_esc(item.version)} {_esc(item.actor)} {_esc(item.reason)} {_esc(item.ts)}</li>"
            for item in harness.profile.history()
        )
        principles = "\n".join(current.operating_principles)
        return _page(
            f"<h1>Persona v{_esc(current.version)}</h1>"
            "<form method='post'><input type='hidden' name='action' value='edit'>"
            f"<p>Name<br><input name='name' value='{_esc(current.name)}'></p>"
            f"<p>Purpose<br><input name='purpose' value='{_esc(current.purpose)}' style='width:100%'></p>"
            f"<p>Style<br><input name='communication_style' value='{_esc(current.communication_style)}' style='width:100%'></p>"
            f"<p>Principles (one per line)<br><textarea name='operating_principles'>{_esc(principles)}</textarea></p>"
            "<button>save as new version</button></form>"
            f"<h2>Pending proposals</h2><ul>{pending}</ul>"
            f"<h2>Version history</h2><ul>{history}</ul>"
        )

    async def tasks(_request: Request) -> HTMLResponse:
        rows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.state)}</td><td>{_esc(item.block_reason)}</td>"
            f"<td>{_esc(item.priority)}</td><td>{_esc(item.due_at or '')}</td>"
            f"<td>{_esc(item.defer_until or '')}</td>"
            f"<td>{_esc(item.domain_id)}</td><td>{_esc(item.goal)}</td>"
            f"<td>{_esc(item.depends_on)}</td><td>{_esc(item.outcome)}</td>"
            f"<td>{_esc(item.last_message)}</td></tr>"
            for item in harness.tasks.list()
        )
        work = harness.scheduler.report()
        return _page(
            "<h1>Tasks</h1>"
            "<h2>Work status</h2><pre>"
            + _esc(work.summary())
            + "</pre>"
            "<h2>All tasks</h2><table><tr><th>id</th><th>state</th><th>reason</th><th>prio</th>"
            "<th>due</th><th>deferred until</th><th>domain</th>"
            "<th>goal</th><th>deps</th><th>outcome</th><th>last message</th></tr>"
            + rows
            + "</table>"
        )

    async def domains(request: Request) -> HTMLResponse:
        if request.method == "POST":
            form = _form(await request.body())
            action = form.get("action", "")
            ident = form.get("id", "")
            if action == "confirm":
                harness.domains.confirm_suggestion(ident)
            elif action == "decline":
                harness.domains.decline_suggestion(ident)
            elif action == "rename":
                harness.domains.rename(ident, form.get("name", ""))
            elif action == "archive":
                harness.domains.archive(ident)
            return RedirectResponse("/domains", status_code=303)

        def actions(ident: str, name: str) -> str:
            return (
                f'<form method="post"><input type="hidden" name="id" value="{_esc(ident)}">'
                f'<input name="name" value="{_esc(name)}">'
                f'<button name="action" value="rename">rename</button>'
                f'<button name="action" value="archive">archive</button></form>'
            )

        drows = "".join(
            f'<tr><td><a href="/domains/{_esc(item.id)}">{_esc(item.id)}</a></td>'
            f'<td><a href="/domains/{_esc(item.id)}">{_esc(item.name)}</a></td>'
            f"<td>{_esc(item.created_by)}</td>"
            f"<td>{_esc(item.created_reason)}</td><td>{_esc(item.capability_names)}</td>"
            f"<td>{actions(item.id, item.name)}</td></tr>"
            for item in harness.domains.list_domains(include_archived=True)
        )
        srows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.status)}</td><td>{_esc(item.draft)}</td>"
            f'<td><form method="post"><input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="confirm">confirm</button>'
            f'<button name="action" value="decline">decline</button></form></td></tr>'
            for item in harness.domains.list_suggestions()
        )
        return _page(
            "<h1>Domains</h1><table><tr><th>id</th><th>name</th><th>by</th><th>why</th><th>caps</th>"
            "<th>actions</th></tr>"
            + drows
            + "</table><h2>Suggestions</h2><table><tr><th>id</th><th>status</th><th>draft</th><th></th></tr>"
            + srows
            + "</table>"
        )

    async def domain_detail(request: Request) -> HTMLResponse:
        domain_id = request.path_params["domain_id"]
        try:
            content = inspect_domain(harness, domain_id)
        except KeyError:
            return _error_page(f"domain not found: {domain_id}", 404)
        meta = content["metadata"]
        counts = content["counts"]
        memory_rows = "".join(
            f"<tr><td>{_esc(item['id'])}</td><td>{_esc(item['type'])}</td>"
            f"<td>{_esc(item['scope'])}</td><td>{_esc(item['status'])}</td>"
            f"<td>{_esc(item['content'])}</td></tr>"
            for group in ("domain_scoped", "global", "self")
            for item in content["memories"][group]
        )
        task_rows = "".join(
            f"<tr><td>{_esc(item['id'])}</td><td>{_esc(item['state'])}</td>"
            f"<td>{_esc(item['goal'])}</td><td>{_esc(item['due_at'])}</td>"
            f"<td>{_esc(item['updated_at'])}</td></tr>"
            for item in content["tasks"]
        )
        decision_rows = "".join(
            f"<tr><td>{_esc(item['id'])}</td><td>{_esc(item['status'])}</td>"
            f"<td>{_esc(item['summary'])}</td><td>{_esc(item['resolution'])}</td></tr>"
            for item in content["decisions"]
        )
        event_rows = "".join(
            f"<tr><td>{_esc(item['ts'])}</td><td>{_esc(item['operation'])}</td>"
            f"<td>{_esc(item['result'])}</td><td>{_esc(item['data'])}</td></tr>"
            for item in content["events"]
        )
        empty_note = (
            "<p class='muted'>This domain has no stored memories, tasks, or decisions. "
            "Nothing is inferred to make it look populated.</p>"
            if content["empty"]
            else ""
        )
        return _page(
            f"<h1>Domain: {_esc(meta['name'])}</h1>"
            f"<p class='muted'><a href='/api/domains/{_esc(meta['id'])}'>JSON</a></p>"
            f"{empty_note}"
            "<h2>Metadata</h2><table>"
            f"<tr><th>id</th><td>{_esc(meta['id'])}</td></tr>"
            f"<tr><th>name</th><td>{_esc(meta['name'])}</td></tr>"
            f"<tr><th>status</th><td>{_esc(meta['status'])}</td></tr>"
            f"<tr><th>created by</th><td>{_esc(meta['created_by'])}</td></tr>"
            f"<tr><th>created at</th><td>{_esc(meta['created_at'])}</td></tr>"
            f"<tr><th>reason</th><td>{_esc(meta['created_reason'])}</td></tr>"
            f"<tr><th>capabilities</th><td>{_esc(meta['capability_names'])}</td></tr>"
            "</table>"
            f"<h2>Summary</h2><pre>{_esc(content['summary'] or 'No summary stored.')}</pre>"
            f"<h2>Memory ({counts['domain_memories']} scoped, {counts['global_memories']} global, "
            f"{counts['self_memories']} self)</h2>"
            "<table><tr><th>id</th><th>type</th><th>scope</th><th>status</th><th>content</th></tr>"
            f"{memory_rows}</table>"
            f"<h2>Tasks ({counts['tasks']})</h2>"
            "<table><tr><th>id</th><th>state</th><th>goal</th><th>due</th><th>updated</th></tr>"
            f"{task_rows}</table>"
            f"<h2>Decisions ({counts['decisions']})</h2>"
            "<table><tr><th>id</th><th>status</th><th>summary</th><th>resolution</th></tr>"
            f"{decision_rows}</table>"
            f"<h2>Events ({counts['events']})</h2>"
            "<table><tr><th>ts</th><th>operation</th><th>result</th><th>data</th></tr>"
            f"{event_rows}</table>"
        )

    async def domain_api(request: Request) -> JSONResponse:
        try:
            content = inspect_domain(harness, request.path_params["domain_id"])
        except KeyError:
            return JSONResponse({"error": "domain not found"}, status_code=404)
        return JSONResponse(content)

    async def capabilities(_request: Request) -> HTMLResponse:
        rows = []
        for manifest in harness.capabilities.list_manifests():
            ops = ", ".join(f"{op.name}:{op.permission}" for op in manifest.operations)
            rows.append(
                f"<tr><td>{_esc(manifest.name)}</td><td>{_esc(manifest.purpose)}</td>"
                f"<td>{_esc(manifest.source)}</td><td>{_esc(manifest.available)}</td><td>{_esc(ops)}</td></tr>"
            )
        enable = "".join(
            f"<li>{_esc(domain.id)} {_esc(domain.name)}: {_esc(domain.capability_names)}</li>"
            for domain in harness.domains.list_domains()
        )
        return _page(
            "<h1>Capabilities</h1><table><tr><th>name</th><th>purpose</th><th>source</th>"
            "<th>available</th><th>ops</th></tr>"
            + "".join(rows)
            + "</table><h2>Per-domain enablement</h2><ul>"
            + enable
            + "</ul>"
        )

    async def consolidation(request: Request) -> HTMLResponse:
        if request.method == "POST":
            form = _form(await request.body())
            action = form.get("action", "")
            try:
                if action == "run":
                    harness.consolidation.run(trigger="ui")
                elif action == "rollback":
                    run_id = form.get("id", "")
                    if not run_id:
                        return _error_page("missing run id", 400)
                    harness.consolidation.rollback(run_id, "user")
                else:
                    return _error_page(f"unknown consolidation action: {action}", 400)
            except KeyError as exc:
                return _error_page(f"consolidation run not found: {exc}", 404)
            except ValueError as exc:
                return _error_page(str(exc), 400)
            return RedirectResponse("/consolidation", status_code=303)
        rows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.status)}</td><td>{_esc(item.rolled_back)}</td>"
            f"<td>{_esc(item.report)}</td><td>{_esc(item.proposals)}</td><td>{_esc(item.results)}</td>"
            f"<td>{_esc(item.findings)}</td>"
            f'<td><form method="post"><input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="rollback">rollback</button></form></td></tr>'
            for item in harness.consolidation.list()
        )
        return _page(
            '<h1>Consolidation</h1><form method="post"><button name="action" value="run">Run</button></form>'
            "<table><tr><th>id</th><th>status</th><th>rolled back</th><th>report</th><th>proposals</th>"
            "<th>results</th><th>findings</th><th></th></tr>"
            + rows
            + "</table>"
        )

    async def settings_view(request: Request) -> HTMLResponse:
        error = ""
        if request.method == "POST":
            form = _form(await request.body())
            key = form.get("key", "")
            action = form.get("action", "")
            try:
                if action == "reset":
                    harness.settings.reset(key, "user")
                else:
                    harness.settings.set(key, form.get("value", ""), "user")
            except (SettingsError, ValueError) as exc:
                error = str(exc)
            else:
                return RedirectResponse("/settings", status_code=303)
        groups: dict[str, list] = {}
        values = harness.settings.as_dict()
        for item in harness.settings.definitions():
            groups.setdefault(item.group, []).append(item)
        chunks = [f'<p class="err">{_esc(error)}</p>' if error else ""]
        for group, defs in groups.items():
            chunks.append(f"<h2>{_esc(group)}</h2><table>")
            for item in defs:
                history = harness.settings.history(item.key)
                last = f"{history[-1].ts} {history[-1].old_value!r} -> {history[-1].new_value!r}" if history else ""
                hist = "".join(
                    f"<li>{_esc(row.ts)} {_esc(row.actor)} {_esc(row.old_value)} -> {_esc(row.new_value)}</li>"
                    for row in reversed(history[-20:])
                )
                chunks.append(
                    f"<tr><td>{_esc(item.key)}<div class='muted'>{'live' if item.live else 'restart required'}</div></td>"
                    f"<td>{_esc(item.description)}</td><td>{_esc(item.default)}</td>"
                    f"<td>{_esc(values.get(item.key))}</td><td>{_esc(item.minimum)}-{_esc(item.maximum)} "
                    f"{_esc(item.allowed or '')}</td><td>{_esc(last)}"
                    f"<details><summary>history</summary><ul>{hist}</ul></details></td>"
                    f"<td><form method='post'><input type='hidden' name='key' value='{_esc(item.key)}'>"
                    f"<input name='value' value='{_esc(values.get(item.key))}'><button>save</button>"
                    f"<button name='action' value='reset'>reset</button></form></td></tr>"
                )
            chunks.append("</table>")
        return _page("<h1>Settings</h1>" + "".join(chunks))

    async def events(request: Request) -> HTMLResponse:
        limit = int(harness.settings.get("ui.page_limit"))
        items = harness.events.query(limit=limit * 4)
        if request.query_params.get("errors"):
            items = [item for item in items if item.result not in {"ok", "succeeded", "committed", "active"}]
        items = list(reversed(items))[:limit]
        rows = "".join(
            f"<tr><td>{_esc(item.ts)}</td><td>{_esc(item.actor)}</td><td>{_esc(item.operation)}</td>"
            f"<td>{_esc(item.target)}</td><td>{_esc(item.result)}</td><td>{_esc(item.data)}</td></tr>"
            for item in items
        )
        return _page(
            '<h1>Events</h1><p><a href="/events">all</a> | <a href="/events?errors=1">errors only</a></p>'
            "<table><tr><th>ts</th><th>actor</th><th>op</th><th>target</th><th>result</th><th>data</th></tr>"
            + rows
            + "</table>"
        )

    async def history(_request: Request) -> HTMLResponse:
        segments = harness.history.list_segments()
        segment_rows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.started_at)}</td><td>{_esc(item.ended_at)}</td>"
            f"<td>{_esc(len(item.interaction_ids))}</td><td>{_esc(len(item.covered_ids))}</td>"
            f"<td>{_esc(item.status)}</td><td>{_esc(item.summary)}</td><td>{_esc(item.error)}</td></tr>"
            for item in segments
        )
        interactions = harness.sessions.list_interactions(limit=None)
        interaction_rows = "".join(
            f"<tr><td>{_esc(item.ts)}</td><td>{_esc(item.assistant_ts)}</td><td>{_esc(item.session_id)}</td>"
            f"<td>{_esc(item.user_text)}</td><td>{_esc(item.assistant_text)}</td></tr>"
            for item in reversed(interactions)
        )
        return _page(
            f"<h1>Conversation history</h1><p class='muted'>Strategy "
            f"{_esc(harness.settings.get('context.history.strategy'))}; budget "
            f"{_esc(harness.settings.get('context.budgets.history'))} tokens. Everything here is "
            "stored; the prompt view may include less.</p>"
            "<h2>Segments and summaries</h2><table><tr><th>id</th><th>started</th><th>ended</th>"
            "<th>turns</th><th>summarized</th><th>status</th><th>summary</th><th>error</th></tr>"
            + segment_rows
            + "</table><h2>Stored messages</h2><table><tr><th>user ts</th><th>assistant ts</th>"
            "<th>session</th><th>user</th><th>assistant</th></tr>"
            + interaction_rows
            + "</table>"
        )

    async def learning(_request: Request) -> HTMLResponse:
        report = learning_report(harness)
        rows = "".join(f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>" for key, value in report.items())
        return _page("<h1>Learning measurements</h1><table>" + rows + "</table>")

    def _reset_result(report) -> HTMLResponse:
        if report.removed:
            removed = "".join(
                f"<tr><td>{_esc(item.path)}</td><td>{_esc(item.files)}</td><td>{_esc(item.bytes)}</td></tr>"
                for item in report.removed
            )
            removed_block = (
                "<table><tr><th>path</th><th>files</th><th>bytes</th></tr>"
                + removed
                + "</table>"
            )
        else:
            removed_block = "<p class='muted'>Nothing was present for this scope.</p>"
        backup = (
            f"<p>Backup: <code>{_esc(report.backup_path)}</code></p>"
            if report.backup_path
            else "<p class='muted'>Backup disabled.</p>"
        )
        seeded = ", ".join(report.seeded) if report.seeded else "nothing"
        return _page(
            f"<h1>Reset complete: {_esc(report.scope)}</h1>"
            f"{backup}<p>Seeded: {_esc(seeded)}</p>"
            f"<p>Stale references cleared: {_esc(report.cleared_references)}. "
            f"Duration: {_esc(report.duration_ms)} ms.</p>"
            f"<h2>Removed</h2>{removed_block}"
            "<p><a href='/reset'>reset again</a> · <a href='/chat'>chat</a></p>"
        )

    async def reset_view(request: Request) -> HTMLResponse:
        word = str(harness.settings.get("maintenance.reset.confirmation_word") or "").strip()
        if not word:
            return _error_page(
                "reset confirmation word is empty; set maintenance.reset.confirmation_word first",
                400,
            )
        if request.method == "POST":
            form = _form(await request.body())
            scope = form.get("scope", "full")
            if scope not in harness.reset.scopes():
                return _error_page(f"unknown reset scope: {scope}", 400)
            if form.get("confirm", "") != word:
                return _error_page("confirmation word does not match; nothing was removed", 400)
            backup = form.get("backup") == "on"
            try:
                report = await asyncio.to_thread(
                    harness.reset.run,
                    scope,
                    actor="ui",
                    backup=backup,
                )
            except ValueError as exc:
                return _error_page(str(exc), 400)
            with jobs_lock:
                jobs.clear()
            return _reset_result(report)

        scopes = "".join(
            f"<li><label><input type='radio' name='scope' value='{_esc(scope)}'"
            f"{' checked' if scope == 'full' else ''}> {_esc(scope)}</label></li>"
            for scope in harness.reset.scopes()
        )
        backup_checked = " checked" if bool(harness.settings.get("maintenance.reset.backup")) else ""
        return _page(
            "<h1>Reset</h1>"
            f"<p class='muted'>Data directory: <code>{_esc(harness.data_dir)}</code>. "
            "A full reset also restores default settings, the code-defined persona, "
            "and the reserved self domain.</p>"
            "<form method='post'>"
            f"<h2>Scope</h2><ul>{scopes}</ul>"
            f"<p><label><input type='checkbox' name='backup' value='on'{backup_checked}> "
            "write a timestamped backup archive first</label></p>"
            f"<p>Type <b>{_esc(word)}</b> to confirm:<br>"
            "<input name='confirm' autocomplete='off'></p>"
            "<p><button>reset</button></p></form>"
            "<p class='err'>This cannot be undone without the backup archive.</p>"
        )

    async def on_startup() -> None:
        # The persistent heartbeat owns all scheduled activity while the app is
        # serving: task recovery, due reminders, deferred releases, and the
        # periodic consolidation pass (consolidation.schedule_enabled).
        harness.scheduler.start()

    async def on_shutdown() -> None:
        harness.scheduler.stop()

    routes = [
        Route("/health", health),
        Route("/chat", chat),
        Route("/chat/domain", chat_domain, methods=["POST"]),
        Route("/chat/interactions/{interaction_id}", chat_interaction),
        Route("/chat/turn", chat_turn, methods=["POST"]),
        Route("/chat/turns/{job_id}/events", chat_events),
        Route("/chat/memory/undo", chat_memory_undo, methods=["POST"]),
        Route("/", traces),
        Route("/traces/{trace_id}", trace_detail),
        Route("/memory", memory, methods=["GET", "POST"]),
        Route("/persona", persona, methods=["GET", "POST"]),
        Route("/tasks", tasks),
        Route("/domains", domains, methods=["GET", "POST"]),
        Route("/domains/{domain_id}", domain_detail),
        Route("/api/domains/{domain_id}", domain_api),
        Route("/history", history),
        Route("/capabilities", capabilities),
        Route("/consolidation", consolidation, methods=["GET", "POST"]),
        Route("/settings", settings_view, methods=["GET", "POST"]),
        Route("/events", events),
        Route("/learning", learning),
        Route("/reset", reset_view, methods=["GET", "POST"]),
    ]
    app = Starlette(routes=routes, on_startup=[on_startup], on_shutdown=[on_shutdown])
    app.middleware("http")(guard)
    return app
