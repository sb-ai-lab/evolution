"""Task 9: ``lab target probe`` (spec 2026-10-02 §5.2) over a fake service client."""
import io
import json

import pytest

from test_service_cli import HEALTH, FakeClient as _FakeClient

from evolution_lab.cli import main as _main
from evolution_lab.cli.client import ApiError
from test_service_probe_routes import SESSION, SID, TURN

CEILING = {"turns": {"used": 1, "max": 3}, "tokens": {"used": 15, "max": 100},
           "cost": {"used": "0.012", "max": "1.5", "currency": "RUB"}}
FULL_TURN = dict(TURN, steps=[{"index": 0, "type": "tool_call", "data": {"name": "x"}},
                              {"index": 1, "type": "assistant_message", "data": {"content": "yo"}}],
                 agent_outcome="handoff", usage={"prompt": 10, "completion": 5, "cached": 0, "total": 15},
                 cost={"amount": "0.012000000", "currency": "RUB", "attribution": "ledger_window",
                       "upper_bound": False}, latency_ms=1840, ceiling=CEILING)
HISTORY = dict(SESSION, ceiling=CEILING, turns=[FULL_TURN])


class FakeClient(_FakeClient):
    """The shared fake, taking the per-call ``timeout`` the probe commands may pass."""

    def __init__(self, responses=()):
        super().__init__(responses)
        self.timeouts = []

    def request_json(self, method, path, *, timeout=None, **values):
        self.timeouts.append(timeout)
        return super().request_json(method, path, **values)


def run(argv, client, stdin=""):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = _main(argv, stdout=stdout, stderr=stderr, stdin=io.StringIO(stdin), client_factory=lambda **_: client)
    return code, stdout.getvalue(), stderr.getvalue()


def test_start_posts_the_closed_body_with_a_key_and_prints_the_id():
    client = FakeClient([HEALTH, SESSION])
    code, out, err = run(["--socket", "/tmp/s.sock", "target", "probe", "start", "acq", "agent", "--max-turns", "3"],
                         client)
    assert code == 0, err
    health, start = client.calls
    assert health[:2] == ("GET", "/v1/health")
    assert start == ("POST", "/v1/projects/acq/probes",
                     {"schema": "lab-service-project-probe-request/1", "implementation": "agent",
                      "ceiling": {"max_turns": 3}, "budget": None, "turn_timeout_seconds": None, "seed": None},
                     True, "lab-probe-session/1")
    assert client.keys[1] is not None
    assert SID in out and "open" in out


def test_start_with_gateway_budget():
    client = FakeClient([SESSION])
    code, _, err = run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "start", "acq", "agent",
                        "--max-turns", "3", "--max-tokens", "500", "--max-cost", "1.5", "--budget-ledger", "/l.db",
                        "--budget-role", "a", "--budget-role", "b", "--budget-currency", "RUB",
                        "--turn-timeout", "30", "--seed", "7"], client)
    assert code == 0, err
    assert client.calls[0][2] == {
        "schema": "lab-service-project-probe-request/1", "implementation": "agent",
        "ceiling": {"max_turns": 3, "max_tokens": 500, "max_cost": "1.5"},
        "budget": {"ledger_path": "/l.db", "roles": ["a", "b"], "currency": "RUB"},
        "turn_timeout_seconds": 30.0, "seed": 7}


@pytest.mark.parametrize("extra", [
    ["--max-cost", "1"], ["--budget-role", "a"], ["--budget-currency", "RUB"],
    ["--budget-ledger", "/l.db", "--budget-currency", "RUB"], ["--budget-ledger", "/l.db", "--budget-role", "a"],
    ["--budget-ledger", "l.db", "--budget-role", "a", "--budget-currency", "RUB"], ["--max-tokens", "0"],
    ["--budget-currency", "EUR"]])
def test_start_option_errors_exit_2_without_a_call(extra):
    client = FakeClient([])
    code, out, err = run(["--socket", "/tmp/s.sock", "target", "probe", "start", "acq", "agent", "--max-turns", "3",
                          *extra], client)
    assert code == 2 and out == "" and client.calls == []


def test_max_cost_without_ledger_names_the_options():
    code, _, err = run(["--socket", "/tmp/s.sock", "target", "probe", "start", "acq", "agent", "--max-turns", "3",
                        "--max-cost", "1"], FakeClient([]))
    assert code == 2 and "--budget-ledger" in err


def test_max_turns_is_required():
    code, _, _ = run(["--socket", "/tmp/s.sock", "target", "probe", "start", "acq", "agent"], FakeClient([]))
    assert code == 2


def test_say_sends_a_keyed_turn_and_prints_it():
    client = FakeClient([HEALTH, FULL_TURN])
    code, out, err = run(["--socket", "/tmp/s.sock", "target", "probe", "say", SID, "hi"], client)
    assert code == 0, err
    assert client.calls[1] == ("POST", f"/v1/probes/{SID}/turns",
                               {"schema": "lab-service-probe-turn-request/1", "content": "hi"}, True,
                               "lab-probe-turn/1")
    assert client.keys[1] is not None
    assert out.splitlines() == [
        f"turn {SID}/1  handoff  ok  1840 ms", "answer: yo", "steps: 2 (tool_call, assistant_message)",
        "tokens: 15  cost: 0.012000000 RUB", "ceiling: turns 1/3, tokens 15/100, cost 0.012/1.5 RUB"]


def test_say_reads_stdin_with_a_dash_and_drops_the_final_newline():
    client = FakeClient([TURN])
    code, _, err = run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "say", SID, "-"], client,
                       stdin="привет\nмир\n")
    assert code == 0, err
    assert client.calls[0][2]["content"] == "привет\nмир"
    assert run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "say", SID, "-"], FakeClient([]),
               stdin="")[0] == 2


def test_say_unknown_tokens_and_no_cost_render_dashes():
    turn = dict(TURN, usage=None, cost=None, answer=None, latency_ms=None, agent_outcome="infrastructure",
                outcome="target_failed", failure={"code": "turn_timeout"})
    code, out, _ = run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "say", SID, "x"],
                       FakeClient([turn]))
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"turn {SID}/1  infrastructure  target_failed  - ms"
    assert "steps: 0" in lines and "tokens: unknown  cost: —" in lines and "failure: turn_timeout" in lines
    assert lines[-1] == "ceiling: turns 0/3"


def test_say_http_timeout_is_not_shorter_than_a_turn():
    client = FakeClient([TURN])
    run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "say", SID, "x"], client)
    assert client.timeouts == [None]  # no client-side read limit unless one was asked for
    client = FakeClient([TURN])
    _, _, err = run(["--socket", "/tmp/s.sock", "--no-request-id", "--timeout", "5", "target", "probe", "say", SID,
                     "x"], client)
    assert client.timeouts[0] >= 610
    assert err == "lab: --timeout raised to 610 s for say (turns may take up to 600 s)\n"
    client = FakeClient([TURN])
    _, _, err = run(["--socket", "/tmp/s.sock", "--no-request-id", "--timeout", "900", "target", "probe", "say", SID,
                     "x"], client)
    assert client.timeouts[0] == 900 and err == ""


def test_history_and_list_are_reads_without_a_key():
    client = FakeClient([HISTORY, {"schema": "lab-probe-session-list/1", "sessions": []}])
    code, out, err = run(["--socket", "/tmp/s.sock", "target", "probe", "history", SID], client)
    assert code == 0, err
    assert client.calls[0] == ("GET", f"/v1/probes/{SID}", None, False, "lab-probe-session/1")
    assert "request: hi" in out and f"turn {SID}/1  handoff  ok  1840 ms" in out and "steps: 2" in out
    assert "tokens: 15  cost: 0.012000000 RUB" in out
    code, out, err = run(["--socket", "/tmp/s.sock", "target", "probe", "list", "acq", "--implementation", "a b"],
                         client)
    assert code == 0, err
    assert client.calls[1] == ("GET", "/v1/projects/acq/probes?implementation=a%20b", None, False,
                               "lab-probe-session-list/1")
    assert client.keys == [None, None] and "no probe sessions" in out
    # a request key option on a read is refused, as for every read
    assert run(["--socket", "/tmp/s.sock", "--request-id", "r1", "--generation", "g" * 32,
                "target", "probe", "history", SID], FakeClient([]))[0] == 2


def test_list_prints_one_line_per_session():
    listing = {"schema": "lab-probe-session-list/1",
               "sessions": [{k: v for k, v in SESSION.items() if k != "turns"}]}
    code, out, _ = run(["--socket", "/tmp/s.sock", "target", "probe", "list", "acq"], FakeClient([listing]))
    assert code == 0 and out.startswith(f"session {SID} open project=acq implementation=agent")


def test_reset_and_close_are_keyed_mutations():
    client = FakeClient([HEALTH, SESSION, HEALTH, dict(SESSION, state="closed", closed_reason="closed")])
    assert run(["--socket", "/tmp/s.sock", "target", "probe", "reset", SID], client)[0] == 0
    code, out, _ = run(["--socket", "/tmp/s.sock", "target", "probe", "close", SID], client)
    assert code == 0 and "closed/closed" in out
    methods = [(c[0], c[1], c[2], c[3]) for c in client.calls if c[1] != "/v1/health"]
    assert methods == [("POST", f"/v1/probes/{SID}/reset", {}, True), ("DELETE", f"/v1/probes/{SID}", None, True)]
    assert all(key is not None for key, call in zip(client.keys, client.calls) if call[1] != "/v1/health")


def test_json_prints_the_form_as_is():
    for argv, form, key in ((["history", SID], HISTORY, []), (["say", SID, "hi"], FULL_TURN, ["--no-request-id"])):
        code, out, _ = run(["--socket", "/tmp/s.sock", "--json", *key, "target", "probe", *argv],
                           FakeClient([form]))
        assert code == 0 and json.loads(out) == form


def test_service_error_is_printed_and_fails():
    error = ApiError({"schema": "lab-error/1", "error": {"code": "probe_ceiling_reached", "stage": "project",
                                                         "message": "m"}})
    code, out, err = run(["--socket", "/tmp/s.sock", "--no-request-id", "target", "probe", "say", SID, "x"],
                         FakeClient([error]))
    assert code != 0 and "probe_ceiling_reached" in err
