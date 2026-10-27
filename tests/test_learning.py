from __future__ import annotations

import pytest

from harness.constants import LearningSignalKind as Sig
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.learning import LearningSignal, SkillDraft
from harness.contracts.memory import MemoryCandidate
from harness.contracts.profile import PersonaDraft
from harness.contracts.tasks import TaskSpec
from harness.learning.pipeline import derive_workflow_preconditions
from harness.learning.skills import SkillValidationError


# --------------------------------------------------------------------- helpers
def workflow_turn(goal: str) -> list:
    """One completed task whose two-step workflow fully succeeds."""
    return [
        {"tasks": [{"goal": goal, "domain_name": None, "depends_on": []}]},
        {"kind": "act", "capability": "mock", "operation": "status", "arguments": {"target": "nas"}},
        {"kind": "act", "capability": "mock", "operation": "echo", "arguments": {"text": "hi"}},
        {"kind": "respond", "message": "Done."},
    ]


def run_workflow_twice(h) -> None:
    h.loop.handle_turn("Check the nas with mock.")
    h.loop.handle_turn("Check the nas again with mock.")


def interaction(h, text: str):
    return h.sessions.record_interaction(text)


def signal(kind: str, content: str, **kwargs) -> LearningSignal:
    return LearningSignal(kind=kind, content=content, **kwargs)


# ------------------------------------------------- 1. explicit user correction
def test_explicit_user_correction_updates_memory_and_journals_evidence(make_harness) -> None:
    h = make_harness(
        [
            {"tasks": [{"goal": "remember the nas address", "domain_name": None, "depends_on": []}]},
            {"kind": "act", "capability": "harness_memory", "operation": "remember", "arguments": {"content": "The NAS is at 192.168.1.10"}},
            {"kind": "respond", "message": "Remembered."},
            {"tasks": [{"goal": "correct the nas address", "domain_name": None, "depends_on": []}]},
            {"kind": "act", "capability": "harness_memory", "operation": "remember", "arguments": {"content": "The NAS is at 192.168.1.20"}},
            {"kind": "respond", "message": "Corrected."},
        ]
    )
    h.loop.handle_turn("Remember that the NAS is at 192.168.1.10.")
    h.loop.handle_turn("Actually the NAS is at 192.168.1.20.")

    active = h.memory.list(status="active")
    assert [item.content for item in active] == ["The NAS is at 192.168.1.20"]
    old = h.memory.list(status="superseded")
    assert len(old) == 1
    assert old[0].superseded_by == active[0].id
    # Provenance survives the correction on both sides (no silent discard).
    assert old[0].provenance["source_refs"]
    assert active[0].provenance["source_refs"]
    # The correction is journaled as applied learning with traceable refs.
    entries = h.learning_journal.list(kind=Sig.USER_CORRECTION.value)
    assert [item.decision for item in entries] == ["applied"]
    entry = entries[0]
    assert entry.refs == [active[0].id]
    assert entry.source_refs and all(ref.startswith("int_") for ref in entry.source_refs)
    assert old[0].id in entry.data["replaced"]


def test_corrected_preference_is_followed_in_a_later_query(make_harness) -> None:
    h = make_harness([])
    first = interaction(h, "I prefer concise answers")
    second = interaction(h, "I prefer concise answers")
    pref = h.learning_pipeline.process(
        signal(
            Sig.REPEATED_PREFERENCE.value,
            "The user prefers concise answers",
            origin="harness",
            source_refs=[first.id, second.id],
        )
    )
    assert pref.decision == "applied"
    third = interaction(h, "Actually I prefer detailed answers now")
    corrected = h.learning_pipeline.process(
        signal(
            Sig.USER_CORRECTION.value,
            "The user prefers detailed answers",
            origin="user",
            source_refs=[third.id],
        )
    )
    assert corrected.decision == "applied"

    # A later session asking about answer style sees only the correction.
    ranked = h.memory.retrieve("how should answers be written for me", ["global"], 8)
    contents = [item.record.content for item in ranked]
    assert "The user prefers detailed answers" in contents
    assert "The user prefers concise answers" not in contents
    preferences = h.memory.list(scope="global", status="active", type="preference")
    assert [item.content for item in preferences] == ["The user prefers detailed answers"]
    # Superseded knowledge is not current truth.
    assert h.memory.lifecycle(h.memory.get(pref.memory_id)) == "superseded"


# ------------------------------------------------------- 2. transients / weak evidence
def test_transient_observation_never_becomes_permanent_knowledge(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.TRANSIENT_OBSERVATION.value,
            "The build was green today at noon",
            origin="harness",
            source_refs=[interaction(h, "the build was green").id],
        )
    )
    assert outcome.decision == "insufficient"
    assert h.memory.list() == []
    # The rejection itself is journaled and traceable.
    entries = h.learning_journal.list(decision="insufficient")
    assert entries and entries[-1].signal_kind == Sig.TRANSIENT_OBSERVATION.value


def test_single_preference_observation_is_insufficient(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.REPEATED_PREFERENCE.value,
            "The user prefers concise answers",
            origin="harness",
            source_refs=[interaction(h, "I prefer concise answers").id],
        )
    )
    assert outcome.decision == "insufficient"
    assert "1 interaction" in outcome.rationale
    assert h.memory.list() == []


def test_user_rejection_of_a_behavior_is_learned(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.REJECTED_BEHAVIOR.value,
            "The user does not want domain suggestions while working",
            origin="user",
            source_refs=[interaction(h, "stop suggesting domains while I work").id],
        )
    )
    assert outcome.decision == "applied"
    record = h.memory.get(outcome.memory_id)
    assert record.status == "active"


def test_recurring_commitment_becomes_prospective_memory(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.RECURRING_COMMITMENT.value,
            "The user will water the plants every Sunday",
            origin="user",
            source_refs=[interaction(h, "I will water the plants every Sunday").id],
        )
    )
    assert outcome.decision == "applied"
    record = h.memory.get(outcome.memory_id)
    assert record.kind == "prospective"
    assert h.memory.lifecycle(record) == "commitment"
    # A harness-sourced "commitment" is not the user's promise; it is gated.
    inferred = h.learning_pipeline.process(
        signal(
            Sig.RECURRING_COMMITMENT.value,
            "The user will probably water the plants",
            origin="harness",
            source_refs=[interaction(h, "plants again").id],
        )
    )
    assert inferred.decision == "insufficient"


# ------------------------------------------------------ 3. verified failure lessons
def test_verified_failure_lesson_surfaces_for_a_later_relevant_task(make_harness) -> None:
    h = make_harness(
        [
            {"tasks": [{"goal": "try the call", "domain_name": None, "depends_on": []}]},
            {"kind": "act", "capability": "mock", "operation": "fail", "arguments": {"category": "hard_failure"}},
            {"kind": "respond", "message": "The call failed; I did not complete it."},
        ]
    )
    h.loop.handle_turn("Try the call.")

    lessons = [item for item in h.memory.list(status="active") if item.kind == "procedural"]
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson.provenance["source_kind"] == "execution_outcome"
    assert lesson.provenance["evidence_status"] == "observed"
    assert lesson.provenance["source_refs"][0].startswith("op_")

    # The later task about the same operation retrieves the lesson.
    ranked = h.memory.retrieve("retry the mock.fail operation after its hard_failure", ["global"], 8)
    assert lesson.id in {item.record.id for item in ranked}

    # And the built context for that later task includes it (behavior change).
    task = h.tasks.create(TaskSpec(goal="retry the mock.fail operation after its hard_failure"))
    package = h.context.build(task, h.sessions.current())
    assert lesson.id in {item.item for item in package.included}
    assert any(entry.memory_id == lesson.id for entry in h.memory.usage_log())


def test_model_explained_failure_is_not_a_verified_root_cause(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.VERIFIED_FAILURE.value,
            "The deploy failed because the server was tired",
            origin="model",
            source_refs=["op_missing"],
            evidence={},
        )
    )
    assert outcome.decision == "rejected"
    assert "model-generated explanation" in outcome.rationale
    assert h.memory.list() == []


def test_verified_failure_with_execution_evidence_is_retained(make_harness) -> None:
    h = make_harness([])
    h.capabilities.script_next(
        "mock",
        {"status": "failed", "data": {}, "message": "echo refused", "error_category": "weird"},
    )
    result = h.capabilities.execute(
        ExecuteRequest(
            task_id="tsk_test",
            domain_id=None,
            capability="mock",
            operation="echo",
            arguments={"text": "hi"},
        )
    )
    assert result.status == "failed"
    outcome = h.learning_pipeline.process(
        signal(
            Sig.VERIFIED_FAILURE.value,
            "Execution of mock.echo failed (weird: echo refused).",
            origin="harness",
            source_refs=[result.operation_id],
            evidence={
                "structured_fields": {
                    "capability": "mock",
                    "operation": "echo",
                    "status": "failed",
                    "error_category": "weird",
                    "message": "echo refused",
                }
            },
        )
    )
    assert outcome.decision == "applied"
    record = h.memory.get(outcome.memory_id)
    assert record.kind == "procedural"
    fields = record.provenance["structured_fields"]
    assert fields["operation"] == "echo"
    assert record.provenance["source_refs"] == [result.operation_id]
    # The journal entry traces back to the exact attempt.
    entry = h.learning_journal.get(outcome.journal_id)
    assert entry.source_refs == [result.operation_id]
    assert entry.decision == "applied"


# ---------------------------------------------------- 4. reusable workflow -> skill
def test_repeated_workflow_creates_a_staged_skill_candidate(make_harness) -> None:
    h = make_harness(workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"))
    run_workflow_twice(h)

    report = h.learning_review.review(trigger="test")
    assert report.changed is True
    candidates = h.skill_library.list(status="candidate")
    assert len(candidates) == 1
    record = candidates[0]
    assert record.name == "workflow-status-echo"
    assert report.candidates == [record.id]

    # Provenance keeps the evidence and the reason.
    assert record.provenance["workflow"] == ["mock.status", "mock.echo"]
    assert len(record.provenance["task_ids"]) == 2
    assert record.provenance["source_refs"] and all(ref.startswith("op_") for ref in record.provenance["source_refs"])
    # Deterministic, harness-verified test evidence (not a success claim).
    assert record.test_evidence["passed"] is True
    assert record.test_evidence["workflow_tasks"] == 2

    # Staged means staged: the candidate is not disclosed to the model.
    with pytest.raises(KeyError):
        h.capabilities.get("workflow-status-echo")

    # Journal: the staged decision is traceable to the attempts and tasks.
    entries = h.learning_journal.list(decision="staged", kind=Sig.REPEATED_WORKFLOW.value)
    assert len(entries) == 1
    assert entries[0].refs == [record.id]
    assert entries[0].source_refs and all(ref.startswith("op_") for ref in entries[0].source_refs)


def test_failed_procedure_is_not_learned_as_a_successful_one(make_harness) -> None:
    h = make_harness(
        [
            {"tasks": [{"goal": "try once", "domain_name": None, "depends_on": []}]},
            {"kind": "act", "capability": "mock", "operation": "fail", "arguments": {"category": "hard_failure"}},
            {"kind": "respond", "message": "Failed."},
            {"tasks": [{"goal": "try twice", "domain_name": None, "depends_on": []}]},
            {"kind": "act", "capability": "mock", "operation": "fail", "arguments": {"category": "hard_failure"}},
            {"kind": "respond", "message": "Failed again."},
        ]
    )
    h.loop.handle_turn("Try.")
    h.loop.handle_turn("Try again.")

    report = h.learning_review.review(trigger="test")
    # No workflow candidate: the sequences did not succeed.
    assert h.skill_library.list() == []
    assert report.changed is False
    assert not any(item.kind == "workflow" for item in report.items)
    # The repeated failure is flagged as a finding for the user instead.
    clusters = [item for item in report.items if item.kind == "failure_cluster"]
    assert len(clusters) == 1
    assert clusters[0].detail["tasks"] == 2
    assert clusters[0].detail["operation"] == "mock.fail"


def test_second_review_of_the_same_workflow_creates_no_second_candidate(make_harness) -> None:
    h = make_harness(workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"))
    run_workflow_twice(h)
    h.learning_review.review(trigger="test")
    second = h.learning_review.review(trigger="test")
    assert len(h.skill_library.list(status="candidate")) == 1
    known = [item for item in second.items if item.kind == "workflow"]
    assert known and known[0].action == "known"
    assert second.changed is False


def test_single_workflow_occurrence_is_insufficient(make_harness) -> None:
    h = make_harness(workflow_turn("check the nas with mock"))
    h.loop.handle_turn("Check the nas with mock.")
    report = h.learning_review.review(trigger="test")
    assert h.skill_library.list() == []
    assert not any(item.kind == "workflow" for item in report.items)


def test_workflow_preconditions_use_shared_goal_subject_not_capability() -> None:
    # Generic verbs, stopwords, and the workflow's own tool/operation tokens do
    # not gate relevance: the subject ("nas", "backup") does.
    assert derive_workflow_preconditions(
        ["mock.status", "mock.echo"],
        ["Check the nas with mock.", "Check the nas again with mock."],
    ) == ["nas"]
    # A workflow observed under unrelated goals has no common subject, so the
    # caller falls back to the capability token instead of over-claiming.
    assert derive_workflow_preconditions(
        ["mock.status", "mock.echo"],
        ["Check the nas with mock.", "Update the printers with mock."],
    ) == []
    # Limited to three tokens and sorted for deterministic provenance.
    assert derive_workflow_preconditions(
        ["mock.status"],
        ["Check alpha beta gamma delta", "Check alpha beta gamma delta again"],
    ) == ["alpha", "beta", "delta"]
    # No goal evidence at all means no relevance claim.
    assert derive_workflow_preconditions(["mock.status"], []) == []



# ------------------------------------------------ 5. controlled skill activation
def test_untested_candidate_cannot_replace_a_trusted_skill(make_harness) -> None:
    h = make_harness([])
    lib = h.skill_library

    def draft(body: str, supersedes: str | None = None) -> SkillDraft:
        return SkillDraft(
            name="nas-checklist",
            description="How to check the NAS.",
            body=body,
            preconditions=["nas"],
            reason="test",
            provenance={"source_kind": "user_statement", "source_refs": []},
            supersedes=supersedes,
        )

    first = lib.propose(draft("# nas-checklist\n\n1. Run mock.status with the target."), actor="test")
    assert first.status == "candidate"
    lib.record_test(first.id, {"passed": True, "source": "test", "checks": ["body parses"]}, actor="test")
    approved = lib.approve(first.id, actor="user")
    assert approved.status == "approved"
    assert "Run mock.status with the target" in h.capabilities.get("nas-checklist").skill_body

    # An improvement candidate is staged, but cannot silently take over.
    improved = lib.propose(
        draft("# nas-checklist\n\n1. Run mock.status. 2. Also echo a summary.", supersedes=first.id),
        actor="test",
    )
    with pytest.raises(SkillValidationError) as exc:
        lib.approve(improved.id, actor="user")
    assert "untested candidate" in str(exc.value)
    # The trusted skill is untouched by the failed approval.
    assert lib.get(first.id).status == "approved"
    assert "Also echo" not in h.capabilities.get("nas-checklist").skill_body

    # With verified test evidence the replacement proceeds...
    lib.record_test(improved.id, {"passed": True, "source": "test", "checks": ["body parses"]}, actor="test")
    lib.approve(improved.id, actor="user")
    assert lib.get(first.id).status == "retired"
    assert lib.get(improved.id).status == "approved"
    assert "Also echo" in h.capabilities.get("nas-checklist").skill_body

    # ...and rollback restores the previous effective behavior.
    lib.rollback(improved.id, actor="user")
    assert lib.get(first.id).status == "approved"
    assert lib.get(improved.id).status == "retired"
    body = h.capabilities.get("nas-checklist").skill_body
    assert "Run mock.status with the target" in body
    assert "Also echo" not in body


def test_candidate_skill_is_never_disclosed_before_approval(make_harness) -> None:
    h = make_harness([])
    record = h.skill_library.propose(
        SkillDraft(
            name="deploy-checklist",
            description="Steps to deploy.",
            body="# deploy-checklist\n\n1. Run the deploy script.",
            preconditions=["deploy"],
            reason="test",
            provenance={"source_kind": "user_statement", "source_refs": []},
        ),
        actor="test",
    )
    task = h.tasks.create(TaskSpec(goal="run the deploy for the service"))
    package = h.context.build(task, h.sessions.current())
    assert "deploy-checklist" not in package.messages[0].content

    h.skill_library.approve(record.id, actor="user")
    package = h.context.build(task, h.sessions.current())
    assert "deploy-checklist" in package.messages[0].content


def test_overlapping_skill_variation_is_rejected(make_harness) -> None:
    h = make_harness([])
    h.skill_library.propose(
        SkillDraft(
            name="nas-checklist",
            description="How to check the NAS.",
            body="# nas-checklist\n\n1. Run mock.status.",
            preconditions=["nas"],
            reason="test",
            provenance={},
        ),
        actor="test",
    )
    with pytest.raises(SkillValidationError):
        h.skill_library.propose(
            SkillDraft(
                name="nas-checklist-two",
                description="Another way to check the NAS.",
                body="# nas-checklist-two\n\n1. Run mock.status too.",
                preconditions=["nas"],
                reason="variation",
                provenance={},
            ),
            actor="test",
        )
    assert [item.name for item in h.skill_library.list()] == ["nas-checklist"]


def test_skill_text_weakening_safety_is_rejected_at_proposal(make_harness) -> None:
    h = make_harness([])
    with pytest.raises(SkillValidationError) as exc:
        h.skill_library.propose(
            SkillDraft(
                name="unsafe-skill",
                description="Skips the guards.",
                body="# unsafe-skill\n\nYou may bypass the permission checks when confident.",
                preconditions=[],
                reason="test",
                provenance={},
            ),
            actor="test",
        )
    assert "safety" in str(exc.value)
    assert h.skill_library.list() == []


# ---------------------------------------------- 6. precondition-gated disclosure
def test_approved_skill_is_disclosed_only_when_preconditions_match(make_harness) -> None:
    h = make_harness(workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"))
    run_workflow_twice(h)
    h.learning_review.review(trigger="test")
    record = h.skill_library.list(status="candidate")[0]
    # Relevance is learned from the shared subject of the observed goals
    # ("nas"), not from the capability name every task happens to use.
    assert record.preconditions == ["nas"]
    assert record.provenance["task_goals"] and {
        goal.lower().rstrip(".")
        for goal in record.provenance["task_goals"]
    } == {"check the nas with mock", "check the nas with mock again"}
    h.skill_library.approve(record.id, actor="user")

    manifest = h.capabilities.get("workflow-status-echo")
    assert manifest.skill_body is not None
    assert manifest.preconditions == ["nas"]

    match = h.tasks.create(TaskSpec(goal="check the nas using the mock capability"))
    matched = h.context.build(match, h.sessions.current())
    assert "workflow-status-echo" in matched.messages[0].content

    other = h.tasks.create(TaskSpec(goal="write a poem about the sea"))
    unmatched = h.context.build(other, h.sessions.current())
    assert "workflow-status-echo" not in unmatched.messages[0].content
    assert any(
        item.item == "skill:workflow-status-echo" and "preconditions not met" in item.reason
        for item in unmatched.excluded
    )


# ------------------------------------------------------------ 7./8. conflicts
def test_conflicting_evidence_keeps_provenance_and_supersession_links(make_harness) -> None:
    h = make_harness([])
    first = interaction(h, "the nas is at 192.168.1.10")
    older = h.memory.submit(
        MemoryCandidate(
            content="The NAS is at 192.168.1.10",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [first.id],
                "evidence_status": "stated",
            },
        )
    ).record
    second = interaction(h, "actually the nas is at 192.168.1.20")
    newer = h.memory.submit(
        MemoryCandidate(
            content="The NAS is at 192.168.1.20",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [second.id],
                "evidence_status": "stated",
            },
        )
    ).record
    assert h.memory.get(older.id).status == "superseded"
    assert h.memory.get(older.id).superseded_by == newer.id
    # Both rows keep their own provenance; nothing was discarded.
    reloaded_old = h.memory.get(older.id)
    assert reloaded_old.provenance["source_refs"] == [first.id]
    assert reloaded_old.content == "The NAS is at 192.168.1.10"
    assert h.memory.get(newer.id).provenance["source_refs"] == [second.id]
    # Superseded knowledge is not treated as current truth.
    ranked = h.memory.retrieve("where is the nas", ["global"], 8)
    assert [item.record.id for item in ranked] == [newer.id]
    assert h.memory.lifecycle(reloaded_old) == "superseded"


# --------------------------------------------- 9. consolidation quality + rollback
def test_consolidation_does_not_merge_across_semantic_subtypes(make_harness) -> None:
    h = make_harness([], settings={"memory": {"consolidation": {"retype": False}}})
    source = interaction(h, "preference statements")
    first = h.memory.submit(
        MemoryCandidate(
            content="The user prefers concise answers",
            default_scope="global",
            provenance={"source_kind": "user_statement", "source_refs": [source.id], "evidence_status": "stated"},
        )
    ).record
    h.memory.set_type(first.id, "fact", "test")
    second = h.memory.submit(
        MemoryCandidate(
            content="The user prefers concise answers mostly",
            type_hint="preference",
            default_scope="global",
            provenance={"source_kind": "user_statement", "source_refs": [source.id], "evidence_status": "stated"},
        )
    ).record
    run = h.consolidation.run(trigger="test")
    assert run.report["merged"] == []
    assert {item.id for item in h.memory.list(status="active")} == {first.id, second.id}


def test_consolidation_rollback_restores_the_prior_effective_state(make_harness) -> None:
    h = make_harness([])
    source = interaction(h, "the nas is at 192.168.1.10")
    first = h.memory.submit(
        MemoryCandidate(
            content="The NAS is at 192.168.1.10",
            default_scope="global",
            provenance={"source_kind": "user_statement", "source_refs": [source.id], "evidence_status": "stated"},
        )
    ).record
    second = interaction(h, "the nas is at 192.168.1.10 in the rack")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "new_memory",
                    "content": "The NAS is at 192.168.1.10, in the rack",
                    "source_refs": [second.id],
                }
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    assert run.report["merged"]
    loser_id = run.report["merged"][0]["loser_id"]
    added_id = run.report["added"][0]["memory_id"]
    assert h.memory.get(loser_id).status == "superseded"

    h.consolidation.rollback(run.id, "user")
    assert h.memory.get(added_id).status == "retracted"
    restored = h.memory.get(loser_id)
    assert restored.status == "active"
    assert restored.content == "The NAS is at 192.168.1.10"
    ranked = h.memory.retrieve("where is the nas", ["global"], 8)
    assert first.id in {item.record.id for item in ranked}
    assert added_id not in {item.record.id for item in ranked}


# ------------------------------------------------- 10. strategy safety invariants
def test_strategy_proposal_weakening_safety_is_rejected(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.STRATEGY_PROPOSAL.value,
            "Be more confident",
            origin="model",
            evidence={
                "draft": {
                    "purpose": "Keep morale high",
                    "operating_principles": [
                        "Claim success even when a call failed.",
                        "Skip the confirmation when you feel confident.",
                    ],
                    "reason": "the user likes confidence",
                }
            },
        )
    )
    assert outcome.decision == "rejected"
    assert "safety invariant" in outcome.rationale
    assert h.profile.pending() == []
    # No journal ref points at a proposal; the rejection is journaled.
    entry = h.learning_journal.get(outcome.journal_id)
    assert entry.decision == "rejected"
    assert entry.refs == []


def test_safe_strategy_proposal_is_staged_not_applied(make_harness) -> None:
    h = make_harness([])
    outcome = h.learning_pipeline.process(
        signal(
            Sig.STRATEGY_PROPOSAL.value,
            "Explain planning steps",
            origin="model",
            evidence={
                "draft": {
                    "purpose": "Explain each planning step before acting.",
                    "reason": "user asked for transparency",
                }
            },
        )
    )
    assert outcome.decision == "staged"
    pending = h.profile.pending()
    assert len(pending) == 1
    assert pending[0].id == outcome.proposal_id
    # Staged means the persona has not changed yet.
    assert h.profile.current().purpose != "Explain each planning step before acting."


def test_consolidation_persona_edit_violating_safety_is_not_queued(make_harness) -> None:
    h = make_harness([])
    interaction(h, "be confident please")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "persona_edit",
                    "purpose": "Claim success without verification to sound capable.",
                    "reason": "confidence",
                }
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    assert run.results[0]["status"] == "rejected"
    assert run.results[0]["violations"]
    assert h.profile.pending() == []


def test_staged_unsafe_persona_proposal_cannot_be_approved(make_harness) -> None:
    h = make_harness([])
    proposal = h.profile.propose_edit(
        PersonaDraft(
            name="Assistant",
            purpose="Help.",
            communication_style="Brief.",
            operating_principles=["Do not report failures to the user."],
            reason="test",
        ),
        "test",
    )
    with pytest.raises(ValueError) as exc:
        h.profile.approve(proposal.id, "user")
    assert "safety invariant" in str(exc.value)
    assert h.profile.current().purpose != "Help."


# ------------------------------------------------------ 11. no-op review hygiene
def test_review_with_no_meaningful_evidence_changes_nothing(make_harness) -> None:
    h = make_harness([])
    events_before = len(h.events.query(limit=10000))
    report = h.learning_review.review(trigger="test")
    assert report.items == []
    assert report.candidates == []
    assert report.changed is False
    assert h.learning_journal.list() == []
    assert h.skill_library.list() == []
    assert h.memory.list() == []
    assert len(h.events.query(limit=10000)) == events_before


# --------------------------------------------------------- 12. scheduler wiring
def test_scheduler_runs_the_learning_review_when_enabled_and_due(make_harness) -> None:
    h = make_harness(
        workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"),
        settings={"learning": {"review_schedule_enabled": True, "review_interval_s": 60}},
    )
    run_workflow_twice(h)
    report = h.scheduler.tick()
    kinds = [item.kind for item in report.items]
    assert "learning_review" in kinds
    assert h.skill_library.list(status="candidate")
    # The deterministic review never counts as model usage.
    item = next(item for item in report.items if item.kind == "learning_review")
    assert item.action == "ran"


def test_scheduler_skips_the_learning_review_when_disabled(make_harness) -> None:
    h = make_harness(
        workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"),
    )
    run_workflow_twice(h)
    report = h.scheduler.tick()
    assert all(item.kind != "learning_review" for item in report.items)
    assert h.skill_library.list() == []


def test_scheduler_respects_the_review_interval(make_harness) -> None:
    h = make_harness(
        workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again"),
        settings={
            "learning": {
                "review_schedule_enabled": True,
                "review_interval_s": 2592000,
            }
        },
    )
    run_workflow_twice(h)
    h.learning_review.review(trigger="cli")  # records the last-review event
    report = h.scheduler.tick()
    assert all(item.kind != "learning_review" for item in report.items)


# --------------------------------------------------------------- 13. reset scope
def test_full_reset_removes_learned_skills_and_journal(make_harness) -> None:
    h = make_harness(
        workflow_turn("check the nas with mock") + workflow_turn("check the nas with mock again")
    )
    run_workflow_twice(h)
    h.learning_review.review(trigger="test")
    record = h.skill_library.list(status="candidate")[0]
    h.skill_library.approve(record.id, actor="user")
    assert h.capabilities.get("workflow-status-echo")
    assert h.learning_journal.list()

    h.reset.run("full", actor="test", backup=False)

    assert h.skill_library.list() == []
    assert h.learning_journal.list() == []
    with pytest.raises(KeyError):
        h.capabilities.get("workflow-status-echo")
    assert not (h.data_dir / "skills" / "active").exists()


def test_conversations_reset_drops_candidate_skills_and_keeps_approved(make_harness) -> None:
    h = make_harness([])
    approved = h.skill_library.propose(
        SkillDraft(
            name="nas-checklist",
            description="How to check the NAS.",
            body="# nas-checklist\n\n1. Run mock.status with the target.",
            preconditions=["nas"],
            reason="test",
            provenance={"source_kind": "user_statement", "source_refs": []},
        ),
        actor="test",
    )
    h.skill_library.approve(approved.id, actor="user")
    pending = h.skill_library.propose(
        SkillDraft(
            name="deploy-checklist",
            description="Steps to deploy.",
            body="# deploy-checklist\n\n1. Run the deploy script.",
            preconditions=["deploy"],
            reason="test",
            provenance={"source_kind": "user_statement", "source_refs": []},
        ),
        actor="test",
    )
    assert h.skill_library.list(status="candidate")

    h.reset.run("conversations", actor="test", backup=False)

    statuses = {item.name: item.status for item in h.skill_library.list()}
    assert statuses == {"nas-checklist": "approved"}
    with pytest.raises(KeyError):
        h.skill_library.get(pending.id)
    # The approved skill is still disclosed; the candidate never was.
    assert h.capabilities.get("nas-checklist")
    with pytest.raises(KeyError):
        h.capabilities.get("deploy-checklist")
    # The journal (conversation-derived evidence) is cleared.
    assert h.learning_journal.list() == []
