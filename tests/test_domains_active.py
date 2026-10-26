from __future__ import annotations

from harness.constants import ExecutionStatus
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.domains import StructureDraft
from harness.contracts.tasks import TaskSpec
from tests.conftest import ask, plan, respond


def draft(name: str, **kwargs) -> StructureDraft:
    return StructureDraft(name=name, reason="test", evidence="test", **kwargs)


def test_explicit_switch_command_sets_active_domain_without_creating(make_harness) -> None:
    h = make_harness([plan("switch to nowhere"), respond("I don't have a domain named nowhere.")])
    domain = h.domains.create_domain(draft("Home Lab"), confirmed=True)

    switched = h.loop.handle_turn("work on home-lab")
    assert "Active domain: 'Home Lab'" in switched.messages[0]
    assert h.sessions.active_domain() == domain.id
    assert len(h.domains.list_domains()) == 2  # self + Home Lab

    # An unresolved phrase is ordinary prose, not a command: it falls through
    # to planning instead of hijacking the turn.
    unknown = h.loop.handle_turn("switch to nowhere")
    assert "nowhere" in unknown.messages[0]
    assert h.sessions.active_domain() == domain.id
    assert len(h.domains.list_domains()) == 2


def test_switch_command_reports_ambiguous_normalized_names(make_harness) -> None:
    h = make_harness([])
    h.domains.create_domain(draft("Home Lab"), confirmed=True)
    h.domains.create_domain(draft("Home-Lab"), confirmed=True)

    result = h.loop.handle_turn("open HOME  LAB")
    assert "Home Lab" in result.messages[0]
    assert "Home-Lab" in result.messages[0]
    assert h.sessions.active_domain() is None


def test_enter_and_leave_domain_update_session_and_running_task(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    task = h.tasks.create(TaskSpec(goal="work"))

    entered = h.capabilities.execute(
        ExecuteRequest(
            task_id=task.id,
            domain_id=None,
            capability="harness_structure",
            operation="enter_domain",
            arguments={"name": "homelab"},
        )
    )
    assert entered.status == ExecutionStatus.SUCCEEDED
    assert h.sessions.active_domain() == domain.id
    assert h.tasks.get(task.id).domain_id == domain.id

    left = h.capabilities.execute(
        ExecuteRequest(
            task_id=task.id,
            domain_id=None,
            capability="harness_structure",
            operation="leave_domain",
            arguments={},
        )
    )
    assert left.status == ExecutionStatus.SUCCEEDED
    assert h.sessions.active_domain() is None
    assert h.tasks.get(task.id).domain_id is None


def test_enter_domain_missing_or_ambiguous_fails_with_candidates(make_harness) -> None:
    h = make_harness([])
    h.domains.create_domain(draft("Home Lab"), confirmed=True)
    h.domains.create_domain(draft("Home-Lab"), confirmed=True)
    task = h.tasks.create(TaskSpec(goal="work"))

    def enter(name: str):
        return h.capabilities.execute(
            ExecuteRequest(
                task_id=task.id,
                domain_id=None,
                capability="harness_structure",
                operation="enter_domain",
                arguments={"name": name},
            )
        )

    missing = enter("nowhere")
    assert missing.status == ExecutionStatus.FAILED
    assert "Home Lab" in missing.data["available"]
    assert h.sessions.active_domain() is None

    ambiguous = enter("HOME  LAB")
    assert ambiguous.status == ExecutionStatus.FAILED
    assert set(ambiguous.data["candidates"]) == {"Home Lab", "Home-Lab"}
    assert h.sessions.active_domain() is None


def test_suggestion_hook_suppresses_existing_pending_and_cooldown(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("Home Lab"), confirmed=True)
    task = h.tasks.create(TaskSpec(goal="structure"))

    def suggest(name: str):
        return h.capabilities.execute(
            ExecuteRequest(
                task_id=task.id,
                domain_id=None,
                capability="harness_structure",
                operation="suggest_domain",
                arguments={"name": name},
            )
        )

    existing = suggest("home-lab")
    assert existing.status == ExecutionStatus.FAILED
    assert existing.data["existing_domain"]["id"] == domain.id
    assert h.domains.list_suggestions() == []

    first = suggest("Future Lab")
    assert first.status == ExecutionStatus.SUCCEEDED
    assert len(h.domains.list_suggestions()) == 1

    duplicate = suggest("future-lab")
    assert duplicate.status == ExecutionStatus.FAILED
    assert duplicate.data["duplicate"] is True
    assert len(h.domains.list_suggestions()) == 1

    h.domains.decline_suggestion(first.data["suggestion_id"])
    cooldown = suggest("Future Lab")
    assert cooldown.status == ExecutionStatus.FAILED
    assert cooldown.data["cooldown"] is True
    assert len(h.domains.list_suggestions()) == 1


def test_bare_yes_does_not_confirm_when_a_task_awaits_the_user(make_harness) -> None:
    h = make_harness([plan("echo"), ask("What should I echo?", "text"), respond("ok")])
    h.loop.handle_turn("echo something")
    h.domains.record_suggestion(draft("homelab"))

    h.loop.handle_turn("yes")
    assert h.domains.find_domain_by_name("homelab") is None
    assert h.domains.list_suggestions()[0].status == "pending"
    assert any("User reply: yes" in item.constraints for item in h.tasks.list())


def test_bare_yes_with_multiple_pending_suggestions_plans_instead(make_harness) -> None:
    h = make_harness([respond("ok")], settings={"tasks": {"planning_mode": "heuristic"}})
    h.domains.record_suggestion(draft("homelab"))
    h.domains.record_suggestion(draft("garden"))

    h.loop.handle_turn("yes")
    assert h.domains.find_domain_by_name("homelab") is None
    assert h.domains.find_domain_by_name("garden") is None
    assert [item.status for item in h.domains.list_suggestions()] == ["pending", "pending"]
