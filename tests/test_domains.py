from __future__ import annotations

import pytest

from harness.constants import SELF_DOMAIN_ID
from harness.contracts.domains import StructureDraft
from harness.domains.registry import UnconfirmedError
from tests.conftest import plan, respond


def draft(name: str, kind: str = "domain", **kwargs) -> StructureDraft:
    return StructureDraft(kind=kind, name=name, reason="test", evidence="test", **kwargs)


def test_self_domain_is_created_once(make_harness) -> None:
    h = make_harness([])
    self_domain = h.domains.get_domain(SELF_DOMAIN_ID)
    assert self_domain.memory_scope_id == "self"
    assert h.domains.ensure_self().id == SELF_DOMAIN_ID
    assert [item.id for item in h.domains.list_domains()] == [SELF_DOMAIN_ID]


def test_creation_requires_confirmation(make_harness) -> None:
    h = make_harness([])
    with pytest.raises(UnconfirmedError):
        h.domains.create_domain(draft("homelab"), confirmed=False)
    with pytest.raises(UnconfirmedError):
        h.domains.create_project(draft("nas", "project"), confirmed=False)
    assert len(h.domains.list_domains()) == 1


def test_domain_and_project_creation_are_idempotent_by_name(make_harness) -> None:
    h = make_harness([])
    first = h.domains.create_domain(draft("Homelab"), confirmed=True)
    second = h.domains.create_domain(draft("homelab"), confirmed=True)
    assert first.id == second.id

    project = h.domains.create_project(draft("NAS", "project", domain_id=first.id), confirmed=True)
    again = h.domains.create_project(draft("nas", "project", domain_id=first.id), confirmed=True)
    assert project.id == again.id
    assert h.domains.find_project_by_name("nas").domain_id == first.id


def test_explicit_user_command_creates_structure_without_model(make_harness) -> None:
    h = make_harness([])
    result = h.loop.handle_turn("Make a new domain where we will talk about homelab automation")
    assert "Created domain 'homelab automation'" in result.messages[0]
    domain = h.domains.find_domain_by_name("homelab automation")
    assert domain is not None and domain.created_by == "user"

    project = h.loop.handle_turn("Create a new project called rack upgrade")
    assert "Created project 'rack upgrade'" in project.messages[0]
    created_project = h.domains.find_project_by_name("rack upgrade")
    assert created_project is not None and created_project.domain_id == SELF_DOMAIN_ID


def test_creation_without_a_name_asks(make_harness) -> None:
    h = make_harness([])
    result = h.loop.handle_turn("Make a new domain")
    assert result.messages == ["What should the domain be about?"]
    assert len(h.domains.list_domains()) == 1


def test_suggestion_confirmation_creates_pending_structure(make_harness) -> None:
    h = make_harness([])
    suggestion = h.domains.record_suggestion(draft("homelab"))
    result = h.loop.handle_turn("yes")
    assert f"Created domain 'homelab'" in result.messages[0]
    assert h.domains.find_domain_by_name("homelab") is not None
    assert h.domains.list_suggestions()[0].id == suggestion.id
    assert h.domains.list_suggestions()[0].status == "confirmed"


def test_declining_sets_a_cooldown_and_does_not_create(make_harness) -> None:
    h = make_harness([])
    h.domains.record_suggestion(draft("homelab"))
    result = h.loop.handle_turn("no")
    assert "won't create" in result.messages[0]
    assert h.domains.find_domain_by_name("homelab") is None
    assert h.domains.suggestion_on_cooldown("homelab") is True
    assert h.domains.list_suggestions()[0].status == "declined"


def test_repeated_subject_creates_one_suggestion(make_harness) -> None:
    h = make_harness(
        [plan("a"), respond("ok"), plan("b"), respond("ok")],
        settings={"domain": {"suggestion": {"threshold": 2, "subject_words": 2}}},
    )
    first = h.loop.handle_turn("homelab notes one")
    assert not any("want me to create" in item for item in first.messages)
    second = h.loop.handle_turn("homelab notes two")
    assert any("want me to create a domain for 'homelab notes'" in item for item in second.messages)
    suggestions = h.domains.list_suggestions()
    assert len(suggestions) == 1

    third = h.loop.handle_turn("homelab notes three")
    assert not any("want me to create" in item for item in third.messages)
    assert len(h.domains.list_suggestions()) == 1


def test_plan_reuses_existing_domain_and_project(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    project = h.domains.create_project(draft("nas", "project", domain_id=domain.id), confirmed=True)
    plan_payload = {
        "tasks": [
            {
                "goal": "check nas",
                "domain_name": "homelab",
                "project_name": "nas",
                "depends_on": [],
            }
        ]
    }
    h.model.push(plan_payload)
    h.model.push(respond("done"))
    result = h.loop.handle_turn("check the nas")
    task = result.tasks[-1]
    assert task.domain_id == domain.id
    assert task.project_id == project.id


def test_rename_and_archive_are_reflected_in_listings(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    project = h.domains.create_project(draft("nas", "project", domain_id=domain.id), confirmed=True)
    workspace = h.domains.create_workspace(project.id, "rack")

    h.domains.rename("domain", domain.id, "home lab")
    h.domains.rename("project", project.id, "nas build")
    h.domains.rename("workspace", workspace.id, "rack notes")
    assert h.domains.get_domain(domain.id).name == "home lab"
    assert h.domains.get_project(project.id).name == "nas build"
    assert h.domains.get_workspace(workspace.id).name == "rack notes"

    h.domains.archive("workspace", workspace.id)
    h.domains.archive("project", project.id)
    h.domains.archive("domain", domain.id)
    assert h.domains.list_domains() == [h.domains.get_domain(SELF_DOMAIN_ID)]
    assert h.domains.list_projects() == []
    assert h.domains.list_workspaces() == []
    assert len(h.domains.list_domains(include_archived=True)) == 2
    with pytest.raises(ValueError):
        h.domains.archive("domain", SELF_DOMAIN_ID)


def test_planner_prompt_includes_the_structure_directory(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    h.sessions.set_active_domain(domain.id)
    h.model.push(plan("check nas"))
    h.model.push(respond("done"))
    result = h.loop.handle_turn("check the nas")

    planner = [
        item
        for item in h.traces.list_for(interaction_id=result.interaction_id)
        if item.role == "planner"
    ][0]
    system = planner.messages[0]["content"]
    assert "Structure directory:" in system
    assert f"Active domain: homelab ({domain.id})" in system
    assert f"- {domain.id} | homelab | projects: 0" in system


def test_plan_resolves_normalized_names_and_reports_unresolved_misses(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("Home Lab"), confirmed=True)
    active = h.domains.create_domain(draft("Other"), confirmed=True)
    h.sessions.set_active_domain(active.id)
    h.model.push(
        {
            "tasks": [
                {"goal": "a", "domain_name": "home-lab", "depends_on": []},
                {"goal": "b", "domain_name": "missing lab", "depends_on": []},
            ]
        }
    )
    h.model.push(respond("a done"))
    h.model.push(respond("b done"))
    result = h.loop.handle_turn("do a and b")

    tasks = {item.goal: item for item in result.tasks}
    assert tasks["a"].domain_id == domain.id
    # An unresolved name falls back to the active domain and is recorded.
    assert tasks["b"].domain_id == active.id
    misses = h.events.query(operation="plan.structure_miss")
    assert misses
    assert misses[-1].data["misses"][0]["requested"] == "missing lab"
    assert misses[-1].data["active_domain_id"] == active.id


def test_heuristic_plan_inherits_the_active_domain(make_harness) -> None:
    h = make_harness([respond("hi")], settings={"tasks": {"planning_mode": "heuristic"}})
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    h.sessions.set_active_domain(domain.id)

    result = h.loop.handle_turn("hello")
    assert result.tasks[-1].domain_id == domain.id
