"""Phase 6 UI contract tests: one test per panel endpoint.

The zero-build UI polls the active panel instead of the monolithic
``/api/state`` payload. These tests pin the response contract each panel
renderer relies on, plus the small mutations the UI exposes (edit/retract a
memory, draft/delete a skill, bind a module to a domain). Keep ``/api/state``
covered by the CLI/server tests; it remains a compatibility aggregate.
"""

from __future__ import annotations

from harness import util


def _manual_run(app, task, status="running"):
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at, transcript)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", status, util.iso(),
         util.dumps([{"role": "user", "content": "go"}])),
    )
    return run_id


# -- Conversation -----------------------------------------------------------


def test_summary_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    response = http.get("/api/summary")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["counts"]) == {
        "open_tasks", "paused", "pending_approvals", "domains", "inbox_new",
        "memories", "archived_memories", "skills", "modules",
    }
    assert payload["working_task"] is None
    assert payload["persona"] == app.config.get("persona", {})
    assert isinstance(payload["domains"], list)
    assert set(payload["scheduler"]) == {"last_tick", "last_report", "enabled"}
    assert payload["now"]
    # app.js formats timestamps in this timezone; the test_time.py source
    # assertion also guards the identifier.
    assert payload["system_timezone"] == util.system_tz_name()


def test_messages_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    sent = http.post("/api/message", json={"text": "hello panel"})
    assert sent.status_code == 200

    response = http.get("/api/messages")
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hello panel"
    assert messages[1]["content"] == "ok"


# -- Work -------------------------------------------------------------------


def test_tasks_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    result = app.runtime.submit_user_message("panel task")
    task = app.tasks.get(result.task_id)
    http = client(app)

    listing = http.get("/api/tasks").json()
    assert [t["id"] for t in listing["tasks"]] == [task["id"]]
    assert listing["counts"]["open_tasks"] == 0
    assert listing["counts"]["subagents"] == 0

    detail = http.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["id"] == task["id"]
    assert {"children", "executions", "approvals"} <= set(detail)
    assert len(detail["runs"]) == 1
    run = detail["runs"][0]
    # The detail drawer gets the exact persisted model request, not a summary.
    assert run["transcript"][0]["role"] == "system"
    assert "Operating rules" in run["transcript"][0]["content"]
    assert run["transcript"][1] == {"role": "user", "content": "panel task"}
    assert run["context_sources"]
    assert isinstance(run["toolset"]["names"], list)


def test_approvals_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    task = app.tasks.create(objective="needs approval", origin="user")
    run_id = _manual_run(app, task, status="waiting_approval")
    approval = app.approvals.create(
        task_id=task["id"], run_id=run_id, action="tool",
        tool="mcp_test_send", arguments={"text": "x"}, risk="confirm",
    )
    http = client(app)

    payload = http.get("/api/approvals").json()
    assert [a["id"] for a in payload["approvals"]] == [approval["id"]]
    assert payload["counts"]["pending_approvals"] == 1
    assert payload["approvals"][0]["tool"] == "mcp_test_send"


def test_schedules_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/schedules", json={
        "name": "panel schedule", "prompt": "wake", "kind": "once",
        "at_ts": "2031-06-15T09:30:00+02:00",
    })
    assert created.status_code == 200
    schedule = created.json()["schedule"]

    payload = http.get("/api/schedules").json()
    assert [s["id"] for s in payload["schedules"]] == [schedule["id"]]
    assert payload["schedules"][0]["name"] == "panel schedule"


def test_inbox_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/inbox", json={
        "source": "chat", "external_id": "panel-1", "title": "Panel ping",
    })
    assert created.status_code == 200

    payload = http.get("/api/inbox").json()
    assert [event["title"] for event in payload["events"]] == ["Panel ping"]
    assert payload["counts"]["new"] == 1
    assert payload["automations"] == []
    assert payload["fires"] == []


def test_automations_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/automations", json={
        "name": "panel rule", "source": "mail", "action": "notify",
    })
    assert created.status_code == 200

    payload = http.get("/api/automations").json()
    assert [rule["name"] for rule in payload["automations"]] == ["panel rule"]
    assert payload["fires"] == []


# -- Knowledge --------------------------------------------------------------


def test_memory_panel_contract_and_mutations(make_app, client):
    app = make_app.build(default="ok")
    memory = app.memory.remember(
        "Panel memory text.", kind="fact", scope="global", origin="user",
    )["memory"]
    http = client(app)

    listing = http.get("/api/memories?status=any").json()
    assert memory["id"] in {item["id"] for item in listing["memories"]}
    assert listing["filter"] == "any"
    assert listing["counts"]["active"] == 1
    assert "fact" in listing["kinds"]
    assert isinstance(listing["artifact_problems"], list)

    edited = http.patch(f"/api/memories/{memory['id']}", json={
        "content": "Edited panel memory.", "kind": "preference",
        "reason": "contract test",
    })
    assert edited.status_code == 200
    assert edited.json()["memory"]["content"] == "Edited panel memory."
    assert edited.json()["memory"]["kind"] == "preference"
    detail = http.get(f"/api/memories/{memory['id']}").json()
    assert len(detail["revisions"]) >= 1

    retracted = http.delete(f"/api/memories/{memory['id']}")
    assert retracted.status_code == 200
    assert retracted.json()["memory"]["status"] == "retracted"
    assert http.get(f"/api/memories/{memory['id']}").json()[
        "memory"
    ]["status"] == "retracted"


def test_domains_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/domains", json={
        "name": "Panel Domain", "description": "from the UI",
    })
    assert created.status_code == 200
    domain = created.json()["domain"]

    domains = http.get("/api/domains").json()["domains"]
    assert domain["id"] in {d["id"] for d in domains}
    assert domain["name"] == "Panel Domain"

    updated = http.patch(f"/api/domains/{domain['id']}", json={
        "description": "edited in the UI",
    })
    assert updated.status_code == 200
    assert updated.json()["domain"]["description"] == "edited in the UI"


def test_skills_panel_contract_crud(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)

    created = http.post("/api/skills", json={
        "name": "panel skill", "description": "made in the UI",
        "preconditions": ["ui"], "body": "Do the panel thing.",
    })
    assert created.status_code == 200
    skill = created.json()["skill"]
    assert skill["status"] == "draft"

    listing = http.get("/api/skills").json()
    assert [s["id"] for s in listing["skills"]] == [skill["id"]]
    assert listing["enabled"] is True
    assert listing["max_body_chars"] > 0

    detail = http.get(f"/api/skills/{skill['id']}").json()
    assert detail["skill"]["name"] == "panel skill"
    assert detail["body"].strip() == "Do the panel thing."

    # Active skills cannot be deleted: the UI disables first, then deletes.
    activated = http.post(f"/api/modules/{skill['id']}/activate")
    assert activated.status_code == 200
    refused = http.delete(f"/api/skills/{skill['id']}")
    assert refused.status_code == 409

    disabled = http.post(f"/api/modules/{skill['id']}/disable")
    assert disabled.status_code == 200
    deleted = http.delete(f"/api/skills/{skill['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == skill["id"]
    assert http.get(f"/api/skills/{skill['id']}").status_code == 404


def test_consolidation_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    payload = http.get("/api/consolidations").json()
    assert payload["consolidations"] == []
    assert payload["schedule_interval_s"] == app.config.get(
        "consolidation.schedule_interval_s"
    )


# -- System -----------------------------------------------------------------


def test_modules_panel_contract_and_bindings(make_app, client):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        util.new_id("dom"), name="Panel", description="bind target",
        created_by="test",
    )
    http = client(app)

    payload = http.get("/api/modules").json()
    assert payload["schema_budget_tokens"] == 2400
    assert set(payload["schema_budget"]) == {
        "tokens", "budget_tokens", "tool_count", "names", "exclusions",
    }
    web = next(m for m in payload["modules"] if m["id"] == "web")
    assert web["bound_domains"] == []
    assert web["tools"][0]["name"] == "web_search"
    assert "provenance" in web

    bound = http.post("/api/modules/web/bindings", json={
        "domain_id": domain["id"], "enabled": True,
    })
    assert bound.status_code == 200
    assert bound.json()["module"]["bound_domains"] == [domain["id"]]
    fresh = http.get("/api/modules").json()
    web = next(m for m in fresh["modules"] if m["id"] == "web")
    assert web["bound_domains"] == [domain["id"]]
    assert web["bindings"][0]["enabled"] is True

    unbound = http.post("/api/modules/web/bindings", json={
        "domain_id": domain["id"], "enabled": False,
    })
    assert unbound.json()["module"]["bound_domains"] == []


def test_tools_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    payload = http.get("/api/tools").json()
    names = {tool["name"] for tool in payload["tools"]}
    assert {"web_search", "search_knowledge"} <= names
    assert isinstance(payload["mcp_status"], dict)
    tool = next(t for t in payload["tools"] if t["name"] == "web_search")
    assert tool["module"] == "web"
    assert {"risk", "side_effects", "read_only", "untrusted_output"} <= set(
        tool
    )


def test_events_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    app.tasks.create(objective="event source", origin="user")
    http = client(app)
    events = http.get("/api/events").json()["events"]
    assert events
    assert any(event["type"] == "task.created" for event in events)
    assert {"type", "level", "ts", "data"} <= set(events[0])


def test_system_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    payload = http.get("/api/system").json()
    assert set(payload["counts"]) == {
        "open_tasks", "paused", "pending_approvals", "domains", "inbox_new",
        "memories", "archived_memories", "skills", "modules",
    }
    assert payload["working_task"] is None
    assert set(payload["scheduler"]) == {"last_tick", "last_report", "enabled"}
    assert any(m["id"] == "web" for m in payload["modules"])
    assert set(payload["model"]) == {"base_url", "name"}
    assert payload["system_timezone"] == util.system_tz_name()


def test_config_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    payload = http.get("/api/config").json()
    assert "server" in payload["config"]
    # Secrets never reach the editable state the UI renders.
    assert payload["config"]["server"].get("token") != app.config.get(
        "server.token"
    )
    assert payload["editable_groups"]
    assert payload["config_path"]


def test_backup_panel_contract(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    assert http.get("/api/backups").json()["backups"] == []
    created = http.post("/api/backup")
    assert created.status_code == 200
    backups = http.get("/api/backups").json()["backups"]
    assert [item["name"] for item in backups] == [
        created.json()["backup"].replace("\\", "/").rsplit("/", 1)[-1]
    ]
    assert backups[0]["size"] > 0
    assert backups[0]["mtime_ms"] > 0


def test_guard_panel_is_a_static_phase_seven_placeholder(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    page = http.get("/")
    assert 'data-tab="guard"' in page.text
    script = http.get("/assets/app.js").text
    assert "Injection guard (Phase 7)" in script
    # No guard endpoint exists yet; the panel must not poll one.
    assert "/api/guard" not in script
