"""Neutral sample documents of the restructure scenario (a fictional parcel-tracking support agent)."""
from __future__ import annotations

import copy

SCOPE = {"schema": "restructure-scope/1", "source": "agent",
         "hide": [{"path": "docs/architecture.md", "reason": "ready-made analysis of the agent"},
                  {"path": "benchmarks", "reason": "reference answers"}]}

QUESTION = {"id": "q1", "text": "Is the redirect still offered on voice?", "why": "voice has no identity check",
            "options": [{"id": "yes", "text": "Yes, on both channels", "consequence": "port the identity check"},
                        {"id": "no", "text": "Chat only", "consequence": None}],
            "recommended": "no", "answer": None}

MAP = {
    "schema": "restructure-behavior-map/2", "source": "agent",
    "problem": {"id": "problem", "text": "Answers customers about their parcels and hands over to an operator.",
                "channels": ["chat", "voice"], "agent_ru": "Parcel agent · first line, chat and voice"},
    "architecture": {"summary": "A rule graph routes every message; the model only classifies.",
                     "numbers": [{"id": "n-lines", "label": "Python lines", "value": "9 000", "sources": ["app"]},
                                 {"id": "n-chains", "label": "chains", "value": "12", "sources": ["app/graph.py#build"]}],
                     "chain": [{"id": "c-transport", "label": "transport", "note": None, "sources": ["app/api.py#entry"]},
                               {"id": "c-graph", "label": "graph", "note": "12 chains", "sources": ["app/graph.py#build"]},
                               {"id": "c-answer", "label": "answer", "note": None, "sources": ["app/answer.py:1-40"]}],
                     "notes": [{"id": "an1", "text": "Routing is code, not the model."}]},
    "routing": {"rules": [{"id": "r-track", "input": "a tracking number", "target": "s-where", "note": None,
                 "sources": ["app/router.py#route"]}],
                "risk": "Classifier thresholds decide the route."},
    "scenarios": [
        {"id": "s-where", "name": "Where is my parcel", "summary": "Finds the parcel and tells its status.",
         "share": {"value": 0.6, "basis": "operator export, 400 dialogues"},
         "outcomes": [{"kind": "answered", "text": "status and the expected date"}],
         "uses": ["t-lookup", "k-statuses"], "port": {"decision": "carry", "note": None},
         "sources": ["app/flows/where.py#handle"]}],
    "tools": [{"id": "t-lookup", "name": "lookup_parcel", "side_effect": False,
               "description": "Reads the parcel record.", "sources": ["app/clients/tracking.py#lookup"]}],
    "knowledge": [{"id": "k-statuses", "name": "Status texts", "kind": "document", "purpose": "human status texts",
                   "location": "app/data/statuses.yaml", "sources": ["app/data/statuses.yaml"]}],
    "outcomes": [{"id": "o-ok", "code": "OK", "meaning": "answered", "answer_code": "200",
                  "sources": ["app/codes.py:12-30"]}],
    "assumptions": [{"id": "a1", "n": 1, "text": "The voice channel uses the same flows as chat.",
                     "sources": ["app/voice.py#entry"]}],
    "questions": [QUESTION]}

VERIFY = {
    "schema": "restructure-verify/1", "port": "port-a", "source": "agent", "port_commit": "a" * 40,
    "layers": [
        {"id": "l-offline", "kind": "offline_tests", "title": "Offline tests", "proves": "the wiring is predictable",
         "status": "passed", "by": "builder", "counts": {"done": 120, "total": 120, "threshold": None},
         "evidence": [{"kind": "command", "ref": "pytest -q", "summary": "120 passed"}],
         "decision": None, "run_at": "2026-10-02T12:00:00Z"},
        {"id": "l-live", "kind": "live_corpus", "title": "Live dialogues",
         "proves": "the model picks the scenario and the tools", "status": "partial", "by": "human",
         "counts": {"done": 2, "total": 20, "threshold": 17},
         "evidence": [{"kind": "log", "ref": "runs/live-1.log", "summary": "2 of 2 matched"}],
         "decision": "stopped after 2: provider unstable", "run_at": "2026-10-02T13:00:00Z"},
        {"id": "l-eval", "kind": "project_evaluation", "title": "Project evaluation", "proves": "a quality number",
         "status": "deferred", "by": "human", "counts": None, "evidence": [], "decision": None, "run_at": None},
        {"id": "l-claims", "kind": "claims_vs_wiring", "title": "Claimed but not wired",
         "proves": "the report claims no more than is wired", "status": "passed", "by": "reviewer", "counts": None,
         "evidence": [{"kind": "note", "ref": "review", "summary": "README matches the wiring"}],
         "decision": None, "run_at": "2026-10-02T14:00:00Z"}],
    "failures": [{"id": "f1", "layer": "l-live", "case": "dialogue 3", "blame": "env", "note": "provider timeout"}],
    "comparison": [{"id": "c-lines", "label": "Python lines", "before": 9000, "after": 1800}],
    "template_notes": [{"id": "n1", "severity": "minor", "text": "The settings loader hides a missing key."}]}
VERIFY.update(schema="restructure-verify/2",
              accepting="The port answers like the original offline; live dialogues are not measured yet.",
              carry_to_brief=[{"id": "cb1", "text": "Live dialogues: provider timeouts — rerun on a stable sandbox."}])

GOAL = {
    "schema": "restructure-goal/2", "source": "agent",
    "prompt": [{"id": "p-role", "title": "Role", "text": "You answer customers about parcels.",
                "origin": "replaces the greeting chain"}],
    "skills": {"links": [{"id": "l-where", "from": [{"kind": "topic", "label": "Where is my parcel", "ref": "s-where"}],
                          "method": "as_skill", "to": [{"kind": "skill", "label": "where"}], "note": None}],
               "knowledge_links": [{"id": "l-statuses",
                                    "from": [{"kind": "knowledge", "label": "Status texts", "ref": "k-statuses"}],
                                    "method": "as_is", "to": [{"kind": "skill", "label": "where"}], "note": None}]},
    "tools": {"links": [{"id": "l-lookup", "from": [{"kind": "call", "label": "lookup_parcel", "ref": "t-lookup"}],
                         "method": "as_tool", "to": [{"kind": "tool", "label": "lookup_parcel"}], "note": None}]},
    "texts": [{"id": "x-handoff", "when": "the parcel is not found twice", "file": "texts/handoff.md",
               "text": "I am passing you to an operator."}],
    "handler": {"before": [{"id": "h-b1", "text": "Strip the tracking number."}],
                "after": [{"id": "h-a1", "text": "Map the answer code."}],
                "codes": [{"id": "h-c1", "from": "E_NOT_FOUND", "to": "not_found", "note": None}]},
    "deps": {"session": [{"id": "d-s1", "text": "customer id"}],
             "clients": [{"id": "d-c1", "name": "tracking", "offline": "fake", "sandbox": "tracking sandbox"}],
             "state": [{"id": "d-st1", "text": "how many lookups failed"}]},
    "modes": {"modes": [{"id": "offline", "included": True, "note": None},
                        {"id": "live", "included": True, "note": "the tracking sandbox"}],
              "ladder": [{"id": "m1", "text": "Offline tests pass", "layer": "offline_tests", "threshold": None},
                         {"id": "m2", "text": "Live dialogues match", "layer": "live_corpus", "threshold": 0.85}]},
    "changes": [],
    "questions": []}

PORT_SPEC = {
    "schema": "restructure-port-spec/2",
    "task": {"text": "Port the parcel agent onto sylph."},
    "stages": [{"id": "st-contract", "title": "Contract", "what": "Inputs, outputs and answer codes",
                "done_when": "contract tests pass", "cells": ["handler"]},
               {"id": "st-offline", "title": "Offline", "what": "One scenario from the public entry point on fakes",
                "done_when": "the offline end-to-end test of 'where' passes", "cells": ["prompt", "skills", "tools"]}],
    "rules": [{"id": "r-redirect", "text": "No address change without the identity check in code",
               "why": "side effect"}],
    "requirements": [{"id": "q-status", "text": "Tells the parcel status", "cell": "skills",
                      "test": "tests/test_where.py::test_status"}],
    "evolvable": [{"id": "ev-prompt", "path": "skills/where/prompt.md", "why": "wording of the answer"}],
    "bounds": {"items": [{"id": "b1", "text": "Do not change src/sylph/."}],
               "claims_check": "List every claimed capability and the code path that wires it."},
    "notes": [{"id": "nt1", "section": "stages", "text": "Two handler rules have no test yet."}]}

_SAMPLES = {"scope": SCOPE, "map": MAP, "verify": VERIFY, "goal": GOAL, "port_spec": PORT_SPEC}


def fresh(name: str) -> dict:
    return copy.deepcopy(_SAMPLES[name])
