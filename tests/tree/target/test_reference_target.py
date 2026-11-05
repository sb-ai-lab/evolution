"""The reference `/evolution/v2` target, exercised over real HTTP.

Every test drives a `ThreadingHTTPServer` on an ephemeral loopback port through
`http.client`, so the transport, the routing and the state machine are all under
test; the fixture shuts each server down and joins its thread, so no test leaks a
listener or a thread into the next one.
"""

import dataclasses
import http.client
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.execution.wire_v2 import (
    PREFIX,
    PROTOCOL_VERSION,
    REQUIRED_ATTESTS,
    parse_error,
    parse_health,
    parse_instance,
    parse_session,
    parse_turn,
)
from evolution.tree.target import reference_target_http
from evolution.tree.target.reference_target import (
    NEUTRAL_MISBEHAVE,
    NO_ATTESTED_SKILL,
    Misbehave,
    ReferenceTarget,
    serve,
)
from tests.reference_target_faults import FaultInjectingReferenceTarget
from tests.tree.artifacts.test_artifact_policy import write

pytestmark = pytest.mark.slow

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
OTHER_POLICY = ArtifactPolicyV1(("build/**",), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731


class Client:
    """A thin `http.client` wrapper: one connection per call, JSON in and out."""

    def __init__(self, target: ReferenceTarget, port: int) -> None:
        self.target = target
        self.port = port

    def raw(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, PREFIX + path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            conn.close()

    def call(self, method, path, body=None):
        status, _, data = self.raw(method, path, body)
        return status, json.loads(data.decode("utf-8"))

    def bytes_of(self, method, path, body=None):
        status, _, data = self.raw(method, path, body)
        return status, data


@pytest.fixture
def allowed(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "allowed"
    root.mkdir()
    return root


@pytest.fixture
def stores():
    opened = []
    yield opened
    for store in opened:
        store.close()


@pytest.fixture
def publish(allowed, tmp_path, stores):
    """Publish a real S2 champion under the allowed root and return its path."""

    def _publish(name, skill=b"champion skill\nsecond line\n"):
        seed_root = Path(os.path.realpath(tmp_path)) / f"seed-{name}"
        write(seed_root, "skills/SKILL.md", skill)
        write(seed_root, "scripts/run.sh", b"#!/bin/sh\n", executable=True)
        write(seed_root, "uv.lock", b"lock")
        store = ArtifactStore.create(
            allowed / name, seed_root, POLICY, parallel_attempts=1, version_resolver=VR
        )
        stores.append(store)
        return store.champion().root

    return _publish


@pytest.fixture
def make_target(allowed):
    running = []

    def _make(**kwargs):
        kwargs.setdefault("policy", POLICY)
        target = FaultInjectingReferenceTarget(allowed_artifact_root=allowed, **kwargs)
        server = serve(target)
        # `poll_interval=0.01`: the stdlib default of 0.5 s is pure dead time in
        # `shutdown()`, and this suite starts a target per test.
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        running.append((server, thread))
        return Client(target, server.server_address[1])

    yield _make
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()


def create_body(instance_id, root, request_id=..., policy=POLICY, strategy=None):
    return dict(
        request_id=f"req-{instance_id}" if request_id is ... else request_id,
        instance_id=instance_id,
        candidate_root=str(root),
        policy=policy.to_payload(),
        strategy="artifact_root_injection" if strategy is None else strategy,
    )


def error_of(body):
    return body["error"]["code"]


# -- health -------------------------------------------------------------------


def test_health_reports_the_spec_body(make_target, allowed):
    client = make_target(seed_mode="unpaired", max_instances=3, max_concurrent_sessions=5)
    status, content_type, data = client.raw("GET", "/health")
    assert status == 200
    assert content_type == "application/json"
    body = json.loads(data)
    health = parse_health(body)
    assert health.ready is True
    assert health.protocol_version == PROTOCOL_VERSION == "2"
    assert health.target_id == "reference"
    assert health.boot_id == client.target.boot_id and len(health.boot_id) == 32
    assert health.contract_digest == "reference-contract-1"
    assert health.settings_hash == "reference-settings-1"
    assert health.runtime_ref_digest == "reference-runtime-1"
    assert health.seed_mode == "unpaired"
    assert health.allowed_artifact_root == str(allowed)
    assert Path(health.allowed_artifact_root).is_absolute()
    assert health.policy_digest == POLICY.digest
    assert health.instances == ()
    caps = health.capabilities
    assert caps.max_instances == 3 and caps.max_concurrent_sessions == 5
    assert caps.materialization == ("artifact_root_injection",)
    assert caps.attests == REQUIRED_ATTESTS
    assert caps.instantiable is True
    assert caps.max_input_bytes == 1 << 20 and caps.max_output_bytes == 1 << 20
    assert set(body) == {
        "ready",
        "protocol_version",
        "target_id",
        "boot_id",
        "contract_digest",
        "settings_hash",
        "runtime_ref_digest",
        "seed_mode",
        "allowed_artifact_root",
        "policy_digest",
        "capabilities",
        "instances",
    }


def test_boot_id_is_fresh_per_target(make_target):
    first, second = make_target(), make_target()
    assert first.target.boot_id != second.target.boot_id


# -- instance creation --------------------------------------------------------


def test_create_attests_the_scan_digests_and_the_epoch(make_target, publish):
    client = make_target()
    root = publish("run-a")
    scan = scan_tree(root, POLICY)
    status, body = client.call("POST", "/instances", create_body("i-1", root))
    assert status == 200
    identity = parse_instance(body)
    assert identity.instance_id == "i-1"
    assert identity.loaded_candidate_digest == scan.candidate_digest
    assert identity.dependency_digest == scan.dependency_digest
    assert identity.file_count == scan.file_count and identity.byte_count == scan.byte_count
    assert identity.attested == REQUIRED_ATTESTS
    assert identity.strategy == "artifact_root_injection"
    _, health_body = client.call("GET", "/health")
    health = parse_health(health_body)
    for field in (
        "contract_digest",
        "settings_hash",
        "runtime_ref_digest",
        "seed_mode",
        "boot_id",
    ):
        assert getattr(identity, field) == getattr(health, field)
    assert identity.protocol_version == health.protocol_version
    assert health.instances == ("i-1",)
    status, get_body = client.call("GET", "/instances/i-1")
    assert status == 200 and get_body == body


def test_two_instances_are_independent_and_listed_sorted(make_target, publish):
    client = make_target()
    a, b = publish("run-a", b"alpha skill\n"), publish("run-b", b"beta skill\n")
    assert client.call("POST", "/instances", create_body("i-2", a))[0] == 200
    assert client.call("POST", "/instances", create_body("i-1", b))[0] == 200
    first = parse_instance(client.call("GET", "/instances/i-2")[1])
    second = parse_instance(client.call("GET", "/instances/i-1")[1])
    assert first.loaded_candidate_digest != second.loaded_candidate_digest
    assert parse_health(client.call("GET", "/health")[1]).instances == ("i-1", "i-2")


def test_create_is_byte_identical_on_replay(make_target, publish):
    client = make_target()
    root = publish("run-a")
    body = create_body("i-1", root)
    first_status, first = client.bytes_of("POST", "/instances", body)
    second_status, second = client.bytes_of("POST", "/instances", body)
    assert first_status == second_status == 200
    assert first == second
    # Key order in the request does not change the canonical replay key.
    shuffled = dict(reversed(list(body.items())))
    assert client.bytes_of("POST", "/instances", shuffled)[1] == first


def test_create_with_the_same_request_id_and_a_different_body_conflicts(make_target, publish):
    client = make_target()
    a, b = publish("run-a"), publish("run-b")
    assert client.call("POST", "/instances", create_body("i-1", a, request_id="r"))[0] == 200
    status, body = client.call("POST", "/instances", create_body("i-2", b, request_id="r"))
    assert status == 409 and error_of(body) == "request_conflict"


def test_create_with_the_same_instance_id_and_a_different_root_conflicts(make_target, publish):
    client = make_target()
    a, b = publish("run-a"), publish("run-b")
    assert client.call("POST", "/instances", create_body("i-1", a, request_id="r1"))[0] == 200
    status, body = client.call("POST", "/instances", create_body("i-1", b, request_id="r2"))
    assert status == 409 and error_of(body) == "instance_conflict"
    # The same instance id with the same root and policy is idempotent across ids.
    status, again = client.call("POST", "/instances", create_body("i-1", a, request_id="r3"))
    assert status == 200 and parse_instance(again).instance_id == "i-1"


def test_a_repeat_creation_under_a_new_request_id_is_recorded(make_target, publish):
    """ADR-0007: every POST is idempotent on its `request_id` while the owning
    instance is live. A second id that resolved to the same instance is a served
    request like any other, so reusing it for a different creation is 409 — not
    a second instance."""
    client = make_target()
    a, b = publish("run-a"), publish("run-b")
    first_status, first = client.bytes_of("POST", "/instances", create_body("i-1", a, "r1"))
    assert first_status == 200

    alias_status, alias = client.bytes_of("POST", "/instances", create_body("i-1", a, "r2"))
    assert alias_status == 200 and alias == first
    # `r2` now replays byte-identically, exactly as `r1` does.
    assert client.bytes_of("POST", "/instances", create_body("i-1", a, "r2"))[1] == alias

    status, body = client.call("POST", "/instances", create_body("i-2", b, "r2"))
    assert status == 409 and error_of(body) == "request_conflict"
    assert parse_health(client.call("GET", "/health")[1]).instances == ("i-1",)


def test_a_recorded_alias_request_id_does_not_survive_the_instance(make_target, publish):
    """The contract scopes a creation record to the instance it created, so releasing the
    instance drops every id that reached it, the aliases included."""
    client = make_target()
    a, b = publish("run-a"), publish("run-b")
    assert client.call("POST", "/instances", create_body("i-1", a, "r1"))[0] == 200
    assert client.call("POST", "/instances", create_body("i-1", a, "r2"))[0] == 200
    assert client.call("DELETE", "/instances/i-1")[0] == 200

    assert client.call("POST", "/instances", create_body("i-2", b, "r2"))[0] == 200


def test_create_replays_after_the_published_root_moved_away(make_target, publish):
    """The contract replays a served `request_id` "without re-executing anything", so the
    record is consulted before the mutable resource is validated: renaming the
    published copy cannot turn a recorded 200 into a 403 `root_not_allowed`."""
    client = make_target()
    root = publish("run-a")
    body = create_body("i-1", root, request_id="r1")
    status, first = client.bytes_of("POST", "/instances", body)
    assert status == 200
    root.rename(root.parent / "moved-away")

    status, again = client.bytes_of("POST", "/instances", body)

    assert status == 200 and again == first


def test_a_recorded_request_id_with_an_out_of_root_root_is_a_request_conflict(
    make_target, publish, tmp_path
):
    """The record decides first, so reusing a served `request_id` for a different
    body is 409 `request_conflict` — never the refusal the new body would earn."""
    client = make_target()
    root = publish("run-a")
    assert client.call("POST", "/instances", create_body("i-1", root, request_id="r1"))[0] == 200
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()

    status, response = client.call("POST", "/instances", create_body("i-1", outside, "r1"))

    assert status == 409 and error_of(response) == "request_conflict"


def test_nonfinite_json_constants_in_a_body_are_an_invalid_request(make_target, publish):
    """`NaN` parses as JSON but cannot be canonicalized; that is the request's
    fault (400), not the target's (500)."""
    client = make_target()
    root = publish("run-a")
    body = create_body("i-1", root, request_id="r1")
    raw = json.dumps(body).replace('"r1"', "NaN").encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
    try:
        conn.request(
            "POST", PREFIX + "/instances", body=raw, headers={"Content-Type": "application/json"}
        )
        reply = conn.getresponse()
        status, response = reply.status, json.loads(reply.read().decode("utf-8"))
    finally:
        conn.close()

    assert status == 400 and error_of(response) == "invalid_request"


def test_root_outside_the_allowed_root_is_refused(make_target, tmp_path):
    client = make_target()
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    status, _, data = client.raw("POST", "/instances", create_body("i-1", outside))
    assert status == 403 and error_of(json.loads(data)) == "root_not_allowed"
    # The refused path never appears in the message.
    assert b"outside" not in data


def test_relative_root_and_dotdot_segments_are_refused(make_target, publish, allowed):
    client = make_target()
    root = publish("run-a")
    status, body = client.call("POST", "/instances", create_body("i-1", Path("relative/path")))
    assert status == 403 and error_of(body) == "root_not_allowed"
    escaped = f"{allowed}/run-a/../../allowed/run-a"
    status, body = client.call("POST", "/instances", create_body("i-2", escaped))
    assert status == 403 and error_of(body) == "root_not_allowed"
    inner = f"{root}/../{root.name}"
    status, body = client.call("POST", "/instances", create_body("i-3", inner))
    assert status == 403 and error_of(body) == "root_not_allowed"


def test_symlinked_component_below_the_allowed_root_is_refused(make_target, publish, allowed):
    client = make_target()
    root = publish("run-a")
    link = allowed / "link"
    link.symlink_to(allowed / "run-a", target_is_directory=True)
    status, body = client.call("POST", "/instances", create_body("i-1", link))
    assert status == 403 and error_of(body) == "root_not_allowed"
    # The same published root reached without the symlink is accepted.
    assert client.call("POST", "/instances", create_body("i-2", root))[0] == 200
    # A symlinked *component* in the middle of the path is refused too.
    through = Path(str(link) + str(root)[len(str(allowed / "run-a")) :])
    status, body = client.call("POST", "/instances", create_body("i-3", through))
    assert status == 403 and error_of(body) == "root_not_allowed"


def test_missing_root_and_file_root_are_refused(make_target, allowed):
    client = make_target()
    plain = allowed / "plain.txt"
    plain.write_text("x")
    status, body = client.call("POST", "/instances", create_body("i-1", plain))
    assert status == 403 and error_of(body) == "root_not_allowed"
    status, body = client.call("POST", "/instances", create_body("i-2", allowed / "absent"))
    assert status == 403 and error_of(body) == "root_not_allowed"


def test_policy_mismatch_and_malformed_policy(make_target, publish):
    client = make_target()
    root = publish("run-a")
    status, body = client.call("POST", "/instances", create_body("i-1", root, policy=OTHER_POLICY))
    assert status == 403 and error_of(body) == "policy_mismatch"
    broken = create_body("i-2", root)
    broken["policy"] = {"version": 1, "excludes": ["../escape"]}
    status, body = client.call("POST", "/instances", broken)
    assert status == 400 and error_of(body) == "invalid_request"
    broken["policy"] = "not-an-object"
    assert client.call("POST", "/instances", broken)[1]["error"]["code"] == "invalid_request"


def test_policy_is_checked_before_containment(make_target, tmp_path):
    """Ruling 4's order: a request that is wrong on both axes reports the policy."""
    client = make_target()
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    body = create_body("i-1", outside, policy=OTHER_POLICY)
    status, response = client.call("POST", "/instances", body)
    assert status == 403 and error_of(response) == "policy_mismatch"


def test_unsupported_strategy_is_refused_before_the_policy(make_target, publish):
    client = make_target()
    root = publish("run-a")
    body = create_body("i-1", root, policy=OTHER_POLICY, strategy="container_exec")
    status, response = client.call("POST", "/instances", body)
    assert status == 400 and error_of(response) == "invalid_request"


def test_malformed_bodies_and_identifiers_are_refused(make_target, publish):
    client = make_target()
    root = publish("run-a")
    extra = create_body("i-1", root) | {"extra": 1}
    assert client.call("POST", "/instances", extra)[0] == 400
    missing = create_body("i-1", root)
    del missing["policy"]
    assert client.call("POST", "/instances", missing)[0] == 400
    bad_id = create_body("bad/id", root)
    status, body = client.call("POST", "/instances", bad_id)
    assert status == 400 and error_of(body) == "invalid_request"
    bad_request_id = create_body("i-1", root, request_id="")
    assert client.call("POST", "/instances", bad_request_id)[0] == 400
    numeric_root = create_body("i-1", root)
    numeric_root["candidate_root"] = 7
    assert client.call("POST", "/instances", numeric_root)[0] == 400


def test_unsafe_candidate_tree_is_an_invalid_request(make_target, allowed):
    client = make_target()
    root = allowed / "hand-made"
    write(root, "skills/SKILL.md", b"hand\n")
    (root / "loop").symlink_to(root / "skills", target_is_directory=True)
    status, body = client.call("POST", "/instances", create_body("i-1", root))
    assert status == 400 and error_of(body) == "invalid_request"


def test_symlinked_skill_file_is_not_followed(make_target, allowed):
    """A policy that excludes `skills/**` never scans it, so the answer must
    come from the attested artifact set, not from a policy-blind read that
    would follow the symlink onto bytes no digest covers."""
    policy = ArtifactPolicyV1(("skills/**",), (), ())
    client = make_target(policy=policy)
    root = allowed / "handmade"
    write(root, "real/SKILL.md", b"secret first line\n")
    (root / "skills").mkdir()
    (root / "skills/SKILL.md").symlink_to(root / "real/SKILL.md")
    assert client.call("POST", "/instances", create_body("i-1", root, policy=policy))[0] == 200
    session = parse_session(open_session(client)[1])
    _, body = send_turn(client, session.session_id, content="q")
    assert parse_turn(body).events[0]["content"] == f"{NO_ATTESTED_SKILL} | q"


def test_excluded_skill_answers_the_fixed_constant_with_identical_digests(make_target, allowed):
    """With a policy excluding `skills/**`, two trees differing only in
    `skills/SKILL.md` are indistinguishable — same digests, same turn answer —
    because the skill line is derived from the attested artifact set, never
    from excluded bytes."""
    policy = ArtifactPolicyV1(("skills/**",), (), ())
    client = make_target(policy=policy)
    root_a = allowed / "tree-a"
    write(root_a, "skills/SKILL.md", b"alpha first line\n")
    root_b = allowed / "tree-b"
    write(root_b, "skills/SKILL.md", b"bravo first line\n")
    _, body_a = client.call("POST", "/instances", create_body("i-a", root_a, policy=policy))
    _, body_b = client.call("POST", "/instances", create_body("i-b", root_b, policy=policy))
    identity_a, identity_b = parse_instance(body_a), parse_instance(body_b)
    assert identity_a.loaded_candidate_digest == identity_b.loaded_candidate_digest
    assert identity_a.dependency_digest == identity_b.dependency_digest

    def answer(instance_id):
        _, session_body = client.call(
            "POST",
            f"/instances/{instance_id}/sessions",
            dict(request_id=f"s-{instance_id}", public={}, seed=7),
        )
        session = parse_session(session_body)
        _, turn_body = client.call(
            "POST",
            f"/instances/{instance_id}/sessions/{session.session_id}/turns",
            dict(request_id=f"t-{instance_id}", message={"content": "q", "fields": {}}),
        )
        return parse_turn(turn_body).events[0]["content"]

    expected = f"{NO_ATTESTED_SKILL} | q"
    assert answer("i-a") == expected
    assert answer("i-b") == expected


def test_dependency_classified_skill_is_attested_and_answers_its_first_line(make_target, allowed):
    """PDR-0004: the turn answer is the first line of `skills/SKILL.md` whenever
    that file is attested — by the candidate digest or, as here, by the
    dependency digest. A policy with `dependency_paths=["skills/SKILL.md"]`
    puts the file in `scan.dependency`, not `scan.artifact`; it must still be
    read, and two trees differing only in it must diverge on both the
    dependency digest and the turn answer."""
    policy = ArtifactPolicyV1((), ("skills/SKILL.md",), ())
    client = make_target(policy=policy)
    root_a = allowed / "tree-a"
    write(root_a, "skills/SKILL.md", b"alpha first line\n")
    root_b = allowed / "tree-b"
    write(root_b, "skills/SKILL.md", b"bravo first line\n")
    _, body_a = client.call("POST", "/instances", create_body("i-a", root_a, policy=policy))
    _, body_b = client.call("POST", "/instances", create_body("i-b", root_b, policy=policy))
    identity_a, identity_b = parse_instance(body_a), parse_instance(body_b)
    assert identity_a.dependency_digest != identity_b.dependency_digest

    def answer(instance_id):
        _, session_body = client.call(
            "POST",
            f"/instances/{instance_id}/sessions",
            dict(request_id=f"s-{instance_id}", public={}, seed=7),
        )
        session = parse_session(session_body)
        _, turn_body = client.call(
            "POST",
            f"/instances/{instance_id}/sessions/{session.session_id}/turns",
            dict(request_id=f"t-{instance_id}", message={"content": "q", "fields": {}}),
        )
        return parse_turn(turn_body).events[0]["content"]

    assert answer("i-a") == "alpha first line | q"
    assert answer("i-b") == "bravo first line | q"


def test_instances_exhausted(make_target, publish):
    client = make_target(max_instances=2)
    roots = [publish(f"run-{n}") for n in range(3)]
    assert client.call("POST", "/instances", create_body("i-0", roots[0]))[0] == 200
    assert client.call("POST", "/instances", create_body("i-1", roots[1]))[0] == 200
    status, body = client.call("POST", "/instances", create_body("i-2", roots[2]))
    assert status == 429 and error_of(body) == "instances_exhausted"
    assert body["error"]["retryable"] is True
    # Releasing one frees the slot.
    assert client.call("DELETE", "/instances/i-0")[1] == {"instance_id": "i-0", "released": True}
    assert client.call("POST", "/instances", create_body("i-2", roots[2]))[0] == 200


# -- sessions and turns -------------------------------------------------------


@pytest.fixture
def instance(make_target, publish):
    def _instance(skill=b"champion skill\nsecond line\n", **kwargs):
        client = make_target(**kwargs)
        root = publish("run-a", skill)
        assert client.call("POST", "/instances", create_body("i-1", root))[0] == 200
        return client

    return _instance


def open_session(client, request_id="s1", seed=7, public=None):
    return client.call(
        "POST",
        "/instances/i-1/sessions",
        dict(
            request_id=request_id, public=public if public is not None else {"case": 1}, seed=seed
        ),
    )


def send_turn(client, session_id, request_id="t1", content="hello"):
    return client.call(
        "POST",
        f"/instances/i-1/sessions/{session_id}/turns",
        dict(request_id=request_id, message={"content": content, "fields": {}}),
    )


def test_session_and_turn_answer_with_the_skill_first_line(instance):
    client = instance()
    status, body = open_session(client)
    assert status == 200
    session = parse_session(body)
    assert session.requested_seed == 7 and session.effective_seed == 7
    status, turn_body = send_turn(client, session.session_id, content="hello")
    assert status == 200
    turn = parse_turn(turn_body)
    assert turn.outcome == "ok" and turn.truncated is False
    assert turn.events == ({"type": "assistant_message", "content": "champion skill | hello"},)
    assert turn.latency_ms >= 0
    assert turn.usage is not None
    assert turn.usage.prompt == len("hello")
    assert turn.usage.completion == len("champion skill | hello")
    assert turn.usage.cached == 0
    assert turn.usage.total == turn.usage.prompt + turn.usage.completion


def test_turn_reports_the_skill_file_as_used(instance):
    client = instance()
    _, body = open_session(client)
    session = parse_session(body)
    _, turn_body = send_turn(client, session.session_id, content="hello")
    assert turn_body["provenance"] == {"used": ["skills/SKILL.md"]}


def test_unattested_skill_reports_nothing_used(make_target, allowed):
    policy = ArtifactPolicyV1(("skills/**",), (), ())
    client = make_target(policy=policy)
    root = allowed / "tree-a"
    write(root, "skills/SKILL.md", b"alpha first line\n")
    assert client.call("POST", "/instances", create_body("i-a", root, policy=policy))[0] == 200
    _, session_body = client.call(
        "POST", "/instances/i-a/sessions", dict(request_id="s-a", public={}, seed=7)
    )
    session = parse_session(session_body)
    _, turn_body = client.call(
        "POST",
        f"/instances/i-a/sessions/{session.session_id}/turns",
        dict(request_id="t-a", message={"content": "q", "fields": {}}),
    )
    assert turn_body["provenance"] == {"used": []}


def test_attested_skill_that_fails_to_read_reports_nothing_used(make_target, publish, monkeypatch):
    client = make_target()
    root = publish("run-a")
    real = Path.read_text

    def failing(self, *args, **kwargs):
        if self.name == "SKILL.md":
            raise OSError("boom")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)
    assert client.call("POST", "/instances", create_body("i-1", root))[0] == 200
    _, session_body = client.call(
        "POST", "/instances/i-1/sessions", dict(request_id="s1", public={}, seed=7)
    )
    session = parse_session(session_body)
    _, turn_body = send_turn(client, session.session_id, content="q")
    assert turn_body["provenance"] == {"used": []}
    assert turn_body["events"][0]["content"] == f"{NO_ATTESTED_SKILL} | q"


def test_reference_instance_declares_no_components(make_target, publish):
    client = make_target()
    root = publish("run-a")
    status, body = client.call("POST", "/instances", create_body("i-1", root))
    assert status == 200
    assert "components" not in body


def test_a_different_candidate_is_observably_different(make_target, publish):
    client = make_target()
    champion = publish("run-a", b"champion skill\n")
    candidate = publish("run-b", b"candidate skill\n")
    for name, root in (("i-1", champion), ("i-2", candidate)):
        assert client.call("POST", "/instances", create_body(name, root))[0] == 200
    answers = []
    for name in ("i-1", "i-2"):
        _, session = client.call(
            "POST", f"/instances/{name}/sessions", dict(request_id="s", public={}, seed=1)
        )
        sid = session["session_id"]
        _, turn = client.call(
            "POST",
            f"/instances/{name}/sessions/{sid}/turns",
            dict(request_id="t", message={"content": "q", "fields": {}}),
        )
        answers.append(turn["events"][0]["content"])
    assert answers == ["champion skill | q", "candidate skill | q"]


def test_truncation_sets_the_flag_and_cuts_the_answer(instance):
    client = instance(max_answer_chars=20)
    session = parse_session(open_session(client)[1])
    _, body = send_turn(client, session.session_id, content="x" * 100)
    turn = parse_turn(body)
    assert turn.truncated is True
    assert len(turn.events[0]["content"]) == 20
    assert turn.events[0]["content"] == "champion skill | xxxx"[:20]
    _, short = send_turn(client, session.session_id, request_id="t2", content="a")
    assert parse_turn(short).truncated is False


@pytest.mark.parametrize(
    "mode,seed,expected",
    [("exact", 11, (11, 11)), ("none", None, (None, None))],
)
def test_seed_modes(instance, mode, seed, expected):
    client = instance(seed_mode=mode)
    status, body = open_session(client, seed=seed)
    assert status == 200
    session = parse_session(body)
    assert (session.requested_seed, session.effective_seed) == expected


def test_exact_mode_requires_an_integer_seed(instance):
    client = instance(seed_mode="exact")
    status, body = open_session(client, seed=None)
    assert status == 400 and error_of(body) == "invalid_request"
    assert open_session(client, seed=True)[0] == 400


def test_none_mode_refuses_a_seed(instance):
    client = instance(seed_mode="none")
    status, body = open_session(client, seed=3)
    assert status == 400 and error_of(body) == "invalid_request"


def test_unpaired_mode_ignores_the_request_and_counts(instance):
    client = instance(seed_mode="unpaired")
    first = parse_session(open_session(client, request_id="s1", seed=99)[1])
    second = parse_session(open_session(client, request_id="s2", seed=99)[1])
    assert first.requested_seed == 99 and second.requested_seed == 99
    assert first.effective_seed != second.effective_seed
    assert {first.effective_seed, second.effective_seed} == {1, 2}


def test_session_and_turn_replays_are_byte_identical(instance):
    client = instance()
    body = dict(request_id="s1", public={"case": 1}, seed=7)
    first = client.bytes_of("POST", "/instances/i-1/sessions", body)
    second = client.bytes_of("POST", "/instances/i-1/sessions", body)
    assert first == second
    sid = json.loads(first[1])["session_id"]
    turn = dict(request_id="t1", message={"content": "hello", "fields": {"a": 1}})
    path = f"/instances/i-1/sessions/{sid}/turns"
    one = client.bytes_of("POST", path, turn)
    two = client.bytes_of("POST", path, turn)
    assert one == two
    # A second session request under the same id is not created.
    assert parse_health(client.call("GET", "/health")[1]).instances == ("i-1",)


def test_session_replay_precedes_the_seed_rule(instance):
    """The session handler consults its record before it judges the body: a seed
    the target would refuse today still replays the answer it gave yesterday."""
    client = instance(seed_mode="exact")
    body = dict(request_id="s1", public={"case": 1}, seed=7)
    status, first = client.bytes_of("POST", "/instances/i-1/sessions", body)
    assert status == 200
    # The same request is no longer a valid one under the target's live rule.
    client.target.seed_mode = "none"

    status, again = client.bytes_of("POST", "/instances/i-1/sessions", body)

    assert status == 200 and again == first
    # A request that reached no record is judged by the rule now in force.
    fresh = dict(request_id="s2", public={"case": 1}, seed=7)
    assert client.call("POST", "/instances/i-1/sessions", fresh)[0] == 400


def test_a_turn_names_the_released_instance_before_it_judges_the_body(instance):
    """The instance is resolved before the endpoint's own validation, because the
    record that may answer the request lives on it."""
    client = instance()
    session = parse_session(open_session(client)[1])
    assert client.call("DELETE", "/instances/i-1")[0] == 200

    status, body = client.call(
        "POST",
        f"/instances/i-1/sessions/{session.session_id}/turns",
        dict(request_id="t9", message={"content": 1, "fields": {}}),
    )

    assert status == 404 and error_of(body) == "unknown_instance"


def test_session_and_turn_request_conflicts(instance):
    client = instance()
    assert open_session(client, request_id="s1", seed=7)[0] == 200
    status, body = open_session(client, request_id="s1", seed=8)
    assert status == 409 and error_of(body) == "request_conflict"
    session = parse_session(open_session(client, request_id="s2", seed=7)[1])
    assert send_turn(client, session.session_id, request_id="t1", content="a")[0] == 200
    status, body = send_turn(client, session.session_id, request_id="t1", content="b")
    assert status == 409 and error_of(body) == "request_conflict"


def test_close_session_is_idempotent_and_closes_turns(instance):
    client = instance()
    session = parse_session(open_session(client)[1])
    sid = session.session_id
    status, body = client.call("DELETE", f"/instances/i-1/sessions/{sid}")
    assert status == 200 and body == {"session_id": sid, "closed": True}
    status, body = client.call("DELETE", f"/instances/i-1/sessions/{sid}")
    assert status == 200 and body == {"session_id": sid, "closed": False}
    status, body = send_turn(client, sid, request_id="t9")
    assert status == 404 and error_of(body) == "unknown_session"
    status, body = send_turn(client, "deadbeef", request_id="t8")
    assert status == 404 and error_of(body) == "unknown_session"


def test_unknown_instance_and_bad_identifiers_in_the_path(instance):
    client = instance()
    status, body = client.call("GET", "/instances/i-absent")
    assert status == 404 and error_of(body) == "unknown_instance"
    status, body = client.call(
        "POST", "/instances/i-absent/sessions", dict(request_id="s", public={}, seed=1)
    )
    assert status == 404 and error_of(body) == "unknown_instance"
    status, body = client.call("GET", "/instances/bad!id")
    assert status == 400 and error_of(body) == "invalid_request"
    status, body = client.call(
        "POST",
        "/instances/i-1/sessions/bad!id/turns",
        dict(request_id="t", message={"content": "a", "fields": {}}),
    )
    assert status == 400 and error_of(body) == "invalid_request"


def test_turn_body_validation(instance):
    client = instance()
    session = parse_session(open_session(client)[1])
    path = f"/instances/i-1/sessions/{session.session_id}/turns"
    for body in (
        dict(request_id="t", message={"content": "a"}),
        dict(request_id="t", message={"content": 1, "fields": {}}),
        dict(request_id="t", message={"content": "a", "fields": []}),
        dict(request_id="t", message="a"),
        dict(request_id="t"),
        dict(request_id="t", message={"content": "a", "fields": {}}, extra=1),
    ):
        assert client.call("POST", path, body)[0] == 400
    for body in (
        dict(request_id="s", public=[], seed=1),
        dict(request_id="s", public={}),
        dict(request_id="s", public={}, seed="1"),
    ):
        assert client.call("POST", "/instances/i-1/sessions", body)[0] == 400


# -- release, tombstones and records -----------------------------------------


def test_release_closes_sessions_and_drops_records(instance):
    client = instance()
    session = parse_session(open_session(client)[1])
    assert send_turn(client, session.session_id)[0] == 200
    status, body = client.call("DELETE", "/instances/i-1")
    assert status == 200 and body == {"instance_id": "i-1", "released": True}
    status, body = client.call("DELETE", "/instances/i-1")
    assert status == 200 and body == {"instance_id": "i-1", "released": False}
    status, body = send_turn(client, session.session_id, request_id="t2")
    assert status == 404 and error_of(body) == "unknown_instance"
    status, body = client.call("GET", "/instances/i-1")
    assert status == 404 and error_of(body) == "unknown_instance"
    assert parse_health(client.call("GET", "/health")[1]).instances == ()
    status, body = client.call("DELETE", "/instances/i-absent")
    assert status == 200 and body == {"instance_id": "i-absent", "released": False}


def test_creation_replay_after_release_is_expired(make_target, publish):
    client = make_target()
    root = publish("run-a")
    body = create_body("i-1", root, request_id="r1")
    assert client.call("POST", "/instances", body)[0] == 200
    assert client.call("DELETE", "/instances/i-1")[0] == 200
    status, response = client.call("POST", "/instances", body)
    assert status == 410 and error_of(response) == "request_expired"
    fresh = create_body("i-1", root, request_id="r2")
    status, response = client.call("POST", "/instances", fresh)
    assert status == 410 and error_of(response) == "request_expired"


def test_tombstones_are_bounded(make_target, publish):
    client = make_target(tombstones=2)
    root = publish("run-a")
    for name in ("i-1", "i-2", "i-3"):
        assert client.call("POST", "/instances", create_body(name, root))[0] == 200
        assert client.call("DELETE", f"/instances/{name}")[0] == 200
    assert client.call("POST", "/instances", create_body("i-3", root, request_id="x3"))[0] == 410
    assert client.call("POST", "/instances", create_body("i-2", root, request_id="x2"))[0] == 410
    # `i-1` fell off the bounded list, so it is treated as a new id.
    status, body = client.call("POST", "/instances", create_body("i-1", root, request_id="x1"))
    assert status == 200 and parse_instance(body).instance_id == "i-1"


def test_records_do_not_survive_the_instance(make_target, publish):
    client = make_target()
    root = publish("run-a")
    assert client.call("POST", "/instances", create_body("i-1", root, request_id="r1"))[0] == 200
    session = parse_session(open_session(client, request_id="s1")[1])
    assert send_turn(client, session.session_id, request_id="t1")[0] == 200
    assert client.call("DELETE", "/instances/i-1")[0] == 200
    assert client.call("POST", "/instances", create_body("i-2", root, request_id="r9"))[0] == 200
    # The old session id and the old request ids mean nothing under the new instance.
    status, body = client.call(
        "POST",
        f"/instances/i-2/sessions/{session.session_id}/turns",
        dict(request_id="t1", message={"content": "a", "fields": {}}),
    )
    assert status == 404 and error_of(body) == "unknown_session"


# -- transport ----------------------------------------------------------------


def test_oversized_request_is_refused_before_the_body(make_target, publish):
    client = make_target(max_input_bytes=2048)
    root = publish("run-a")
    body = create_body("i-1", root)
    body["padding"] = "p" * 4096
    status, data = client.bytes_of("POST", "/instances", body)
    assert status == 413
    assert error_of(json.loads(data)) == "invalid_request"
    assert client.call("GET", "/health")[0] == 200


def _oversized_head(length, port_path=f"{PREFIX}/instances", value=None):
    declared = str(length) if value is None else value
    return (
        f"POST {port_path} HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
        f"Content-Length: {declared}\r\n\r\n"
    ).encode()


def _read_all(sock):
    answer = b""
    while chunk := sock.recv(65536):
        answer += chunk
    return answer


def test_an_oversized_request_is_answered_and_drained_not_reset(make_target):
    """The refusal reaches a client that writes its whole body first."""
    client = make_target(max_input_bytes=2048)
    body = b"x" * (32 << 20)
    with socket.create_connection(("127.0.0.1", client.port), timeout=20) as sock:
        sock.sendall(_oversized_head(len(body)))
        sock.sendall(body)  # 0.3.0a28: BrokenPipeError / ConnectionResetError here
        sock.shutdown(socket.SHUT_WR)
        answer = _read_all(sock)
    head, _, payload = answer.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413 ")
    assert json.loads(payload)["error"]["code"] == "invalid_request"
    assert client.call("GET", "/health")[0] == 200


def test_an_invalid_content_length_is_answered_and_drained_not_reset(make_target):
    client = make_target()
    body = b"x" * (32 << 20)
    with socket.create_connection(("127.0.0.1", client.port), timeout=20) as sock:
        sock.sendall(_oversized_head(0, value="abc"))
        sock.sendall(body)
        sock.shutdown(socket.SHUT_WR)
        answer = _read_all(sock)
    head, _, payload = answer.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400 ")
    assert json.loads(payload)["error"]["code"] == "invalid_request"


def test_the_drain_stops_after_its_byte_bound(make_target, monkeypatch):
    """Past the bound the server closes as before; the answer was sent first."""
    monkeypatch.setattr(reference_target_http, "_DRAIN_BYTES", 1 << 16)
    client = make_target(max_input_bytes=2048)
    body = b"x" * (32 << 20)
    with socket.create_connection(("127.0.0.1", client.port), timeout=20) as sock:
        sock.sendall(_oversized_head(len(body)))
        with pytest.raises(ConnectionError):
            sock.sendall(body)
        assert sock.recv(65536).startswith(b"HTTP/1.1 413 ")


def test_the_drain_stops_after_its_time_bound(make_target, monkeypatch):
    """A client that stops sending holds the handler for `_DRAIN_SECONDS` only."""
    monkeypatch.setattr(reference_target_http, "_DRAIN_SECONDS", 0.2)
    client = make_target(max_input_bytes=2048)
    with socket.create_connection(("127.0.0.1", client.port), timeout=20) as sock:
        sock.sendall(_oversized_head(1 << 20))
        assert _read_all(sock).startswith(b"HTTP/1.1 413 ")  # FIN after the answer
        time.sleep(0.6)  # past the drain bound: the server has closed
        with pytest.raises(ConnectionError):
            sock.sendall(b"x" * (32 << 20))


def test_invalid_json_and_unknown_routes(make_target):
    client = make_target()
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
    try:
        conn.request("POST", PREFIX + "/instances", body=b"{not json")
        response = conn.getresponse()
        assert response.status == 400
        assert error_of(json.loads(response.read())) == "invalid_request"
    finally:
        conn.close()
    for method, path in (
        ("GET", "/nowhere"),
        ("POST", "/health"),
        ("GET", "/instances"),
        ("PUT", "/instances/i-1"),
        ("DELETE", "/health"),
    ):
        status, body = client.call(method, path, {} if method in ("POST", "PUT") else None)
        assert status == 404 and error_of(body) == "invalid_request", (method, path)
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
    try:
        conn.request("GET", "/elsewhere/health")
        assert conn.getresponse().status == 404
    finally:
        conn.close()


def test_every_error_body_parses_as_a_closed_wire_error(make_target, publish):
    client = make_target()
    outside = publish("run-a").parent
    status, body = client.call("POST", "/instances", create_body("i-1", outside / "absent"))
    error = parse_error(status, body)
    assert error.code == "root_not_allowed" and error.retryable is False
    assert set(body["error"]) == {"code", "message", "retryable"}


# -- misbehave hooks ----------------------------------------------------------


def test_lie_candidate_digest_and_drop_attested(instance, publish):
    client = instance()
    honest = parse_instance(client.call("GET", "/instances/i-1")[1])
    client.target.fault(lie_candidate_digest=True)
    lied = parse_instance(client.call("GET", "/instances/i-1")[1])
    assert lied.loaded_candidate_digest != honest.loaded_candidate_digest
    assert lied.dependency_digest == honest.dependency_digest
    client.target.fault(lie_candidate_digest=False)
    client.target.fault(drop_attested=True)
    dropped = parse_instance(client.call("GET", "/instances/i-1")[1])
    assert dropped.attested == ()
    client.target.fault(drop_attested=False)
    assert parse_instance(client.call("GET", "/instances/i-1")[1]) == honest


def test_refuse_release_after_keeps_the_instance_alive(instance, publish):
    """The hook models the one target shape a probe cannot see in its own checks:
    releases are served for a while and then refused, so what the client asked to
    delete stays live. Session closes are not releases and are never counted."""
    client = instance()
    session = parse_session(open_session(client)[1])
    client.target.fault(refuse_release_after=1)
    assert client.call("DELETE", f"/instances/i-1/sessions/{session.session_id}")[0] == 200
    assert client.call("POST", "/instances", create_body("i-2", publish("run-b")))[0] == 200
    assert client.call("DELETE", "/instances/i-1")[0] == 200

    status, body = client.call("DELETE", "/instances/i-2")

    assert status == 500 and error_of(body) == "internal"
    assert parse_health(client.call("GET", "/health")[1]).instances == ("i-2",)


def test_keep_nth_instance_refuses_every_release_of_that_instance_only(instance, publish):
    """The hook models a target that keeps one particular instance: every
    `DELETE` of the N-th created one is refused, retries included, while the
    instances around it release normally. Counting is by creation order, so
    the hook is set before the kept instance exists."""
    client = instance()
    client.target.fault(keep_nth_instance=2)
    assert client.call("POST", "/instances", create_body("i-2", publish("run-b")))[0] == 200
    assert client.call("POST", "/instances", create_body("i-3", publish("run-c")))[0] == 200
    assert client.call("DELETE", "/instances/i-1")[0] == 200

    first = client.call("DELETE", "/instances/i-2")
    again = client.call("DELETE", "/instances/i-2")

    assert first[0] == 500 and error_of(first[1]) == "internal"
    assert again[0] == 500 and error_of(again[1]) == "internal"
    assert client.call("DELETE", "/instances/i-3")[0] == 200
    assert parse_health(client.call("GET", "/health")[1]).instances == ("i-2",)


def test_internal_error_hook_fails_every_request(instance):
    client = instance()
    client.target.fault(internal_error=True)
    for method, path in (("GET", "/health"), ("GET", "/instances/i-1"), ("GET", "/nowhere")):
        status, body = client.call(method, path)
        assert status == 500 and error_of(body) == "internal"
        assert body["error"]["retryable"] is True
    client.target.fault(internal_error=False)
    assert client.call("GET", "/health")[0] == 200


def test_sleep_hook_stalls_every_endpoint(instance, publish):
    """`callee_timeout` must be provable on create and health, not only on a turn."""
    client = instance()
    session = parse_session(open_session(client)[1])
    client.target.fault(sleep_seconds=0.1)
    started = time.monotonic()
    status, body = send_turn(client, session.session_id, request_id="t1")
    assert status == 200 and time.monotonic() - started >= 0.1
    # The reported latency is the latency the client saw, stall included.
    assert parse_turn(body).latency_ms >= 100
    started = time.monotonic()
    assert client.call("GET", "/health")[0] == 200
    assert time.monotonic() - started >= 0.1
    root = publish("run-b")
    started = time.monotonic()
    assert client.call("POST", "/instances", create_body("i-2", root))[0] == 200
    assert time.monotonic() - started >= 0.1
    client.target.fault(sleep_seconds=0.0)


def test_oversize_hook_inflates_every_body(instance):
    """`response_too_large` must be provable on identity, not only on a turn."""
    client = instance()
    session = parse_session(open_session(client)[1])
    client.target.fault(oversize_bytes=5000)
    _, body = send_turn(client, session.session_id, request_id="t2")
    turn = parse_turn(body)
    assert len(turn.events[0]["content"]) > 5000
    assert turn.events[0]["content"].endswith(" " * 5000)
    # `usage` stays the honest observation of what the target produced.
    assert turn.usage is not None and turn.usage.completion == len("champion skill | hello")
    _, identity = client.bytes_of("GET", "/instances/i-1")
    assert len(identity) > 5000
    _, health = client.bytes_of("GET", "/health")
    assert len(health) > 5000
    # A health body stays schema-valid: only its free-form field grew.
    assert parse_health(json.loads(health)).allowed_artifact_root.endswith(" " * 5000)
    client.target.fault(oversize_bytes=0)
    assert len(client.bytes_of("GET", "/instances/i-1")[1]) < 5000


def test_slow_drip_paces_every_ok_response(instance, publish):
    client = instance()
    session = parse_session(open_session(client)[1])
    turn_path = f"/instances/i-1/sessions/{session.session_id}/turns"
    baseline_started = time.monotonic()
    status, payload = client.bytes_of(
        "POST", turn_path, dict(request_id="t1", message={"content": "hello", "fields": {}})
    )
    baseline = time.monotonic() - baseline_started
    assert status == 200 and len(payload) > 50
    client.target.fault(slow_drip_seconds=0.001)
    started = time.monotonic()
    status, _ = client.bytes_of(
        "POST", turn_path, dict(request_id="t2", message={"content": "hello", "fields": {}})
    )
    elapsed = time.monotonic() - started
    assert status == 200
    assert elapsed >= 50 * 0.001
    assert elapsed > baseline
    # Instance creation is dripped too: the adapter's read timeout must be
    # provable on `materialize`, not only on a turn.
    root = publish("run-b")
    started = time.monotonic()
    assert client.call("POST", "/instances", create_body("i-2", root))[0] == 200
    assert time.monotonic() - started >= 50 * 0.001
    client.target.fault(slow_drip_seconds=0.0)
    started = time.monotonic()
    assert client.call("GET", "/health")[0] == 200
    assert time.monotonic() - started < 0.1


# -- linearizability ----------------------------------------------------------


def test_the_instance_lock_covers_the_whole_handler(instance):
    """Every instance-scoped handler parks on the instance lock, release included.

    `sleep_seconds` cannot serve as the probe: it stalls in `dispatch`, outside
    the lock, precisely so a stalled create or identity is provable too. So the
    lock is held from the test thread instead — a direct proof that turn, get and
    delete all wait for it, which is the linearizability guarantee.
    """
    client = instance()
    session = parse_session(open_session(client)[1])
    held = client.target._instances["i-1"].lock
    done = []

    def run(call):
        done.append(call())

    calls = (
        lambda: send_turn(client, session.session_id, request_id="t1"),
        lambda: client.call("GET", "/instances/i-1"),
        lambda: client.call("DELETE", "/instances/i-1"),
    )
    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    with held:
        for thread in threads:
            thread.start()
        time.sleep(0.25)
        assert done == []
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    # Whatever order they won the lock in, the result is a valid linearization:
    # the release succeeded and the other two either committed before it or saw
    # the released instance.
    assert len(done) == 3
    statuses = sorted(status for status, _ in done)
    assert statuses in ([200, 200, 200], [200, 200, 404], [200, 404, 404])
    release = [body for _, body in done if "released" in body]
    assert release == [{"instance_id": "i-1", "released": True}]
    assert parse_health(client.call("GET", "/health")[1]).instances == ()


def test_concurrent_session_creation_loses_nothing(instance):
    client = instance()
    created = []

    def run(n):
        status, body = open_session(client, request_id=f"s{n}")
        created.append((status, body.get("session_id")))

    threads = [threading.Thread(target=run, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert [status for status, _ in created] == [200] * 8
    ids = {session_id for _, session_id in created}
    assert len(ids) == 8
    for n, session_id in enumerate(sorted(ids)):
        assert send_turn(client, session_id, request_id=f"turn{n}")[0] == 200


def test_release_races_session_creation_without_orphans(instance):
    client = instance()
    outcomes = []

    def open_many():
        for n in range(40):
            status, _ = open_session(client, request_id=f"race{n}")
            outcomes.append(status)

    worker = threading.Thread(target=open_many)
    worker.start()
    time.sleep(0.01)
    assert client.call("DELETE", "/instances/i-1")[1]["released"] is True
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert set(outcomes) <= {200, 404}
    assert 404 in outcomes
    assert parse_health(client.call("GET", "/health")[1]).instances == ()


# -- construction -------------------------------------------------------------


def test_constructor_rejects_a_bad_configuration(allowed):
    with pytest.raises(ValueError):
        ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY, seed_mode="random")
    with pytest.raises(ValueError):
        ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY, max_instances=0)
    with pytest.raises(ValueError):
        ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY, target_id="bad id")
    with pytest.raises(ValueError):
        ReferenceTarget(allowed_artifact_root=Path("relative"), policy=POLICY)


def test_allowed_root_is_reported_resolved(tmp_path):
    real = Path(os.path.realpath(tmp_path)) / "real"
    real.mkdir()
    link = Path(os.path.realpath(tmp_path)) / "link"
    link.symlink_to(real, target_is_directory=True)
    target = ReferenceTarget(allowed_artifact_root=link, policy=POLICY)
    assert target.allowed_artifact_root == real
    status, payload = target.dispatch("GET", f"{PREFIX}/health", b"")
    assert status == 200
    assert json.loads(payload)["allowed_artifact_root"] == str(real)
    assert not Path(json.loads(payload)["allowed_artifact_root"]).is_symlink()
    assert stat.S_ISDIR(real.lstat().st_mode)


def test_client_teardown_is_not_logged_but_a_bug_is(allowed, capsys):
    """A reset connection must not print a traceback; a real fault still does."""
    from evolution.tree.target.reference_target_http import _TargetServer

    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    server = _TargetServer(("127.0.0.1", 0), target)
    try:
        try:
            raise ConnectionResetError(104, "reset")
        except ConnectionResetError:
            server.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise RuntimeError("fixture bug")
        except RuntimeError:
            server.handle_error(None, ("127.0.0.1", 1))
        assert "fixture bug" in capsys.readouterr().err
    finally:
        server.server_close()


# -- frozen fault hooks, test-only subclass, IPv6 loopback --------------------


def test_product_target_cannot_be_made_to_misbehave(allowed):
    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    assert target.misbehave is NEUTRAL_MISBEHAVE
    with pytest.raises(AttributeError):
        target.misbehave = Misbehave(internal_error=True)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.misbehave.internal_error = True  # type: ignore[misc]
    other = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    assert other.misbehave is target.misbehave
    assert target.misbehave == Misbehave()


def test_fault_injecting_subclass_replaces_its_own_value(allowed):
    target = FaultInjectingReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    assert target.misbehave == Misbehave()
    changed = target.fault(internal_error=True)
    assert changed.internal_error is True
    assert target.misbehave is changed
    assert NEUTRAL_MISBEHAVE.internal_error is False
    target.reset()
    assert target.misbehave == Misbehave()


def test_product_target_answers_health_after_fault_attempts(allowed):
    # `make_target` builds a FaultInjectingReferenceTarget after this task; the
    # product class is exercised directly here.
    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    server = serve(target)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        with pytest.raises(dataclasses.FrozenInstanceError):
            target.misbehave.internal_error = True  # type: ignore[misc]
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/evolution/v2/health")
        response = conn.getresponse()
        assert response.status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_serve_refuses_a_non_loopback_host_before_binding(allowed, monkeypatch):
    """`0.0.0.0` is refused before any socket is constructed, not merely
    before it is bound: patching `_TargetServer` to fail proves `serve` never
    reaches it for a non-loopback host."""
    import evolution.tree.target.reference_target_http as reference_target_http

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("a non-loopback host must never reach the socket constructor")

    monkeypatch.setattr(reference_target_http, "_TargetServer", _must_not_be_called)
    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    with pytest.raises(ValueError, match="loopback"):
        serve(target, host="0.0.0.0")


def test_serve_binds_localhost(allowed):
    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    server = serve(target, host="localhost", port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/evolution/v2/health")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_serve_binds_ipv6_loopback(allowed):
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support")
    target = ReferenceTarget(allowed_artifact_root=allowed, policy=POLICY)
    try:
        server = serve(target, host="::1", port=0)
    except OSError:
        pytest.skip("no IPv6 loopback on this host")
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        conn = http.client.HTTPConnection("::1", server.server_address[1], timeout=5)
        conn.request("GET", "/evolution/v2/health")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
