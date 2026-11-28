from pathlib import Path

import pytest

from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.session import client_ref
from test_projects_session import session_env  # noqa: F401  (fixture)


def _spec(projects):
    project = projects.get("acq")
    path = Path(project.working_copy.path) / project.materials_root / "SPEC.md"
    path.write_text("ТЗ v1\n")
    return path


async def test_confirmation_becomes_a_fact_and_goes_stale(session_env):
    session, projects, owner = session_env
    spec = _spec(projects)
    confirmation = await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                                         linked=[{"kind": "catalogue", "ref": "requirements"},
                                                 {"kind": "place", "ref": "implementation:agent"}],
                                         channel="cli", author="t", note=None)
    assert confirmation.attests["digest"].startswith("sha256:")
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.holds and fact.source["attests"]["path"] == "SPEC.md"
    spec.write_text("ТЗ v2\n")
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert not fact.holds and fact.stale_reason.codes == ("digest_changed",)


async def test_confirmation_race_and_reserved_names(session_env):
    session, projects, owner = session_env
    _spec(projects)
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md", "digest": "sha256:" + "0" * 64},
                              linked=[], channel="cli", author="t", note=None)
    assert caught.value.code == "digest_mismatch"
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="connected", attests={"path": "SPEC.md"}, linked=[], channel="cli", author="t", note=None)
    assert caught.value.code == "reserved_claim"


async def test_accept_carries_and_next_edit_stales_again(session_env):
    session, projects, owner = session_env
    spec = _spec(projects)
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[], channel="cli", author="t", note=None)
    spec.write_text("ТЗ v2 (опечатка)\n")
    preview = await session.accept("acq", None, only=None, across_pairs=False, reason="опечатка", channel="cli", author="t", dry_run=True)
    assert preview.id is None and [f["id"] for f in preview.facts] == ["spec.confirmed"]
    _, facts = await session.facts("acq")
    assert not next(f for f in facts if f.id == "spec.confirmed").holds  # dry run wrote nothing
    accepted = await session.accept("acq", None, only=None, across_pairs=False, reason="опечатка", channel="cli", author="t", dry_run=False)
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.holds and fact.carried_over[0]["acceptance"] == accepted.id
    spec.write_text("ТЗ v3\n")
    _, facts = await session.facts("acq")
    assert not next(f for f in facts if f.id == "spec.confirmed").holds
    await session.revoke_acceptance("acq", accepted.id, "передумал")


async def test_accept_refuses_other_pair_without_flag_and_nothing_to_accept(session_env):
    session, projects, owner = session_env
    with pytest.raises(ProjectError) as caught:
        await session.accept("acq", None, only=None, across_pairs=False, reason="r", channel="cli", author="t", dry_run=False)
    assert caught.value.code == "not_acceptable"


# --- Fix round 1 (controller ruling R16) ---

async def test_stale_codes_cover_every_changed_digest_against_the_latest_accepted_state(session_env, monkeypatch):
    """I1: codes must compare against the latest accepted ``to`` (not the original confirmation),
    and report every changed position, not just the first."""
    from evolution_lab import guides as guides_module
    session, projects, owner = session_env
    spec = _spec(projects)
    monkeypatch.setattr(guides_module, "requirements", lambda: {"digest": "1" * 64})
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "catalogue", "ref": "requirements"}],
                          channel="cli", author="t", note=None)

    # (a) the attested file and the linked catalogue both change: both codes show, in order.
    spec.write_text("ТЗ v2\n")
    monkeypatch.setattr(guides_module, "requirements", lambda: {"digest": "2" * 64})
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.stale_reason.codes == ("digest_changed", "catalogue_changed")

    # (b) accept the SPEC edit only, then change just the catalogue again: only catalogue_changed
    # remains — codes compare against the accepted state, not the original confirmation.
    accepted = await session.accept("acq", None, only=["spec.confirmed"], across_pairs=False, reason="r",
                                    channel="cli", author="t", dry_run=False)
    assert accepted.id is not None
    monkeypatch.setattr(guides_module, "requirements", lambda: {"digest": "3" * 64})
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.stale_reason.codes == ("catalogue_changed",)


def test_catalogue_link_accepts_scenarios(session_env):
    from evolution_lab.guides import scenarios
    from evolution_lab.projects.attest import current_digests
    from evolution_lab.projects.revision import materials_revision
    session, projects, owner = session_env
    _spec(projects)
    project = projects.get("acq")
    revision = materials_revision(project)
    now = current_digests(projects, project, None, revision, attests_path="SPEC.md",
                          linked=[{"kind": "catalogue", "ref": "scenarios"}])
    assert now["linked"][0]["digest"] == scenarios().catalogue["digest"]


async def test_evidence_fact_acceptance_end_to_end_diffs_only_the_new_change(session_env):
    """I1 (evidence facts, not just confirmations): accept carries a check.passed fact; a later
    edit reports ``changes`` against the accepted inventory, listing only what changed since."""
    from evolution_lab.owner.models import ClientTask
    session, projects, owner = session_env
    project = projects.get("acq")
    agent = Path(project.working_copy.path) / "agent"
    (agent / "b.py").write_text("B = 1\n")
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert check.holds

    (agent / "a.py").write_text("A = 2\n")
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert not check.holds and check.stale_reason.codes == ("inputs_changed",)

    accepted = await session.accept("acq", None, only=["check.passed"], across_pairs=False, reason="typo",
                                    channel="cli", author="t", dry_run=False)
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert check.holds and check.carried_over[0]["acceptance"] == accepted.id

    (agent / "b.py").write_text("B = 2\n")
    _, facts = await session.facts("acq")
    check = next(f for f in facts if f.id == "check.passed")
    assert not check.holds and check.stale_reason.codes == ("inputs_changed",)
    assert check.stale_reason.changes.modified == ("agent/b.py",)


async def test_revoke_acceptance_restales_the_fact_and_keeps_the_record(session_env):
    """I1/acceptance lifecycle: a revoked acceptance no longer carries the fact forward, but the
    acceptance record itself is kept (with ``revoked`` set), not deleted."""
    session, projects, owner = session_env
    spec = _spec(projects)
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                          channel="cli", author="t", note=None)
    spec.write_text("ТЗ v2\n")
    accepted = await session.accept("acq", None, only=None, across_pairs=False, reason="опечатка",
                                    channel="cli", author="t", dry_run=False)
    _, facts = await session.facts("acq")
    assert next(f for f in facts if f.id == "spec.confirmed").holds

    revoked = await session.revoke_acceptance("acq", accepted.id, "передумал")
    assert revoked.id == accepted.id and revoked.revoked["reason"] == "передумал"
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert not fact.holds and fact.stale_reason.codes == ("digest_changed",)
    stored = next(a for a in projects.store.acceptances("acq") if a.id == accepted.id)
    assert stored.revoked is not None


async def test_project_level_confirmation_with_two_implementations(session_env):
    """I2: implementation_id=None means project-level (no default-implementation lookup), so it
    stays buildable even with several active implementations, and applies to all of them."""
    session, projects, owner = session_env
    _spec(projects)
    projects.update("acq", {"op": "add_implementation",
                            "implementation": {"id": "agent2", "name": "Agent2", "place": "agent"}})
    confirmation = await session.confirm("acq", None, claim="global.ready", attests={"path": "SPEC.md"}, linked=[],
                                         channel="cli", author="t", note=None)
    assert confirmation.implementation is None
    for implementation_id in ("agent", "agent2"):
        _, facts = await session.facts("acq", implementation_id)
        fact = next(f for f in facts if f.id == "global.ready")
        assert fact.holds


async def test_linked_file_ref_path_traversal_and_absolute_are_rejected(session_env):
    """I3: a linked ``file`` ref is validated exactly like ``attests.path``."""
    session, projects, owner = session_env
    _spec(projects)
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                              linked=[{"kind": "file", "ref": "../x"}], channel="cli", author="t", note=None)
    assert caught.value.code == "invalid_request"
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                              linked=[{"kind": "file", "ref": "/etc/passwd"}], channel="cli", author="t", note=None)
    assert caught.value.code == "invalid_request"


def test_plan_acceptance_is_pure():
    from evolution_lab.projects.attest import plan_acceptance
    from evolution_lab.projects.models import Fact, StaleReason

    other_pair = Fact("check.passed", None, True, {"kind": "task", "id": "t1"},
                      StaleReason(("other_pair", "inputs_changed")), (), {"inputs_digest": "D2"})
    package_valid = Fact("package.valid", None, {"scope": "form"}, {"kind": "package_entry"},
                        StaleReason(("inputs_changed",)), (), {"inputs_digest": "D2"})
    false_value = Fact("check.passed", None, False, {"kind": "task", "id": "t2"},
                       StaleReason(("inputs_changed",)), (), {"inputs_digest": "D2"})
    connected = Fact("connected", None, True, {"kind": "derived"}, StaleReason(("inputs_changed",)), (), {})
    facts = [other_pair, package_valid, false_value, connected]

    with pytest.raises(ProjectError) as caught:
        plan_acceptance(facts, only=None, across_pairs=False)
    assert caught.value.code == "not_acceptable"  # other_pair excluded without the flag; the rest never accept

    accepted = plan_acceptance(facts, only=None, across_pairs=True)
    assert [f.id for f in accepted] == ["check.passed"] and accepted[0] is other_pair

    with pytest.raises(ProjectError) as caught:
        plan_acceptance(facts, only=["connected"], across_pairs=True)
    assert caught.value.code == "not_acceptable"


# --- Final-review fix wave ---

@pytest.mark.parametrize("revoke,codes", [("first", ("digest_changed", "catalogue_changed")),
                                          ("second", ("catalogue_changed",))])
async def test_revoking_a_link_of_a_confirmation_chain_breaks_it_from_there(session_env, monkeypatch, revoke, codes):
    """I1: A1 carries SPEC v1->v2, A2 carries catalogue 1->2. Revoking A1 breaks the whole chain
    (codes against the original confirmation); revoking only A2 stales relative to A1's ``to``."""
    from evolution_lab import guides as guides_module
    session, projects, owner = session_env
    spec = _spec(projects)
    monkeypatch.setattr(guides_module, "requirements", lambda: {"digest": "1" * 64})
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "catalogue", "ref": "requirements"}], channel="cli", author="t", note=None)
    spec.write_text("ТЗ v2\n")
    first = await session.accept("acq", None, only=["spec.confirmed"], across_pairs=False, reason="r1",
                                 channel="cli", author="t", dry_run=False)
    monkeypatch.setattr(guides_module, "requirements", lambda: {"digest": "2" * 64})
    second = await session.accept("acq", None, only=["spec.confirmed"], across_pairs=False, reason="r2",
                                  channel="cli", author="t", dry_run=False)
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.holds and [item["acceptance"] for item in fact.carried_over] == [first.id, second.id]

    await session.revoke_acceptance("acq", (first if revoke == "first" else second).id, "wrong")
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert not fact.holds and fact.carried_over == () and fact.stale_reason.codes == codes


async def test_a_confirmation_with_a_missing_digest_is_not_acceptable(session_env):
    """I2: the attested file is gone, so the current digest is missing — such a state must never
    become an acceptance's ``to`` (it would then hold against itself)."""
    session, projects, owner = session_env
    spec = _spec(projects)
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                          channel="cli", author="t", note=None)
    spec.unlink()
    for only in (None, ["spec.confirmed"]):
        with pytest.raises(ProjectError) as caught:
            await session.accept("acq", None, only=only, across_pairs=False, reason="r", channel="cli", author="t",
                                 dry_run=False)
        assert caught.value.code == "not_acceptable"
    assert projects.store.acceptances("acq") == ()
    _, facts = await session.facts("acq")
    assert not next(f for f in facts if f.id == "spec.confirmed").holds


async def test_accept_refuses_when_the_inputs_move_after_the_facts_read(session_env, monkeypatch):
    """I4: the inventory stored under ``changes_to`` must be the one that digest names; if the
    sources change between the facts read and the inventory recomputation, nothing is written."""
    from evolution_lab.owner.models import ClientTask
    session, projects, owner = session_env
    project = projects.get("acq")
    agent = Path(project.working_copy.path) / "agent"
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    (agent / "a.py").write_text("A = 2\n")

    real_facts, seen = session.facts, {}

    async def facts_then_edit(*args, **kwargs):
        revision, facts = await real_facts(*args, **kwargs)
        seen["to"] = next(f for f in facts if f.id == "check.passed").state["inputs_digest"]
        (agent / "a.py").write_text("A = 3  # edited between the facts read and the inventory\n")
        return revision, facts
    monkeypatch.setattr(session, "facts", facts_then_edit)

    with pytest.raises(ProjectError) as caught:
        await session.accept("acq", None, only=["check.passed"], across_pairs=False, reason="r", channel="cli",
                             author="t", dry_run=False)
    assert caught.value.code == "inputs_changed"
    assert projects.store.acceptances("acq") == ()
    assert projects.store.inventory(seen["to"]) is None


# --- Codex branch review round 1 ---

def _clock(monkeypatch):
    """A strictly increasing ``utc_now`` for the session and the attest writers, so the order of
    accept/revoke is not left to second-resolution wall time."""
    from evolution_lab.projects import attest as attest_module, session as session_module
    ticks = iter(range(10, 60))
    now = lambda: f"2026-09-27T10:00:{next(ticks):02d}Z"  # noqa: E731
    monkeypatch.setattr(attest_module, "utc_now", now)
    monkeypatch.setattr(session_module, "utc_now", now)


async def test_re_revoking_an_acceptance_keeps_the_first_revocation_and_a_later_re_acceptance(session_env,
                                                                                            monkeypatch):
    """Codex acceptance-rerevoke-breaks-chain (R-a): A1 accepted, revoked; A2 re-accepted; revoking A1
    again must not move its ``revoked.at`` past A2 — the fact keeps holding through A2."""
    _clock(monkeypatch)
    session, projects, owner = session_env
    spec = _spec(projects)
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                          channel="cli", author="t", note=None)
    spec.write_text("ТЗ v2\n")
    first = await session.accept("acq", None, only=None, across_pairs=False, reason="r1", channel="cli",
                                 author="t", dry_run=False)
    revoked = await session.revoke_acceptance("acq", first.id, "wrong")
    second = await session.accept("acq", None, only=None, across_pairs=False, reason="r2", channel="cli",
                                  author="t", dry_run=False)
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.holds and [item["acceptance"] for item in fact.carried_over] == [second.id]

    again = await session.revoke_acceptance("acq", first.id, "wrong again")
    _, facts = await session.facts("acq")
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert fact.holds and [item["acceptance"] for item in fact.carried_over] == [second.id]
    assert again == revoked and again.revoked == {"at": revoked.revoked["at"], "reason": "wrong"}
    stored = next(a for a in projects.store.acceptances("acq") if a.id == first.id)
    assert stored.revoked == revoked.revoked
    with pytest.raises(ProjectError) as caught:
        await session.revoke_acceptance("acq", "a-unknown", "r")
    assert caught.value.code == "project_not_found"


async def test_re_revoking_a_confirmation_returns_it_unchanged(session_env, monkeypatch):
    """R-a, the confirmation twin: a second revoke keeps the first ``revoked {at, reason}``."""
    _clock(monkeypatch)
    session, projects, owner = session_env
    _spec(projects)
    confirmation = await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                                         linked=[], channel="cli", author="t", note=None)
    revoked = await session.revoke_confirmation("acq", confirmation.id, "wrong")
    again = await session.revoke_confirmation("acq", confirmation.id, "wrong again")
    assert again == revoked and again.revoked == {"at": revoked.revoked["at"], "reason": "wrong"}
    stored = next(c for c in projects.store.confirmations("acq") if c.id == confirmation.id)
    assert stored.revoked == revoked.revoked
    with pytest.raises(ProjectError) as caught:
        await session.revoke_confirmation("acq", "c-unknown", "r")
    assert caught.value.code == "project_not_found"


async def test_status_step_names_both_confirmations_and_takes_done_at_from_the_last(session_env, monkeypatch):
    """W61 (spec §3.2): a project-level and an implementation-level confirmation of one claim."""
    _clock(monkeypatch)
    session, projects, owner = session_env
    _spec(projects)
    mine = await session.confirm("acq", "agent", claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                                 channel="cli", author="tester", note=None)
    shared = await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                                   channel="auto-evolve", author="lead", note=None)
    value = await session.status_payload("acq", scenario="connect")
    step = {s["id"]: s for s in value["scenarios"][0]["steps"]}["spec-confirm"]
    assert step["sources"] == [{"fact": "spec.confirmed", "confirmations": [
        {"id": mine.id, "author": "tester", "channel": "cli", "created_at": mine.created_at},
        {"id": shared.id, "author": "lead", "channel": "auto-evolve", "created_at": shared.created_at}]}]
    assert step["done_at"] == shared.created_at
    facts = {(f["id"], (f["subject"] or {}).get("implementation")): f for f in value["facts"]}
    assert facts[("spec.confirmed", None)]["source"]["author"] == "lead"


async def test_implementation_ported_is_a_reserved_claim(session_env):
    session, projects, owner = session_env
    _spec(projects)
    with pytest.raises(ProjectError) as caught:
        await session.confirm("acq", None, claim="implementation.ported", attests={"path": "SPEC.md"},
                              linked=[], channel="cli", author="t", note=None)
    assert caught.value.code == "reserved_claim"



async def _released(awaitable, fifo):
    """``awaitable`` within 10 s; on a timeout the reader stuck in open() is released so the run cannot hang."""
    import asyncio
    import os
    try:
        return await asyncio.wait_for(awaitable, timeout=10)
    except TimeoutError:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        raise


@pytest.mark.parametrize("where", ["attests", "linked"])
async def test_a_fifo_at_a_confirmed_path_reads_like_a_missing_file_without_blocking(session_env, where):
    """codex r1 of the dev35 branch (fold-in): the digest of an attested or linked file is never a blocking read;
    a special file there is what a missing file is — the confirmation's fact does not hold."""
    import os
    session, projects, owner = session_env
    spec = _spec(projects)
    notes = spec.with_name("NOTES.md")
    notes.write_text("n\n")
    await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"},
                          linked=[{"kind": "file", "ref": "NOTES.md"}], channel="cli", author="t", note=None)
    target = spec if where == "attests" else notes
    target.unlink()
    _, facts = await _released(session.facts("acq"), target)
    missing = next(f for f in facts if f.id == "spec.confirmed")
    os.mkfifo(target)
    _, facts = await _released(session.facts("acq"), target)
    fifo = next(f for f in facts if f.id == "spec.confirmed")
    assert not fifo.holds and fifo == missing
    with pytest.raises(ProjectError) as caught:  # confirming a special file is refused as a missing one is
        await _released(session.confirm("acq", None, claim="spec.other", attests={"path": target.name}, linked=[],
                                        channel="cli", author="t", note=None), target)
    assert caught.value.code == "invalid_request"

# PDR-0027 / ADR-0024: the acceptance must match the preview exactly.

async def _stale_confirmations(session, projects, *, claims=("spec.confirmed",)):
    path = _spec(projects)
    for claim in claims:
        await session.confirm("acq", None, claim=claim, attests={"path": "SPEC.md"}, linked=[],
                              channel="cli", author="t", note=None)
    path.write_text("ТЗ v2\n")
    return path


async def _accept(session, *, dry_run=False, **kwargs):
    return await session.accept("acq", None, only=None, across_pairs=False, reason="typo", channel="cli",
                                author="t", dry_run=dry_run, **kwargs)


def _expected(preview):
    return [{key: row[key] for key in ("id", "from", "to")} for row in preview.facts]


async def test_accept_with_matching_expected_writes(session_env):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects, claims=("spec.confirmed", "review.confirmed"))
    preview = await _accept(session, dry_run=True)
    expected = list(reversed(_expected(preview)))
    record = await _accept(session, expected=expected + expected[:1])  # exact duplicates are set members
    assert record.id and record.facts == preview.facts
    assert projects.store.acceptances("acq") == (record,)


@pytest.mark.parametrize("change", ["bytes", "source"])
async def test_accept_expected_changed_to_refuses(session_env, change):
    session, projects, _ = session_env
    path = await _stale_confirmations(session, projects)
    preview = await _accept(session, dry_run=True)
    if change == "source":
        path.write_text("ТЗ v1\n")
        await session.confirm("acq", None, claim="spec.confirmed", attests={"path": "SPEC.md"}, linked=[],
                              channel="cli", author="t", note=None)
    path.write_text("ТЗ v3\n" if change == "bytes" else "ТЗ v2\n")
    before = projects.store.acceptances("acq")
    with pytest.raises(ProjectError) as caught:
        await _accept(session, expected=_expected(preview))
    assert caught.value.code == "digest_mismatch"
    assert projects.store.acceptances("acq") == before


@pytest.mark.parametrize("change", ["extra", "missing"])
async def test_accept_expected_extra_and_missing_fact_refuse(session_env, change):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects, claims=("spec.confirmed", "review.confirmed"))
    preview = await _accept(session, dry_run=True)
    expected = _expected(preview)
    expected = expected + [{**expected[0], "id": "other.confirmed"}] if change == "extra" else expected[:1]
    with pytest.raises(ProjectError) as caught:
        await _accept(session, expected=expected)
    assert caught.value.code == "digest_mismatch"
    assert projects.store.acceptances("acq") == ()


async def test_accept_expected_only_fact_accepted_meanwhile_is_digest_mismatch(session_env):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects)
    preview = await _accept(session, dry_run=True)
    accepted = await _accept(session)
    with pytest.raises(ProjectError) as caught:
        await session.accept("acq", None, only=["spec.confirmed"], across_pairs=False, reason="typo", channel="cli",
                             author="t", dry_run=False, expected=_expected(preview))
    assert caught.value.code == "digest_mismatch"
    assert projects.store.acceptances("acq") == (accepted,)


@pytest.mark.parametrize("expected", [None, [{"id": [], "from": None, "to": None}]])
async def test_accept_dry_run_ignores_expected(session_env, expected):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects)
    preview = await _accept(session, dry_run=True, expected=expected)
    assert preview.id is None and preview.facts
    assert projects.store.acceptances("acq") == ()


@pytest.mark.parametrize("field,value", [("id", []), ("id", {}), ("from", []), ("to", None)])
async def test_accept_unmatchable_expected_json_refuses(session_env, field, value):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects)
    expected = _expected(await _accept(session, dry_run=True))
    expected[0][field] = value
    with pytest.raises(ProjectError) as caught:
        await _accept(session, expected=expected)
    assert caught.value.code == "digest_mismatch"
    assert projects.store.acceptances("acq") == ()


@pytest.mark.parametrize("explicit_null", [False, True])
async def test_accept_without_expected_unchanged(session_env, monkeypatch, explicit_null):
    session, projects, _ = session_env
    await _stale_confirmations(session, projects)
    facts = session.facts
    async def unlocked_facts(*args, **kwargs):
        assert not session._mutation_lock.locked()
        return await facts(*args, **kwargs)
    monkeypatch.setattr(session, "facts", unlocked_facts)
    record = await _accept(session, **({"expected": None} if explicit_null else {}))
    assert record.id and record.facts


@pytest.mark.parametrize("move_inputs", [False, True])
async def test_accept_expected_saves_inventory_of_recorded_digest(session_env, monkeypatch, move_inputs):
    from evolution_lab.owner.models import ClientTask
    from evolution_lab.projects import session as session_module
    session, projects, owner = session_env
    project = projects.get("acq")
    agent = Path(project.working_copy.path) / "agent" / "a.py"
    summary, _ = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    agent.write_text("A = 2\n")
    preview = await _accept(session, dry_run=True)
    facts, entries_for_state = session.facts, session_module._entries_for_state
    async def locked_facts(*args, **kwargs):
        assert session._mutation_lock.locked()
        result = await facts(*args, **kwargs)
        if move_inputs:
            agent.write_text("A = 3\n")
        return result
    def locked_inventory(*args, **kwargs):
        assert session._mutation_lock.locked()
        return entries_for_state(*args, **kwargs)
    monkeypatch.setattr(session, "facts", locked_facts)
    monkeypatch.setattr(session_module, "_entries_for_state", locked_inventory)
    digest = preview.facts[0]["to"]["inputs_digest"]
    if move_inputs:
        with pytest.raises(ProjectError) as caught:
            await _accept(session, expected=_expected(preview))
        assert caught.value.code == "inputs_changed"
        assert projects.store.inventory(digest) is None and projects.store.acceptances("acq") == ()
    else:
        record = await _accept(session, expected=_expected(preview))
        assert record.facts == preview.facts
        assert projects.store.inventory(record.facts[0]["to"]["inputs_digest"]) is not None


async def test_accept_expected_blocks_mutations_through_write(session_env, monkeypatch):
    import asyncio
    session, projects, _ = session_env
    await _stale_confirmations(session, projects)
    preview = await _accept(session, dry_run=True)
    confirmation = projects.store.confirmations("acq")[0]
    facts = session.facts
    entered, release, revoke_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def paused_facts(*args, **kwargs):
        result = await facts(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(session, "facts", paused_facts)
    writing = asyncio.create_task(_accept(session, expected=_expected(preview)))
    waiting = asyncio.create_task(entered.wait())
    await asyncio.wait((writing, waiting), return_when=asyncio.FIRST_COMPLETED)
    if not entered.is_set():
        waiting.cancel()
        await writing  # surface a failure before the facts read, rather than hanging
    async def revoke():
        revoke_attempted.set()
        return await session.revoke_confirmation("acq", confirmation.id, "changed")
    revoking = asyncio.create_task(revoke())
    try:
        await revoke_attempted.wait()
        assert not revoking.done()
        assert projects.store.confirmations("acq")[0].revoked is None
    finally:
        release.set()
        results = await asyncio.gather(writing, revoking, return_exceptions=True)
    assert not any(isinstance(value, BaseException) for value in results), results
    assert projects.store.acceptances("acq") == (results[0],)
    assert results[1].revoked is not None
