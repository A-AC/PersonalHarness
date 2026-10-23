from __future__ import annotations

from html import escape
from urllib.parse import parse_qs

from jinja2 import Template
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from harness.app import Harness
from harness.contracts.profile import PersonaDraft
from harness.contracts.settings import SettingsError
from harness.measure import learning_report


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
.muted{color:#999}
textarea{width:100%;min-height:5em;background:#1b1b1b;color:#eee}
input{background:#1b1b1b;color:#eee}
</style></head>
<body>
<nav>
<a href="/">traces</a><a href="/memory">memory</a><a href="/persona">persona</a>
<a href="/tasks">tasks</a><a href="/domains">domains</a><a href="/capabilities">capabilities</a>
<a href="/consolidation">consolidation</a><a href="/settings">settings</a>
<a href="/events">events</a><a href="/learning">learning</a>
</nav>
<main>{{ body|safe }}</main></body></html>"""
)


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(LAYOUT.render(body=body))


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


def create_app(harness: Harness) -> Starlette:
    async def guard(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not _authorized(request, harness):
            return Response("unauthorized", status_code=401)
        response = await call_next(request)
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
            f"<td>{_esc(item.task_id)}</td><td>{_esc(item.validation)}</td>"
            f"<td>{_esc(item.finish_reason)}</td><td>{_esc(item.ts)}</td></tr>"
            for item in reversed(items)
        )
        return _page(
            "<h1>Traces</h1><table><tr><th>id</th><th>task</th><th>validation</th>"
            "<th>finish</th><th>ts</th></tr>"
            + rows
            + "</table>"
        )

    async def trace_detail(request: Request) -> HTMLResponse:
        item = harness.traces.get(request.path_params["trace_id"])
        if item is None:
            return _page("<p>missing</p>")
        included = "".join(f"<li>include {_esc(row)}</li>" for row in item.included)
        excluded = "".join(f"<li>exclude {_esc(row)}</li>" for row in item.excluded)
        prompt = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in item.messages)
        return _page(
            f"<h1>{_esc(item.id)}</h1><p>task {_esc(item.task_id)} interaction {_esc(item.interaction_id)}</p>"
            f"<p class='muted'>latency {_esc(item.latency_ms)} ms, tokens {_esc(item.token_counts)}, "
            f"finish {_esc(item.finish_reason)}</p>"
            f"<h2>Included / excluded</h2><ul>{included}{excluded}</ul>"
            f"<h2>Prompt</h2><pre>{_esc(prompt)}</pre>"
            f"<h2>Raw output</h2><pre>{_esc(item.raw_output)}</pre>"
            f"<h2>Parsed / validation</h2><pre>{_esc(item.parsed)}\n{_esc(item.validation)}</pre>"
        )

    async def memory(request: Request) -> HTMLResponse:
        if request.method == "POST":
            form = _form(await request.body())
            action = form.get("action", "")
            mem_id = form.get("id", "")
            if action == "retract":
                harness.memory.retract(mem_id, "user")
            elif action == "approve":
                harness.memory.approve(mem_id, "user")
            elif action == "reject":
                harness.memory.reject(mem_id, "user")
            return RedirectResponse("/memory", status_code=303)
        rows = []
        for item in harness.memory.list():
            rows.append(
                f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.type)}</td><td>{_esc(item.origin)}</td>"
                f"<td>{_esc(item.status)}</td><td>{_esc(item.scope)}</td><td>{_esc(item.content)}</td>"
                f"<td>{_esc(item.provenance)}</td>"
                f'<td><form method="post"><input type="hidden" name="id" value="{_esc(item.id)}">'
                f'<button name="action" value="retract">retract</button>'
                f'<button name="action" value="approve">approve</button>'
                f'<button name="action" value="reject">reject</button></form></td></tr>'
            )
        learned = "".join(
            f'<li>{_esc(item.id)} ({_esc(item.type)}, {_esc(item.scope)}): {_esc(item.content)} '
            f'<form method="post" style="display:inline"><input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="retract">retract</button></form></li>'
            for item in harness.memory.recently_learned(limit=int(harness.settings.get("memory.recent_limit")))
        )
        pending = "".join(
            f"<li>{_esc(item.id)}: {_esc(item.content)} ({_esc(item.provenance)})</li>"
            for item in harness.memory.list(status="proposed")
        )
        return _page(
            "<h1>Memory</h1><h2>Awaiting approval</h2><ul>"
            + pending
            + "</ul><h2>Recently learned</h2><ul>"
            + learned
            + "</ul><h2>All records</h2>"
            "<table><tr><th>id</th><th>type</th><th>origin</th><th>status</th><th>scope</th>"
            "<th>content</th><th>provenance</th><th></th></tr>"
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
            f"<td>{_esc(item.domain_id)}</td><td>{_esc(item.project_id)}</td><td>{_esc(item.goal)}</td>"
            f"<td>{_esc(item.depends_on)}</td><td>{_esc(item.outcome)}</td>"
            f"<td>{_esc(item.last_message)}</td></tr>"
            for item in harness.tasks.list()
        )
        return _page(
            "<h1>Tasks</h1><table><tr><th>id</th><th>state</th><th>reason</th><th>domain</th>"
            "<th>project</th><th>goal</th><th>deps</th><th>outcome</th><th>last message</th></tr>"
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
                harness.domains.rename(form.get("kind", "domain"), ident, form.get("name", ""))
            elif action == "archive":
                harness.domains.archive(form.get("kind", "domain"), ident)
            return RedirectResponse("/domains", status_code=303)

        def actions(kind: str, ident: str, name: str) -> str:
            return (
                f'<form method="post"><input type="hidden" name="kind" value="{_esc(kind)}">'
                f'<input type="hidden" name="id" value="{_esc(ident)}">'
                f'<input name="name" value="{_esc(name)}">'
                f'<button name="action" value="rename">rename</button>'
                f'<button name="action" value="archive">archive</button></form>'
            )

        drows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.name)}</td><td>{_esc(item.created_by)}</td>"
            f"<td>{_esc(item.created_reason)}</td><td>{_esc(item.capability_names)}</td>"
            f"<td>{_esc(item.memory_scope_id)}</td><td>{actions('domain', item.id, item.name)}</td></tr>"
            for item in harness.domains.list_domains(include_archived=True)
        )
        prows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.name)}</td><td>{_esc(item.domain_id)}</td>"
            f"<td>{_esc(item.created_reason)}</td><td>{actions('project', item.id, item.name)}</td></tr>"
            for item in harness.domains.list_projects(include_archived=True)
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
            "<th>memory scope</th><th>actions</th></tr>"
            + drows
            + "</table><h2>Projects</h2><table><tr><th>id</th><th>name</th><th>domain</th>"
            "<th>why</th><th>actions</th></tr>"
            + prows
            + "</table><h2>Suggestions</h2><table><tr><th>id</th><th>status</th><th>draft</th><th></th></tr>"
            + srows
            + "</table>"
        )

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
            if action == "run":
                harness.consolidation.run(trigger="ui")
            elif action == "rollback":
                harness.consolidation.rollback(form.get("id", ""), "user")
            return RedirectResponse("/consolidation", status_code=303)
        rows = "".join(
            f"<tr><td>{_esc(item.id)}</td><td>{_esc(item.status)}</td><td>{_esc(item.rolled_back)}</td>"
            f"<td>{_esc(item.proposals)}</td><td>{_esc(item.results)}</td><td>{_esc(item.findings)}</td>"
            f'<td><form method="post"><input type="hidden" name="id" value="{_esc(item.id)}">'
            f'<button name="action" value="rollback">rollback</button></form></td></tr>'
            for item in harness.consolidation.list()
        )
        return _page(
            '<h1>Consolidation</h1><form method="post"><button name="action" value="run">Run</button></form>'
            "<table><tr><th>id</th><th>status</th><th>rolled back</th><th>proposals</th>"
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

    async def learning(_request: Request) -> HTMLResponse:
        report = learning_report(harness)
        rows = "".join(f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>" for key, value in report.items())
        return _page("<h1>Learning measurements</h1><table>" + rows + "</table>")

    routes = [
        Route("/health", health),
        Route("/", traces),
        Route("/traces/{trace_id}", trace_detail),
        Route("/memory", memory, methods=["GET", "POST"]),
        Route("/persona", persona, methods=["GET", "POST"]),
        Route("/tasks", tasks),
        Route("/domains", domains, methods=["GET", "POST"]),
        Route("/capabilities", capabilities),
        Route("/consolidation", consolidation, methods=["GET", "POST"]),
        Route("/settings", settings_view, methods=["GET", "POST"]),
        Route("/events", events),
        Route("/learning", learning),
    ]
    app = Starlette(routes=routes)
    app.middleware("http")(guard)
    return app
