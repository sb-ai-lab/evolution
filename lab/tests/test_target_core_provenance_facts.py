"""Characterize the released Core b17 boundary, without the Lab backend."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.campaign_identity import CampaignIdentityV1, derived_identity
from evolution.tree.execution.interaction import TurnResult
from evolution.tree.execution.rollout import _ProvenanceUnion
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter
from evolution.tree.execution.wire_v2 import PREFIX, STRATEGY, validate_turn_provenance
from evolution.tree.target import target_server
from evolution.tree.target.target_server import BackendReply, TargetServer


class Session:
    effective_seed = None

    def __init__(self, provenance):
        self.provenance = provenance

    def turn(self, message):
        return BackendReply(TurnResult(({"type": "assistant", "content": "hello"},), None, 0, "ok", provenance=self.provenance))

    def close(self):
        pass


class Instance:
    def __init__(self, provenance=None):
        self.provenance = provenance

    def open(self, public, seed):
        return Session(self.provenance)

    def close(self):
        pass


class WithComponents(Instance):
    def __init__(self, components, provenance=None):
        super().__init__(provenance)
        self.value = components
        self.calls = 0

    def components(self):
        self.calls += 1
        return self.value


def server_for(tmp_path, instance, limit):
    root = tmp_path.resolve()
    (root / "candidate").mkdir(exist_ok=True)
    policy = ArtifactPolicyV1((), (), ())
    server = TargetServer(SimpleNamespace(load=lambda tree: instance), allowed_artifact_root=root,
                          policy=policy, seed_mode="none", target_id="synthetic",
                          contract_digest="contract", settings_hash="settings",
                          runtime_ref_digest="runtime", max_output_bytes=limit)
    def post(path, body):
        return server.dispatch("POST", PREFIX + path, json.dumps(body).encode())
    def create():
        return post("/instances", {"request_id": "create", "instance_id": "run-0123456789abcdef0123456789abcdef", "candidate_root": str(root / "candidate"), "policy": policy.to_payload(), "strategy": STRATEGY})
    return server, post, create


def test_turn_result_validates_provenance():
    result = TurnResult((), None, 0, "ok", provenance={"used": ["skills/A.md"]})
    assert dict(result.provenance) == dict(validate_turn_provenance({"used": ["skills/A.md"]}))
    with pytest.raises(ValueError):
        TurnResult((), None, 0, "ok", provenance={"used": "skills/A.md"})


def test_turn_provenance_counts_in_canonical_response_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(target_server, "time", SimpleNamespace(monotonic=lambda: 0))
    # The identical answer is 112 bytes without provenance and 150 with it.
    for name, provenance, limit, expected in [("plain", None, 112, 200), ("with", {"used": ["skills/A.md"]}, 112, 500), ("large", {"used": ["skills/A.md"]}, 1000, 200)]:
        root = tmp_path / name
        root.mkdir()
        server, post, create = server_for(root, Instance(provenance), limit)
        try:
            status, body = create()
            assert status == 200
            iid = json.loads(body)["instance_id"]
            status, body = post(f"/instances/{iid}/sessions", {"request_id": "open", "public": {}, "seed": None})
            assert status == 200
            sid = json.loads(body)["session_id"]
            status, body = post(f"/instances/{iid}/sessions/{sid}/turns", {"request_id": "turn", "message": {"content": "x", "fields": {}}})
            assert status == expected
            if status == 200:
                assert len(body) == (112 if provenance is None else 150)
                assert json.loads(body).get("provenance") == provenance
            else:
                assert json.loads(body)["error"]["code"] == "internal"
        finally:
            server.close()


def test_components_are_in_creation_response_and_count_toward_limit(tmp_path):
    components = {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}}
    root = tmp_path / "large"
    root.mkdir()
    inst = WithComponents(components)
    server, _, create = server_for(root, inst, 10000)
    try:
        status, body = create()
        assert status == 200
        assert json.loads(body)["components"] == components
        assert inst.calls == 1
        size = len(body)
        assert size == 662
    finally:
        server.close()
    root = tmp_path / "small"
    root.mkdir()
    server, _, create = server_for(root, WithComponents(components), size - 1)
    try:
        status, body = create()
        assert status == 500
        assert json.loads(body)["error"]["code"] == "internal"
    finally:
        server.close()


def test_missing_components_is_omitted_but_none_is_invalid(tmp_path):
    for name, instance, expected in [("absent", Instance(), 200), ("none", WithComponents(None), 500)]:
        root = tmp_path / name
        root.mkdir()
        server, _, create = server_for(root, instance, 10000)
        try:
            status, body = create()
            assert status == expected
            if status == 200:
                assert "components" not in json.loads(body)
            else:
                assert json.loads(body)["error"]["code"] == "internal"
        finally:
            server.close()


def test_rollout_union_and_journal_marker():
    union = _ProvenanceUnion()
    union.add(["skills/A.md", "skills/B.md"])
    union.add(["skills/B.md", "skills/C.md"])
    assert union.event_fields() == {"used": ["skills/A.md", "skills/B.md", "skills/C.md"]}
    fields = union.event_fields()
    union.add(None)
    assert union.event_fields() == {"used": None}
    # campaign_identity.derived_identity marks CampaignPolicyV11-derived rows;
    # evaluation.evaluate constructs RunIdentityV1 without that marker.
    class CapturingJournal:
        secrets = ()  # b18: NativeJournal names the run secrets the rollout screens provenance against

        def __init__(self):
            self.records = []

        def append(self, event, payload, *, operation_id):
            self.records.append((event, payload))
            return len(self.records)

    event = {"event": "rollout_finished", "operation_id": "op", "case_id": "case", "sample_id": "sample", "candidate_digest": "a" * 64, "terminal": "ok", **fields}
    campaign, evaluation = CapturingJournal(), CapturingJournal()
    JournalRolloutAdapter(campaign, row_provenance=True).append(event)
    JournalRolloutAdapter(evaluation).append(event)
    assert campaign.records[0][1]["provenance"] == event["used"]
    assert "provenance" not in evaluation.records[0][1]


def test_campaign_measurement_identity_enables_row_provenance():
    # Start from a recorded campaign's inputs, and ask b17 to derive its
    # measurement identity anew. No Lab backend or campaign execution is used.
    path = Path(__file__).parent / "fixtures/suspension_evidence/final-fault/campaign.jsonl"
    campaign_payload = json.loads(path.read_text().splitlines()[0])["payload"]["identity"]
    campaign = CampaignIdentityV1.from_payload(campaign_payload)
    measured = derived_identity(campaign, "a" * 64, "champion")
    assert measured.row_provenance is True
    assert measured.to_payload()["row_provenance"] == 1
