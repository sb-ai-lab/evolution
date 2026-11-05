"""Characterisation lock for every reason, label, and error string the promotion gate emits.

Gate reasons are NOT logs. The writer prompt receives
``editor_facing_gate_reason(telemetry)`` in ``rejected_attempts[].reason`` (and in
``_rejected_attempts/attemptN_reason.txt``), while the short ``telemetry["reason"]``
tags are persisted in the attempt log and summarised by ``evolution.evolve.meta_skill``.
The end-of-run race verdict
(``race_ship_decision(...)["label"]`` / ``["confidence"]``) is persisted as the shipped
version's provenance. Reformatting one of these strings changes the optimizer's input
or the run record, so a refactor must never change them by accident.

**Which universe this lock covers**, decided before it was re-landed rather than
after the first confusing failure:

* Scope is ``evolution.evolve.promotion_gate`` — the module that decides a
  promotion — not all of ``evolve/``. The wider package-level inventory (13
  strings across ``promotion_gate``, ``optimizer``, ``edit_ops`` and
  ``rewrite_edit_ops``) is checked separately by
  ``test_gate_reason_inventory.py``, which needs no fixtures because it locks the
  vocabulary rather than the bytes. Splitting them keeps this file's coverage
  guarantee — every literal is reached by a driven case — actually true, instead
  of true-by-exception-list.
* ``meta_skill._REJECT_REASON``'s 15 entries are deliberately **excluded**. They
  reach a ``"reason"`` key through ``build_rejected_entry``, but they are a
  message catalogue keyed by candidate status, not gate verdict vocabulary;
  folding fifteen prose sentences in here would couple this lock to writer
  copy-editing and bury the nine strings that decide promotions.

A note for whoever hits an unexplained count change: when this lock was archived
the ``ast.Dict``-only scanner found **4** literals, and re-running it at HEAD
found **6**. That gap is *not* scanner blindness — ``insufficient_pairs`` and
``task_regression`` are new branches added to ``conservative_paired_gate`` after
the archive, and both are plain dict literals. The blindness is real but separate:
the scanner also missed ``telemetry["reason"] = ...`` subscript assignment, which
is where ``paired_improvement``, ``underpowered`` and ``no_significant_gain``
live. Fixing both brings the module inventory to **9**, all nine driven below.

This module locks, for ``evolution.evolve.promotion_gate``:

1. the static inventory of ``"reason"``-field literals, of every ``raise``-message
   fragment, and of the race ship-decision vocabulary -- the string constants in the
   bodies (docstrings excluded) of ``_race_confidence`` / ``_race_labels_argmax`` /
   ``race_ship_decision``, so ``"unconfirmed"``, the ``f"reeval_argmax_{tag}"`` stem,
   the ``confirmed@`` format spec and the result-dict keys are all locked. Everything
   here is AST-extracted, so branches unreachable from a small fixture -- the two
   "joint distribution too large" RuntimeErrors -- stay locked too;
2. the exact ``(decision, reason)`` pairs and the full telemetry bytes returned by the
   promotion decision functions, driven with fixed deterministic fixtures, INCLUDING
   both alpha boundaries of ``_lex_gate_telemetry``: ``p_value == alpha`` promotes
   (that comparison is ``<=``, not ``<``) and ``p_floor == alpha`` is NOT reported
   UNDERPOWERED (that comparison is a strict ``>``, not ``>=``);
3. the exact ``(shipped_version, label, confidence)`` triples and the full result bytes
   of ``race_ship_decision`` under both ship rules -- including the version sort and the
   "ties keep the earliest version" rule -- plus the ``_race_confidence`` threshold table
   probed on and one step past each of its 0.05 / 0.10 / 0.20 edges;
4. the verbatim editor-facing rejection prose;
5. the exact text of every gate error message driven by a small fixture, which is every
   ``raise`` in the module except the two "joint distribution too large" RuntimeErrors
   (reaching those needs a >2M-state joint); those two stay locked statically by (1).

Claims (1)/(2), (1)/(3) and (1)/(5) are welded together by four coverage assertions
(``test_every_reason_literal_is_reached_by_a_driven_case``,
``test_every_race_label_literal_is_reached_by_a_driven_case``,
``test_every_race_confidence_literal_is_reached_by_a_driven_case``,
``test_only_the_oversize_joint_runtime_errors_are_undriven``). Each compares the driven
fixtures against the LIVE AST inventory, not against a golden, so a reason, label,
confidence, or error added to the module that no fixture reaches fails the suite and
cannot be silenced by refreshing a golden. "Every string is locked" therefore stays true
as the gate grows, instead of quietly narrowing to "every string we thought of".

Nothing here samples: ``perm_lex`` enumerates combinations, ``perm_lex_paired``
enumerates ``2^pairs`` sign flips, and every race fixture stays under the 16
nonzero-diff-unit ceiling that makes ``_max_stat_null`` enumerate exactly, so every
locked value is deterministic. The Monte-Carlo fallback above that ceiling is
deterministic too (fixed seed) but costs seconds per call, so it is deliberately NOT
driven here; it emits the same label vocabulary and differs only in ``exact: false``.

To refresh after an INTENTIONAL change, run

    python -m pytest tests/characterisation/test_gate_reasons.py -q

Each assertion prints the *actual* value (inventory tuple, sha256, or text); copy it
into the matching golden constant below in the SAME commit.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from evolution.evolve import promotion_gate as pg

_MODULE_TREE = ast.parse(Path(pg.__file__).read_text(encoding="utf-8"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. Static inventory of reason / raise / race-vocabulary literals
# ---------------------------------------------------------------------------

GOLDEN_REASON_FIELD_LITERALS = (
    "inconclusive_missing_arm",
    "insufficient_pairs",
    "no_observed_gain",
    "no_significant_gain",
    "no_validation_tasks",
    "paired_improvement",
    "task_regression",
    "underpowered",
    "unpaired_or_empty",
)

GOLDEN_RAISE_MESSAGE_FRAGMENTS = (
    " must have the same paired trial count",
    " out of range (0, 1)",
    " pass rates must be finite and in [0, 1]",
    "; expected one of ",
    "EVO_GATE_TAU_SCALE must be finite and non-negative, got ",
    "EVO_PROMOTE_ALPHA ",
    "EVO_PROMOTE_DELTA and EVO_PROMOTE_TOLERANCE must be finite",
    "alpha must be finite and in (0, 1)",
    "delta and tolerance must be finite",
    "min_pairs must be positive",
    "parent and candidate must contain the same tasks",
    "perm_lex joint distribution too large; reduce validation tasks or gate trials",
    "perm_lex_paired joint distribution too large; reduce validation tasks or gate trials",
    "race arms must share the (task, trial) shape, got ",
    "race has no (task, trial) units",
    "race version must look like vN, got ",
    "race_ship_decision: no versions",
    "task ",
    "unknown EVO_GATE_METRIC ",
    "unknown EVO_RACE_SHIP_RULE ",
    "unknown gate metric ",
)

# Every string constant in the bodies of the three race functions, docstrings excluded.
# Renaming "unconfirmed", the f"reeval_argmax_{tag}" stem, or any result-dict key moves
# this tuple.
GOLDEN_RACE_VOCABULARY = (
    ".2f",
    "; expected one of ",
    "alpha",
    "argmax",
    "baseline",
    "best_version",
    "confidence",
    "confirmed",
    "confirmed@",
    "d_vs_baseline",
    "exact",
    "label",
    "means",
    "n_units",
    "p_floor",
    "p_value",
    "race_ship_decision: no versions",
    "reeval_argmax_",
    "reeval_confirmed_winner",
    "reeval_ship_v0",
    "reeval_ship_v0_nonseparation",
    "reeval_ship_v0_underpowered",
    "reeval_single_version",
    "ship_rule",
    "shipped_version",
    "unconfirmed",
    "underpowered",
    "unknown EVO_RACE_SHIP_RULE ",
    "versions",
)

GOLDEN_INVENTORY_SHA = "3ff04542f3b87451638b528193c126cc202aafd01146b92b302460b95dd742d6"

_RACE_VOCABULARY_FUNCTIONS = ("_race_confidence", "_race_labels_argmax", "race_ship_decision")


def _result_literals(node: ast.AST) -> set[str]:
    """String literals an expression can *evaluate to*, ignoring its operands.

    A plain ``ast.walk`` is wrong here: in
    ``"underpowered" if d.get("status") == "UNDERPOWERED" else d.get("reason", "x")``
    the strings ``"status"``, ``"UNDERPOWERED"`` and ``"reason"`` are a lookup key,
    a comparison operand and another lookup key. None of them can ever be the
    stored reason, and admitting them would put three values in the locked
    vocabulary that no fixture could possibly drive.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return _result_literals(node.body) | _result_literals(node.orelse)
    is_get_call = (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 2
    )
    if is_get_call:
        assert isinstance(node, ast.Call)
        return _result_literals(node.args[1])
    return set()


def _reason_field_literals() -> tuple[str, ...]:
    """Every string literal that can become a ``"reason"`` field in this module.

    Two shapes carry a reason, and a scanner that sees only the first is blind to
    the branch that decides most real promotions:

    * ``{**telemetry, "reason": "task_regression"}`` — a dict literal;
    * ``telemetry["reason"] = "paired_improvement"`` — subscript assignment,
      including the conditional form whose two arms are both locked here.

    Shapes it still does **not** see, so a reason written this way would drop out
    of the inventory *and* out of the coverage assertion that pairs with it:
    ``AugAssign``, ``setdefault``, ``update(reason=...)``, a value produced by a
    helper or named constant, and an f-string. None occur today. Adding one is
    the way to defeat this lock, which is why the list is written down.
    """
    found: set[str] = set()
    for node in ast.walk(_MODULE_TREE):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                is_reason_key = isinstance(key, ast.Constant) and key.value == "reason"
                if is_reason_key:
                    found |= _result_literals(value)
        elif isinstance(node, ast.Assign):
            targets_reason = any(
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "reason"
                for target in node.targets
            )
            if targets_reason:
                found |= _result_literals(node.value)
    return tuple(sorted(found))


def _raise_message_fragments() -> tuple[str, ...]:
    """Every string fragment that can appear inside a raised exception message.

    f-string templates contribute their constant segments, so the wording is locked
    even where the value is interpolated.
    """
    found: set[str] = set()
    for node in ast.walk(_MODULE_TREE):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        for sub in ast.walk(node.exc):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.add(sub.value)
    return tuple(sorted(found))


def _race_vocabulary() -> tuple[str, ...]:
    """Every string constant emitted or keyed by the race ship-decision functions.

    Docstrings are skipped so prose may be reworded, but f-string constant segments and
    format specs are kept, which is what pins ``reeval_argmax_<tag>`` and
    ``confirmed@{level:.2f}``.
    """
    found: set[str] = set()
    for node in ast.walk(_MODULE_TREE):
        if not isinstance(node, ast.FunctionDef) or node.name not in _RACE_VOCABULARY_FUNCTIONS:
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        for statement in body:
            for sub in ast.walk(statement):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.add(sub.value)
    return tuple(sorted(found))


def _race_confidence_return_stems() -> tuple[str, ...]:
    """The literal stem of every value ``_race_confidence`` can return.

    An f-string return contributes only its leading constant, so ``confirmed@`` stands
    for the whole ``confirmed@<level>`` family.
    """
    stems: set[str] = set()
    for node in ast.walk(_MODULE_TREE):
        if not isinstance(node, ast.FunctionDef) or node.name != "_race_confidence":
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            returned = statement.value
            if isinstance(returned, ast.Constant) and isinstance(returned.value, str):
                stems.add(returned.value)
            elif isinstance(returned, ast.JoinedStr) and returned.values:
                head = returned.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    stems.add(head.value)
    return tuple(sorted(stems))


def _inventory_text() -> str:
    return "\n".join(
        (
            *_reason_field_literals(),
            "--",
            *_raise_message_fragments(),
            "--",
            *_race_vocabulary(),
        )
    )


def test_reason_field_literal_inventory_is_locked():
    actual = _reason_field_literals()
    assert actual == GOLDEN_REASON_FIELD_LITERALS, (
        f"promotion_gate reason literals drifted; actual inventory: {actual!r}"
    )


def test_raise_message_fragment_inventory_is_locked():
    actual = _raise_message_fragments()
    assert actual == GOLDEN_RAISE_MESSAGE_FRAGMENTS, (
        f"promotion_gate raise-message literals drifted; actual inventory: {actual!r}"
    )


def test_race_vocabulary_inventory_is_locked():
    actual = _race_vocabulary()
    assert actual == GOLDEN_RACE_VOCABULARY, (
        f"race_ship_decision label/confidence vocabulary drifted; actual inventory: {actual!r}"
    )


def test_inventory_hash_is_locked():
    actual = _sha(_inventory_text())
    assert actual == GOLDEN_INVENTORY_SHA, (
        f"promotion_gate reason/raise inventory hash drifted; actual: {actual}"
    )


# ---------------------------------------------------------------------------
# 2. Driven decision cases
# ---------------------------------------------------------------------------

_GATE_ENV_VARS = (
    "EVO_PROMOTE_DELTA",
    "EVO_PROMOTE_TOLERANCE",
    "EVO_GATE_METRIC",
    "EVO_PROMOTE_ALPHA",
    "EVO_GATE_TAU_SCALE",
    "EVO_DISABLE_DELTA_GATE",
    "EVO_GATE_POOL_PARENT",
)

ALPHA = 0.10


def _scored(table: dict[tuple[str, str], Any]) -> Callable[[str, str], Any]:
    return lambda body, task_id: table[(body, task_id)]


_PASS_RATE_SCORES = _scored({("parent", "v1"): 0.40, ("cand", "v1"): 0.90})
_REGRESS_SCORES = _scored({("parent", "v1"): 0.90, ("cand", "v1"): 0.40})
_TRIAL_SCORES = _scored(
    {("parent", "v1"): [0.0, 0.0, 0.0, 0.0], ("cand", "v1"): [1.0, 1.0, 1.0, 1.0]}
)
_TRAINVAL_GAIN = _scored(
    {
        ("parent", "t1"): 0.0,
        ("parent", "t2"): 0.0,
        ("parent", "v1"): 0.0,
        ("cand", "t1"): 1.0,
        ("cand", "t2"): 1.0,
        ("cand", "v1"): 1.0,
    }
)
_TRAINVAL_FLAT = _scored(
    {
        ("parent", "t1"): 0.5,
        ("parent", "t2"): 0.5,
        ("parent", "v1"): 0.5,
        ("cand", "t1"): 0.5,
        ("cand", "t2"): 0.5,
        ("cand", "v1"): 0.5,
    }
)


def _gate(score_fn: Callable[[str, str], Any], **kwargs: Any) -> tuple[bool, dict]:
    return pg.gate_candidate(
        parent_body="parent",
        candidate_body="cand",
        validate_ids=kwargs.pop("validate_ids", ["v1"]),
        score_fn=score_fn,
        **kwargs,
    )


# A hand-built joint distribution whose p-value and p-floor are exact binary fractions,
# so `alpha` can be placed EXACTLY on either comparison boundary of _lex_gate_telemetry.
# Support {2, 1, 0} with counts {1, 1, 2}: the lex-max mass is 1/4 (the p-floor) and the
# mass at or above the observed (1, 0.0) is 2/4 (the p-value).
_BOUNDARY_JOINT: dict[tuple[Fraction, float], int] = {
    (Fraction(2), 0.0): 1,
    (Fraction(1), 0.0): 1,
    (Fraction(0), 0.0): 2,
}
_BOUNDARY_OBS = (Fraction(1), 0.0)
_BOUNDARY_P_VALUE = 0.5
_BOUNDARY_P_FLOOR = 0.25


def _trainval(parent_body: str, candidate_body: str) -> tuple[bool, dict]:
    return pg.gate_candidate_trainval(
        parent_body=parent_body,
        candidate_body=candidate_body,
        train_ids=["t1", "t2"],
        val_ids=["v1"],
        score_fn=_TRAINVAL_GAIN,
        tau_scale=0.5,
    )


# name -> (env overrides, thunk)
_CASES: dict[str, tuple[dict[str, str], Callable[[], Any]]] = {
    "gate_zone.improve": ({}, lambda: pg.gate_zone(0.2, 0.1)),
    "gate_zone.regress": ({}, lambda: pg.gate_zone(-0.2, 0.1)),
    "gate_zone.noise_zero": ({}, lambda: pg.gate_zone(0.0, 0.1)),
    "gate_zone.noise_at_upper_edge": ({}, lambda: pg.gate_zone(0.1, 0.1)),
    "gate_zone.noise_at_lower_edge": ({}, lambda: pg.gate_zone(-0.1, 0.1)),
    "tau_band.default_scale": ({}, lambda: pg.tau_band(2, 1)),
    "tau_band.zero_tasks": ({}, lambda: pg.tau_band(0, 0, 0.5)),
    "aggregate.pass_rate": ({}, lambda: pg._aggregate_gate_metric([[0.5, 1.0]], "pass_rate")),
    "aggregate.pass_rate_empty": ({}, lambda: pg._aggregate_gate_metric([], "pass_rate")),
    "aggregate.solve_count": ({}, lambda: pg._aggregate_gate_metric([[0.5, 1.0]], "solve_count")),
    "delta_gate_enabled.has_validation_split": (
        {},
        lambda: pg.delta_gate_enabled(["a", "b", "c"]),
    ),
    "delta_gate_enabled.no_validation_split": ({}, lambda: pg.delta_gate_enabled(["a"])),
    "delta_gate_enabled.env_disabled": (
        {"EVO_DISABLE_DELTA_GATE": "1"},
        lambda: pg.delta_gate_enabled(["a", "b", "c"]),
    ),
    "validate_gate_environment.defaults": ({}, pg.validate_gate_environment),
    "validate_gate_environment.trainval_allowed": (
        {"EVO_GATE_METRIC": "solve_count_trainval"},
        lambda: pg.validate_gate_environment(allow_trainval=True),
    ),
    "perm_lex.missing_arm": ({}, lambda: pg.perm_lex_decision([[]], [[1.0]], ALPHA)),
    "perm_lex.no_gain_worse": ({}, lambda: pg.perm_lex_decision([[1.0]], [[0.5]], ALPHA)),
    "perm_lex.no_gain_tied": ({}, lambda: pg.perm_lex_decision([[0.5]], [[0.5]], ALPHA)),
    "perm_lex.promote": (
        {},
        lambda: pg.perm_lex_decision([[0.9, 0.8, 0.7]], [[1.0, 1.0, 1.0]], ALPHA),
    ),
    "perm_lex.reject_p_above_alpha": (
        {},
        lambda: pg.perm_lex_decision([[0.1, 0.2, 0.6]], [[0.3, 0.4, 0.5]], ALPHA),
    ),
    "perm_lex.reject_rate_only_gain": (
        {},
        lambda: pg.perm_lex_decision([[0.5, 0.5, 0.5]], [[0.5, 0.5, 0.6]], ALPHA),
    ),
    "perm_lex.underpowered": ({}, lambda: pg.perm_lex_decision([[0.0]], [[1.0]], ALPHA)),
    "perm_lex_paired.unpaired": (
        {},
        lambda: pg.perm_lex_paired_decision([[0.5, 0.5]], [[1.0]], ALPHA),
    ),
    "perm_lex_paired.empty": ({}, lambda: pg.perm_lex_paired_decision([[]], [[]], ALPHA)),
    "perm_lex_paired.no_gain": (
        {},
        lambda: pg.perm_lex_paired_decision([[1.0, 1.0]], [[0.0, 0.0]], ALPHA),
    ),
    "perm_lex_paired.promote": (
        {},
        lambda: pg.perm_lex_paired_decision([[0.0] * 4], [[1.0] * 4], ALPHA),
    ),
    "perm_lex_paired.reject_p_above_alpha": (
        {},
        lambda: pg.perm_lex_paired_decision([[0.0, 0.0, 0.0, 1.0]], [[1.0, 1.0, 1.0, 0.0]], ALPHA),
    ),
    "perm_lex_paired.underpowered": (
        {},
        lambda: pg.perm_lex_paired_decision([[0.0]], [[1.0]], ALPHA),
    ),
    "lex_boundary.p_value_equals_alpha": (
        {},
        lambda: pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_VALUE),
    ),
    "lex_boundary.p_value_just_above_alpha": (
        {},
        lambda: pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_VALUE - 0.01),
    ),
    "lex_boundary.p_floor_equals_alpha": (
        {},
        lambda: pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_FLOOR),
    ),
    "lex_boundary.p_floor_just_above_alpha": (
        {},
        lambda: pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_FLOOR - 0.01),
    ),
    "perm_lex.p_value_equals_alpha": ({}, lambda: pg.perm_lex_decision([[0.0]], [[1.0]], 0.5)),
    "perm_lex_paired.p_value_equals_alpha": (
        {},
        lambda: pg.perm_lex_paired_decision([[0.0]], [[1.0]], 0.5),
    ),
    "conservative_paired.insufficient_pairs": (
        {},
        lambda: pg.conservative_paired_gate({"t1": [0.5]}, {"t1": [0.9]}),
    ),
    "conservative_paired.task_regression": (
        {},
        lambda: pg.conservative_paired_gate(
            {"t1": [0.5, 0.5], "t2": [0.5, 0.5]},
            {"t1": [0.9, 0.9], "t2": [0.1, 0.1]},
        ),
    ),
    "conservative_paired.paired_improvement": (
        {},
        lambda: pg.conservative_paired_gate({"t1": [0.0] * 4}, {"t1": [1.0] * 4}),
    ),
    "conservative_paired.underpowered": (
        {},
        lambda: pg.conservative_paired_gate({"t1": [0.0]}, {"t1": [1.0]}, min_pairs=1),
    ),
    "conservative_paired.no_observed_gain": (
        {},
        lambda: pg.conservative_paired_gate({"t1": [0.5] * 4}, {"t1": [0.5] * 4}),
    ),
    # The fallback arm: not promoted, not UNDERPOWERED, and the inner decision
    # carries no "reason" of its own, so the default in the ternary is what lands.
    "conservative_paired.no_significant_gain": (
        {},
        lambda: pg.conservative_paired_gate(
            {"t1": [0.0, 0.0, 0.0, 1.0]}, {"t1": [1.0, 1.0, 1.0, 0.0]}
        ),
    ),
    "gate_candidate.no_validation_tasks": ({}, lambda: _gate(_PASS_RATE_SCORES, validate_ids=[])),
    "gate_candidate.pass_rate_promoted": (
        {},
        lambda: _gate(_PASS_RATE_SCORES, delta=0.0, tolerance=0.0, metric="pass_rate"),
    ),
    "gate_candidate.pass_rate_rolled_back": (
        {},
        lambda: _gate(_REGRESS_SCORES, delta=0.0, tolerance=0.0, metric="pass_rate"),
    ),
    "gate_candidate.pass_rate_tolerance_promoted": (
        {},
        lambda: _gate(_REGRESS_SCORES, delta=0.0, tolerance=0.75, metric="pass_rate"),
    ),
    "gate_candidate.solve_count_promoted": (
        {},
        lambda: _gate(_TRIAL_SCORES, delta=0.0, tolerance=0.0, metric="solve_count"),
    ),
    "gate_candidate.perm_lex_env": (
        {"EVO_GATE_METRIC": "perm_lex", "EVO_PROMOTE_ALPHA": "0.10"},
        lambda: _gate(_TRIAL_SCORES),
    ),
    "gate_candidate.perm_lex_paired_env": (
        {"EVO_GATE_METRIC": "perm_lex_paired", "EVO_PROMOTE_ALPHA": "0.10"},
        lambda: _gate(_TRIAL_SCORES),
    ),
    "gate_candidate_trainval.improve": ({}, lambda: _trainval("parent", "cand")),
    "gate_candidate_trainval.regress": ({}, lambda: _trainval("cand", "parent")),
    "gate_candidate_trainval.noise": (
        {},
        lambda: pg.gate_candidate_trainval(
            parent_body="parent",
            candidate_body="cand",
            train_ids=["t1", "t2"],
            val_ids=["v1"],
            score_fn=_TRAINVAL_FLAT,
            tau_scale=0.5,
        ),
    ),
}

# case name -> the exact ``telemetry["reason"]`` emitted, None when the case emits no
# reason string at all (either the branch sets no reason, or the call returns a scalar).
GOLDEN_REASONS: dict[str, str | None] = {
    "aggregate.pass_rate": None,
    "aggregate.pass_rate_empty": None,
    "aggregate.solve_count": None,
    "conservative_paired.insufficient_pairs": "insufficient_pairs",
    "conservative_paired.no_observed_gain": "no_observed_gain",
    "conservative_paired.no_significant_gain": "no_significant_gain",
    "conservative_paired.paired_improvement": "paired_improvement",
    "conservative_paired.task_regression": "task_regression",
    "conservative_paired.underpowered": "underpowered",
    "delta_gate_enabled.env_disabled": None,
    "delta_gate_enabled.has_validation_split": None,
    "delta_gate_enabled.no_validation_split": None,
    "gate_candidate.no_validation_tasks": "no_validation_tasks",
    "gate_candidate.pass_rate_promoted": None,
    "gate_candidate.pass_rate_rolled_back": None,
    "gate_candidate.pass_rate_tolerance_promoted": None,
    "gate_candidate.perm_lex_env": None,
    "gate_candidate.perm_lex_paired_env": None,
    "gate_candidate.solve_count_promoted": None,
    "gate_candidate_trainval.improve": None,
    "gate_candidate_trainval.noise": None,
    "gate_candidate_trainval.regress": None,
    "gate_zone.improve": None,
    "gate_zone.noise_at_lower_edge": None,
    "gate_zone.noise_at_upper_edge": None,
    "gate_zone.noise_zero": None,
    "gate_zone.regress": None,
    "lex_boundary.p_floor_equals_alpha": None,
    "lex_boundary.p_floor_just_above_alpha": None,
    "lex_boundary.p_value_equals_alpha": None,
    "lex_boundary.p_value_just_above_alpha": None,
    "perm_lex.missing_arm": "inconclusive_missing_arm",
    "perm_lex.no_gain_tied": "no_observed_gain",
    "perm_lex.no_gain_worse": "no_observed_gain",
    "perm_lex.p_value_equals_alpha": None,
    "perm_lex.promote": None,
    "perm_lex.reject_p_above_alpha": None,
    "perm_lex.reject_rate_only_gain": None,
    "perm_lex.underpowered": None,
    "perm_lex_paired.empty": "unpaired_or_empty",
    "perm_lex_paired.no_gain": "no_observed_gain",
    "perm_lex_paired.p_value_equals_alpha": None,
    "perm_lex_paired.promote": None,
    "perm_lex_paired.reject_p_above_alpha": None,
    "perm_lex_paired.underpowered": None,
    "perm_lex_paired.unpaired": "unpaired_or_empty",
    "tau_band.default_scale": None,
    "tau_band.zero_tasks": None,
    "validate_gate_environment.defaults": None,
    "validate_gate_environment.trainval_allowed": None,
}

# case name -> sha256 of the canonical JSON of the whole returned value.
GOLDEN_CASE_HASHES: dict[str, str] = {
    "aggregate.pass_rate": "d9d343e7a76da2730d28d617087cd6e0e917517ae5182439dcdb7b164f7528a0",
    "aggregate.pass_rate_empty": "d8b42cb220d5fd5974287c09df934755174112a8bea29ae02ca97dd50ea1d337",
    "aggregate.solve_count": "69f4efcb304c83cc897ad19c828fba31fd34e247dd75d40cca95f56097d1410d",
    "conservative_paired.insufficient_pairs": (
        "8991b0cdc3b8f3d6c0c17d300390e1af682faf9a4d5973841f141bff02c7e45e"
    ),
    "conservative_paired.no_observed_gain": (
        "6c7466f7068cf37f140420e426115cd08c02c9886dae112352f581e5db17ff95"
    ),
    "conservative_paired.no_significant_gain": (
        "73904489e0bcca7b17ec4f167bce571ceece13e2fdbdaaf5ffd15fad4b3044d4"
    ),
    "conservative_paired.paired_improvement": (
        "aa1864e41e541f21b96a2ae741af53091f9daf0f963258a6b61811f0e85a52da"
    ),
    "conservative_paired.task_regression": (
        "ba4ea35051b5516d28cb35a414bed4a0151cfe40d253e4ec707521b2b188db40"
    ),
    "conservative_paired.underpowered": (
        "5fa4841b6ba754c76857ed8307117b5487c0ab052e4646c8dd05622c38787cf6"
    ),
    "delta_gate_enabled.env_disabled": "1e544d43b574a8e36402bd746d6a6260b4e1169718052ad5354a5b58c6ad01c5",
    "delta_gate_enabled.has_validation_split": "8c5aaab6e1efdec7f7f9f65cc0de77461c36a46e6b7c9ee7a35299dd6e8449ee",
    "delta_gate_enabled.no_validation_split": "1e544d43b574a8e36402bd746d6a6260b4e1169718052ad5354a5b58c6ad01c5",
    "gate_candidate.no_validation_tasks": "4d3819d1834f1879c688e99f563f190de668ac91f81b47c7ab3aa9c988497ecf",
    "gate_candidate.pass_rate_promoted": "04ac00b63453442da416b79026db88661783629a375461786606a68f7acf2645",
    "gate_candidate.pass_rate_rolled_back": "b8b7aacf9b87845f726595973d80d5ce05c166ccaa32f7cea01a326c5083e8f7",
    "gate_candidate.pass_rate_tolerance_promoted": "a55cc08f9111466acd444dd3a640eeddff1838f7478bfb5050b16f98c5ed0b9e",
    "gate_candidate.perm_lex_env": "32cceaacdc8e21af57c836ece2d7adf62592242258b1525e33428704d898f2fd",
    "gate_candidate.perm_lex_paired_env": "7b011368318c837bc8850578a7bb27adc0afcc31912204cd800f4c777db2ebcb",
    "gate_candidate.solve_count_promoted": "da839ebb96ff9ce540b7a713c6957dd13709549e885d568a1e5789e09d7a0d9b",
    "gate_candidate_trainval.improve": "2a23cd8fcce77ee929c48cac9e49e8b217d8b67e5df37a6092001fe673aff243",
    "gate_candidate_trainval.noise": "b6852016e494d903ecd0ed9fe3da87980980319f97d6ce60c549d92a29ee66d3",
    "gate_candidate_trainval.regress": "132522f5757a2972a1d4940032ffeae90cb82243f7cfc6af9d0134e273864630",
    "gate_zone.improve": "e555845f06701f14c5bb7fa317d1f68b629ceb3fa648ef1fd997f8d70e0b9f92",
    "gate_zone.noise_at_lower_edge": "2659c2618d29666340887959b0bcbae006828e3e93ace9912ccc406012191f06",
    "gate_zone.noise_at_upper_edge": "2659c2618d29666340887959b0bcbae006828e3e93ace9912ccc406012191f06",
    "gate_zone.noise_zero": "2659c2618d29666340887959b0bcbae006828e3e93ace9912ccc406012191f06",
    "gate_zone.regress": "8c3c40c61a6a1c28c78c8c8a20ab679d88fce79bef4c98dfa00371942b29e226",
    "lex_boundary.p_floor_equals_alpha": "e91db4a56a928a7cf7769426cfeb52afcee23715a6f49a8b2a4497cf309a6f0d",
    "lex_boundary.p_floor_just_above_alpha": "69ab7da74dd5e37c692025c65c011bf4c941e8300f3d523f58e48950246e32e3",
    "lex_boundary.p_value_equals_alpha": "23b919227cd7d033e68816f2dc72974e8b146fbab4e177119fce008642c18651",
    "lex_boundary.p_value_just_above_alpha": "ab3315c6223e46c726ef3c03d608cabd25191d4d8c9cc1ba2c4c70896a34ba54",
    "perm_lex.missing_arm": "3d45d7b9c73f246a6a79fbe932127b75d3ed020dc99e35e1e8ff5bbc10834051",
    "perm_lex.no_gain_tied": "46a461dfb890256f31e71537ab59056afbe87efeb9caba46ae5291b11dd1f4e5",
    "perm_lex.no_gain_worse": "550476bb6e910c7747da24c35d643ff06a0ae23f2192aa1153f7768c74c6ed60",
    "perm_lex.p_value_equals_alpha": "0ee6ae27cb4fab35ecfe333692f29ee2803cea15429e4aa16a7636adc69c0121",
    "perm_lex.promote": "9234ba3ec6608f054432bf090bfd3b4cd5862c80791d0ece2dfac06d0d409636",
    "perm_lex.reject_p_above_alpha": "3b701e86bac3e559a1ecfde3be929dc1b5f84f8270a253e289280b68dac950c1",
    "perm_lex.reject_rate_only_gain": "a5d9670e9e876d095a6b208a38ead420e53f3c2865d2f8e5402d3d497383a3c9",
    "perm_lex.underpowered": "02c923f0ba062e1813c83eea653d4d2d55b2f904d038ca270eb5da65a47c76b8",
    "perm_lex_paired.empty": "4043cfdebb2b13d71b50ea2c3f998714d5053572c1884c21bd9c0caa28e9c38b",
    "perm_lex_paired.no_gain": "98be371b4d5ef8657bba12ff03f602c12cee23cadcc9fe9f27ad38769db8637d",
    "perm_lex_paired.p_value_equals_alpha": "b538ede28c64ce94691f1b4bf2f406923986a93023a6b62b9d3f428f2c472fc8",
    "perm_lex_paired.promote": "3523122c3c13b121b853e30c61629151130d464cc80cf9c3c6e77bbf05c9a936",
    "perm_lex_paired.reject_p_above_alpha": "1af0242a27f1df434f56b004279eb5c441383aab1c16f36fd6dee8e057c857e2",
    "perm_lex_paired.underpowered": "ce463d8906684570bb6f1e30b89367b9c4a457e8418bb217d9d89ebd837411fe",
    "perm_lex_paired.unpaired": "4043cfdebb2b13d71b50ea2c3f998714d5053572c1884c21bd9c0caa28e9c38b",
    "tau_band.default_scale": "a06b1c7dc68112c3d463e9a3a42db0c15719f00aa20e5216df58cc45ba595273",
    "tau_band.zero_tasks": "d526eac20d05c422e2ed4189cb82704982abae1ddd19b160db1ec2b16865e5bc",
    "validate_gate_environment.defaults": "1c197daef20de3f47eec5e2f735ec6669869d3180cc29f35be4788511e0af0f8",
    "validate_gate_environment.trainval_allowed": "1c197daef20de3f47eec5e2f735ec6669869d3180cc29f35be4788511e0af0f8",
}


def _payload(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return {"promote": result[0], "telemetry": result[1]}
    return {"value": result}


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _reason_of(result: Any) -> str | None:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        value = result[1].get("reason")
        return None if value is None else str(value)
    return None


def _clear_gate_env(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]) -> None:
    for var in _GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in overrides.items():
        monkeypatch.setenv(var, value)


def _run_case(name: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    overrides, thunk = _CASES[name]
    _clear_gate_env(monkeypatch, overrides)
    return thunk()


def test_case_registry_and_golden_tables_agree():
    assert set(_CASES) == set(GOLDEN_REASONS) == set(GOLDEN_CASE_HASHES), (
        f"gate case registry and the golden tables drifted apart; registry: {sorted(_CASES)}"
    )


@pytest.mark.parametrize("name", sorted(_CASES))
def test_gate_reason_string_is_locked(name: str, monkeypatch: pytest.MonkeyPatch):
    actual = _reason_of(_run_case(name, monkeypatch))
    assert actual == GOLDEN_REASONS[name], f"gate reason for {name} drifted; actual: {actual!r}"


@pytest.mark.parametrize("name", sorted(_CASES))
def test_gate_decision_bytes_are_locked(name: str, monkeypatch: pytest.MonkeyPatch):
    canonical = _canonical(_payload(_run_case(name, monkeypatch)))
    actual = _sha(canonical)
    assert actual == GOLDEN_CASE_HASHES[name], (
        f"gate decision for {name} drifted; actual sha256: {actual}\npayload: {canonical}"
    )


def test_every_reason_literal_is_reached_by_a_driven_case(monkeypatch: pytest.MonkeyPatch):
    """Ties section 2's fixtures back to section 1's inventory.

    A ``"reason"`` literal added to the module that no fixture reaches fails here, so
    "every reason string is locked" cannot silently rot as branches are added.
    """
    emitted = {_reason_of(_run_case(name, monkeypatch)) for name in _CASES}
    emitted.discard(None)
    assert emitted == set(_reason_field_literals()), (
        f"driven reasons and the LIVE reason inventory disagree; driven: {sorted(emitted)}. "
        "Refreshing the golden inventory alone will not silence this -- add a fixture."
    )


def test_unpaired_input_returns_exactly_p_value_none_and_unpaired_or_empty():
    """The paired gate's unpaired rejection carries no telemetry beyond the reason."""
    assert pg.perm_lex_paired_decision([[0.5, 0.5]], [[1.0]], ALPHA) == (
        False,
        {"p_value": None, "reason": "unpaired_or_empty"},
    )


def test_p_value_exactly_at_alpha_promotes():
    """``p_value <= alpha``: the boundary is INCLUSIVE, so ``<`` would be wrong."""
    promote, _ = pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_VALUE)
    assert promote is True, "p_value == alpha must promote; the comparison must stay '<='"


def test_p_value_a_hair_above_alpha_rejects():
    """The companion side of the same boundary, so 'always promote' cannot pass."""
    promote, _ = pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_VALUE - 0.01)
    assert promote is False, "p_value just above alpha must reject"


def test_p_floor_exactly_at_alpha_is_not_underpowered():
    """``p_floor > alpha``: the boundary is EXCLUSIVE, so ``>=`` would over-report."""
    _, telemetry = pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_FLOOR)
    assert "status" not in telemetry, (
        f"p_floor == alpha must NOT be UNDERPOWERED; the comparison must stay '>'; got {telemetry}"
    )


def test_p_floor_a_hair_above_alpha_is_underpowered():
    """The companion side of the same boundary, so 'never UNDERPOWERED' cannot pass."""
    _, telemetry = pg._lex_gate_telemetry(_BOUNDARY_JOINT, _BOUNDARY_OBS, _BOUNDARY_P_FLOOR - 0.01)
    assert telemetry.get("status") == "UNDERPOWERED", (
        f"p_floor above alpha must report UNDERPOWERED; got {telemetry}"
    )


# ---------------------------------------------------------------------------
# 3. Race ship decisions: label + confidence vocabulary
# ---------------------------------------------------------------------------

# Every fixture keeps at most 6 (task, trial) units, well under the 16-unit ceiling
# above which _max_stat_null falls back to Monte-Carlo, so the null is enumerated
# exactly and every p-value below is a binary fraction.
_RACE_SINGLE = {"v0": [[0.0, 1.0]]}
_RACE_V0_BEST = {"v0": [[1.0, 1.0, 1.0, 1.0]], "v1": [[0.0, 0.0, 0.0, 0.0]]}
_RACE_V1_SWEEP = {"v0": [[0.0] * 6, [0.0] * 6], "v1": [[1.0] * 6, [1.0] * 6]}
_RACE_V1_FOUR = {"v0": [[0.0] * 4], "v1": [[1.0] * 4]}
_RACE_V1_THREE = {"v0": [[0.0] * 3], "v1": [[1.0] * 3]}
_RACE_V1_TWO = {"v0": [[0.0] * 2], "v1": [[1.0] * 2]}
_RACE_V1_NOISY = {
    "v0": [[0.0, 1.0, 0.0, 1.0, 0.0, 1.0]],
    "v1": [[1.0, 0.0, 1.0, 0.0, 1.0, 1.0]],
}
_RACE_THREE_VERSIONS = {
    "v0": [[0.0] * 5],
    "v2": [[1.0] * 5],
    "v1": [[0.0, 1.0, 0.0, 1.0, 0.0]],
}
# Both arms mean 0.5, and both dicts are built out of version order, so these two
# fixtures pin the version sort AND "ties keep the earliest version".
_RACE_TIED_MEANS = {"v1": [[0.0, 1.0, 0.0, 1.0]], "v0": [[1.0, 0.0, 1.0, 0.0]]}

# case name -> (version_rates, alpha, ship_rule)
_RACE_CASES: dict[str, tuple[dict[str, list[list[float]]], float, str]] = {
    "single_version.argmax": (_RACE_SINGLE, 0.10, "argmax"),
    "single_version.confirm": (_RACE_SINGLE, 0.10, "confirm"),
    "v0_best.argmax": (_RACE_V0_BEST, 0.10, "argmax"),
    "v0_best.confirm": (_RACE_V0_BEST, 0.10, "confirm"),
    "v1_sweep.argmax": (_RACE_V1_SWEEP, 0.10, "argmax"),
    "v1_sweep.confirm": (_RACE_V1_SWEEP, 0.10, "confirm"),
    "v1_four_units.argmax": (_RACE_V1_FOUR, 0.10, "argmax"),
    "v1_four_units.confirm": (_RACE_V1_FOUR, 0.10, "confirm"),
    "v1_three_units.argmax": (_RACE_V1_THREE, 0.10, "argmax"),
    "v1_two_units.argmax": (_RACE_V1_TWO, 0.10, "argmax"),
    "v1_two_units.confirm": (_RACE_V1_TWO, 0.10, "confirm"),
    "v1_noisy.argmax": (_RACE_V1_NOISY, 0.10, "argmax"),
    "v1_noisy.confirm": (_RACE_V1_NOISY, 0.10, "confirm"),
    "three_versions.argmax": (_RACE_THREE_VERSIONS, 0.10, "argmax"),
    "three_versions.confirm": (_RACE_THREE_VERSIONS, 0.10, "confirm"),
    "tied_means.argmax": (_RACE_TIED_MEANS, 0.10, "argmax"),
    "tied_means.confirm": (_RACE_TIED_MEANS, 0.10, "confirm"),
}

# case name -> the exact (shipped_version, label, confidence) the race records.
GOLDEN_RACE_VERDICTS: dict[str, tuple[str, str, str | None]] = {
    "single_version.argmax": ("v0", "reeval_single_version", None),
    "single_version.confirm": ("v0", "reeval_single_version", None),
    "three_versions.argmax": ("v2", "reeval_argmax_confirmed", "confirmed@0.05"),
    "three_versions.confirm": ("v2", "reeval_confirmed_winner", "confirmed@0.05"),
    "tied_means.argmax": ("v0", "reeval_ship_v0", "unconfirmed"),
    "tied_means.confirm": ("v0", "reeval_ship_v0_nonseparation", "unconfirmed"),
    "v0_best.argmax": ("v0", "reeval_ship_v0", "unconfirmed"),
    "v0_best.confirm": ("v0", "reeval_ship_v0_nonseparation", "unconfirmed"),
    "v1_four_units.argmax": ("v1", "reeval_argmax_confirmed", "confirmed@0.10"),
    "v1_four_units.confirm": ("v1", "reeval_confirmed_winner", "confirmed@0.10"),
    "v1_noisy.argmax": ("v1", "reeval_argmax_unconfirmed", "unconfirmed"),
    "v1_noisy.confirm": ("v0", "reeval_ship_v0_nonseparation", "unconfirmed"),
    "v1_sweep.argmax": ("v1", "reeval_argmax_confirmed", "confirmed@0.05"),
    "v1_sweep.confirm": ("v1", "reeval_confirmed_winner", "confirmed@0.05"),
    "v1_three_units.argmax": ("v1", "reeval_argmax_confirmed", "confirmed@0.20"),
    "v1_two_units.argmax": ("v1", "reeval_argmax_underpowered", "underpowered"),
    "v1_two_units.confirm": ("v0", "reeval_ship_v0_underpowered", "underpowered"),
}

# case name -> sha256 of the canonical JSON of the whole returned decision dict.
GOLDEN_RACE_DECISION_HASHES: dict[str, str] = {
    "single_version.argmax": "d0f8b9d7d1c319836d36f0aa0d68ece22546aeff4ec7f99b4025faabcfd9f53c",
    "single_version.confirm": "69bb59899eed2fe855b05db8241f6d7095297dc11270b5e534b124f2dd6de83d",
    "three_versions.argmax": "7f94520aad4c16efc2f0ae397dd4a1a303846969520479cd99136c795d54be3d",
    "three_versions.confirm": "e5645ecd69fb4d106cc5ad6f9ba7dc63bb0356e8043a3b6942821cd76ce70188",
    "tied_means.argmax": "590ebb47d52c278c7170c117702a912b7683af8c7610843e755635c0d3550400",
    "tied_means.confirm": "4f415885bd9db53e36e92990986fc4379e8a6d2afaef630b89c3b228fc9f25fe",
    "v0_best.argmax": "f057fc21f6776434c988b801dffc8fb43326690691550e9bb73e41b22e855c42",
    "v0_best.confirm": "82c6a4a29d184d0366345b5f5dd200e693c5659ce746eb1244ae014e594d4063",
    "v1_four_units.argmax": "0a5adfde82463ebeb830cb17968bbb26ce4405d03209ab5407a2b147c2563949",
    "v1_four_units.confirm": "f401f5788c2349d32acbae8c0efc91101bee906866cf9d392b4e82125e554079",
    "v1_noisy.argmax": "48e7f6326c7458555c7a7383b6024673887fdf995ce7bccf31736f572fa5ecce",
    "v1_noisy.confirm": "1c16a133d290edbd291d0436cc7a2caba10f05db6bdbfca1eccae447a2a3054f",
    "v1_sweep.argmax": "8bc71ada5c9fcba50adbaba26a2a384f486d31b86d3214a59d6d8cc223e67094",
    "v1_sweep.confirm": "0816c913931ff528707700d1be28c7ae1dc38b3e0721f35696cbd800b3b54dd7",
    "v1_three_units.argmax": "836f5a1e5b775b0a1069593f638081f86f99ae31817aeccb7d066240daf09b42",
    "v1_two_units.argmax": "8f7f6d96ed89a8155ef9a1946b20885f03e8a1ae6f716d997983793534f54909",
    "v1_two_units.confirm": "ecdc6535608dbbd1061d0d10609e633f447f86c96cd4894df4356963113281d3",
}

# (p_value, p_floor) -> the exact confidence label. Each threshold is probed ON its edge
# and one ulp-ish step past it, so both the 0.05 / 0.10 / 0.20 constants and the
# inclusive `p_value <= level` / exclusive `p_floor > 0.20` comparisons are pinned.
GOLDEN_RACE_CONFIDENCE: dict[tuple[float, float], str] = {
    (0.0, 0.0): "confirmed@0.05",
    (0.05, 0.0): "confirmed@0.05",
    (0.06, 0.0): "confirmed@0.10",
    (0.10, 0.0): "confirmed@0.10",
    (0.11, 0.0): "confirmed@0.20",
    (0.20, 0.0): "confirmed@0.20",
    (0.21, 0.0): "unconfirmed",
    (1.0, 0.0): "unconfirmed",
    (0.0, 0.20): "confirmed@0.05",
    (0.0, 0.21): "underpowered",
}


def test_race_case_registry_and_golden_tables_agree():
    assert set(_RACE_CASES) == set(GOLDEN_RACE_VERDICTS) == set(GOLDEN_RACE_DECISION_HASHES), (
        f"race case registry and the golden tables drifted apart; registry: {sorted(_RACE_CASES)}"
    )


def _race_decision(name: str) -> dict[str, Any]:
    version_rates, alpha, ship_rule = _RACE_CASES[name]
    return pg.race_ship_decision(version_rates, alpha=alpha, ship_rule=ship_rule)


@pytest.mark.parametrize("name", sorted(_RACE_CASES))
def test_race_verdict_is_locked(name: str):
    result = _race_decision(name)
    actual = (result["shipped_version"], result["label"], result["confidence"])
    assert actual == GOLDEN_RACE_VERDICTS[name], (
        f"race verdict for {name} drifted; actual: {actual!r}"
    )


@pytest.mark.parametrize("name", sorted(_RACE_CASES))
def test_race_decision_bytes_are_locked(name: str):
    canonical = _canonical(_race_decision(name))
    actual = _sha(canonical)
    assert actual == GOLDEN_RACE_DECISION_HASHES[name], (
        f"race decision for {name} drifted; actual sha256: {actual}\npayload: {canonical}"
    )


@pytest.mark.parametrize("probe", sorted(GOLDEN_RACE_CONFIDENCE))
def test_race_confidence_label_is_locked(probe: tuple[float, float]):
    p_value, p_floor = probe
    actual = pg._race_confidence(p_value, p_floor)
    assert actual == GOLDEN_RACE_CONFIDENCE[probe], (
        f"_race_confidence{probe!r} drifted; actual: {actual!r}"
    )


def test_every_race_label_literal_is_reached_by_a_driven_case():
    """Ties section 3's fixtures back to section 1's inventory.

    A label literal added to the module that no fixture can reach fails here, so the
    "every label is locked" claim cannot silently rot as branches are added.
    """
    emitted = {_race_decision(name)["label"] for name in _RACE_CASES}
    literals = [text for text in _race_vocabulary() if text.startswith("reeval")]
    unreached = [
        literal
        for literal in literals
        if not any(label == literal or label.startswith(literal) for label in emitted)
    ]
    assert not unreached, (
        f"race label literals no fixture reaches: {unreached}; emitted: {sorted(emitted)}"
    )


def test_every_race_confidence_literal_is_reached_by_a_driven_case():
    emitted = {pg._race_confidence(*probe) for probe in GOLDEN_RACE_CONFIDENCE}
    unreached = [
        stem
        for stem in _race_confidence_return_stems()
        if not any(label.startswith(stem) for label in emitted)
    ]
    assert not unreached, (
        f"confidence labels no probe reaches: {unreached}; emitted: {sorted(emitted)}"
    )


# ---------------------------------------------------------------------------
# 4. Editor-facing reason prose (this is what lands in the next writer prompt)
# ---------------------------------------------------------------------------

COARSE_REJECTION_TEXT = (
    "Rejected: this edit did not improve the skill on held-out tasks (it did not "
    "generalize). Make a more general fix to the procedure for the failure category, "
    "and do NOT remove working content that other tasks rely on."
)

_NUMERIC_TELEMETRY = {
    "parent_mean": 0.4,
    "candidate_mean": 0.25,
    "n_val": 2,
    "metric": "pass_rate",
}

_EDITOR_CASES: dict[str, Callable[[], str]] = {
    "blind": lambda: pg.editor_facing_gate_reason(_NUMERIC_TELEMETRY, blind=True),
    "blind_empty_telemetry": lambda: pg.editor_facing_gate_reason({}, blind=True),
    "verbose_strict": lambda: pg.editor_facing_gate_reason(_NUMERIC_TELEMETRY, blind=False),
    "verbose_tolerance": lambda: pg.editor_facing_gate_reason(
        {**_NUMERIC_TELEMETRY, "tolerance": 0.05}, blind=False
    ),
    "verbose_solve_count_label": lambda: pg.editor_facing_gate_reason(
        {**_NUMERIC_TELEMETRY, "metric": "solve_count"}, blind=False
    ),
    "verbose_empty_telemetry": lambda: pg.editor_facing_gate_reason({}, blind=False),
    "verbose_value_error_fallback": lambda: pg.editor_facing_gate_reason(
        {**_NUMERIC_TELEMETRY, "parent_mean": "oops"}, blind=False
    ),
    "verbose_type_error_fallback": lambda: pg.editor_facing_gate_reason(
        {**_NUMERIC_TELEMETRY, "candidate_mean": None}, blind=False
    ),
}

GOLDEN_EDITOR_REASON_HASHES: dict[str, str] = {
    "blind": "f45389ae65acce3bf08a4167704d91aeba86b328b329cc7b6a8cf0aaeb3eb664",
    "blind_empty_telemetry": "f45389ae65acce3bf08a4167704d91aeba86b328b329cc7b6a8cf0aaeb3eb664",
    "verbose_empty_telemetry": "9ee5b12cd30c7682c8fdafcf715dfd605e987cc16a6d7a7202d8cdce35dd6ff6",
    "verbose_solve_count_label": "062b976ccbe2b662e57fb502d1cb698c2ed5cfdb59b31ee855d2ae90963703bd",
    "verbose_strict": "58cd7f52d459721ed87104209376815e57a4bb0d1b436e1651ed977fe6037f57",
    "verbose_tolerance": "d3ed52f3d3998da36244b4e7abd73d62b10f16db19ab1c411249fdd1528daf37",
    "verbose_type_error_fallback": "f45389ae65acce3bf08a4167704d91aeba86b328b329cc7b6a8cf0aaeb3eb664",
    "verbose_value_error_fallback": "f45389ae65acce3bf08a4167704d91aeba86b328b329cc7b6a8cf0aaeb3eb664",
}


def test_coarse_rejection_text_is_locked():
    actual = pg.editor_facing_gate_reason({}, blind=True)
    assert actual == COARSE_REJECTION_TEXT, f"blind rejection prose drifted; actual: {actual!r}"


def test_editor_reason_registry_and_golden_table_agree():
    assert set(_EDITOR_CASES) == set(GOLDEN_EDITOR_REASON_HASHES), (
        "editor reason cases and the golden table drifted apart"
    )


@pytest.mark.parametrize("name", sorted(_EDITOR_CASES))
def test_editor_facing_reason_is_locked(name: str):
    text = _EDITOR_CASES[name]()
    actual = _sha(text)
    assert actual == GOLDEN_EDITOR_REASON_HASHES[name], (
        f"editor-facing reason {name} drifted; actual sha256: {actual}\ntext: {text!r}"
    )


# ---------------------------------------------------------------------------
# 5. Raised gate error messages
# ---------------------------------------------------------------------------


def _raises(thunk: Callable[[], Any]) -> Callable[[], str]:
    def run() -> str:
        try:
            thunk()
        except (ValueError, RuntimeError) as exc:
            return f"{type(exc).__name__}: {exc}"
        raise AssertionError("expected the gate to raise")

    return run


_ERROR_CASES: dict[str, tuple[dict[str, str], Callable[[], str]]] = {
    "conservative_paired.alpha_out_of_range": (
        {},
        _raises(lambda: pg.conservative_paired_gate({"t1": [0.5]}, {"t1": [0.5]}, alpha=0.0)),
    ),
    "conservative_paired.min_pairs_not_positive": (
        {},
        _raises(lambda: pg.conservative_paired_gate({"t1": [0.5]}, {"t1": [0.5]}, min_pairs=0)),
    ),
    "conservative_paired.task_sets_differ": (
        {},
        _raises(lambda: pg.conservative_paired_gate({"t1": [0.5]}, {"t2": [0.5]})),
    ),
    "conservative_paired.trial_count_mismatch": (
        {},
        _raises(lambda: pg.conservative_paired_gate({"t1": [0.5, 0.5]}, {"t1": [0.5]})),
    ),
    "conservative_paired.rate_out_of_unit_interval": (
        {},
        _raises(lambda: pg.conservative_paired_gate({"t1": [0.5]}, {"t1": [2.0]})),
    ),
    "tau_band.negative_scale": ({}, _raises(lambda: pg.tau_band(2, 1, -0.5))),
    "tau_band.non_finite_scale": ({}, _raises(lambda: pg.tau_band(2, 1, float("inf")))),
    "aggregate.unknown_metric": (
        {},
        _raises(lambda: pg._aggregate_gate_metric([[0.5]], "mystery_mean")),
    ),
    "validate_gate_environment.unknown_metric": (
        {"EVO_GATE_METRIC": "nonsense"},
        _raises(pg.validate_gate_environment),
    ),
    "validate_gate_environment.trainval_not_allowed": (
        {"EVO_GATE_METRIC": "solve_count_trainval"},
        _raises(pg.validate_gate_environment),
    ),
    "validate_gate_environment.non_finite_delta": (
        {"EVO_PROMOTE_DELTA": "nan"},
        _raises(pg.validate_gate_environment),
    ),
    "validate_gate_environment.alpha_out_of_range": (
        {"EVO_GATE_METRIC": "perm_lex", "EVO_PROMOTE_ALPHA": "2"},
        _raises(pg.validate_gate_environment),
    ),
    "validate_gate_environment.bad_tau_scale": (
        {"EVO_GATE_METRIC": "solve_count_trainval", "EVO_GATE_TAU_SCALE": "-1"},
        _raises(lambda: pg.validate_gate_environment(allow_trainval=True)),
    ),
    "gate_candidate.unknown_metric": (
        {"EVO_GATE_METRIC": "nonsense"},
        _raises(lambda: _gate(_PASS_RATE_SCORES)),
    ),
    "gate_candidate.non_finite_delta": (
        {},
        _raises(lambda: _gate(_PASS_RATE_SCORES, delta=float("nan"), metric="pass_rate")),
    ),
    "gate_candidate.alpha_out_of_range": (
        {"EVO_GATE_METRIC": "perm_lex", "EVO_PROMOTE_ALPHA": "2"},
        _raises(lambda: _gate(_TRIAL_SCORES)),
    ),
    "race.unknown_ship_rule": (
        {},
        _raises(lambda: pg.race_ship_decision(_RACE_SINGLE, alpha=0.10, ship_rule="nonsense")),
    ),
    "race.no_versions": ({}, _raises(lambda: pg.race_ship_decision({}, alpha=0.10))),
    "race.bad_version_name": (
        {},
        _raises(lambda: pg.race_ship_decision({"best": [[1.0]]}, alpha=0.10)),
    ),
    "race.shape_mismatch": (
        {},
        _raises(lambda: pg.race_ship_decision({"v0": [[1.0]], "v1": [[1.0, 1.0]]}, alpha=0.10)),
    ),
    "race.no_units": ({}, _raises(lambda: pg.race_ship_decision({"v0": [], "v1": []}, alpha=0.10))),
}

_METRIC_TAIL = "; expected one of ('pass_rate', 'solve_count', 'perm_lex', 'perm_lex_paired')"
_SHIP_RULE_TAIL = "; expected one of ('argmax', 'confirm')"

# The ONLY raise fragments no fixture below drives: reaching them needs a joint support
# above 2_000_000 states, which is not a small fixture. They stay locked statically by
# GOLDEN_RAISE_MESSAGE_FRAGMENTS.
GOLDEN_UNDRIVEN_RAISE_FRAGMENTS = (
    "perm_lex joint distribution too large; reduce validation tasks or gate trials",
    "perm_lex_paired joint distribution too large; reduce validation tasks or gate trials",
)

GOLDEN_ERROR_MESSAGES: dict[str, str] = {
    "aggregate.unknown_metric": ("ValueError: unknown gate metric 'mystery_mean'" + _METRIC_TAIL),
    "conservative_paired.alpha_out_of_range": "ValueError: alpha must be finite and in (0, 1)",
    "conservative_paired.min_pairs_not_positive": "ValueError: min_pairs must be positive",
    "conservative_paired.rate_out_of_unit_interval": (
        "ValueError: task 't1' pass rates must be finite and in [0, 1]"
    ),
    "conservative_paired.task_sets_differ": (
        "ValueError: parent and candidate must contain the same tasks"
    ),
    "conservative_paired.trial_count_mismatch": (
        "ValueError: task 't1' must have the same paired trial count"
    ),
    "race.bad_version_name": "ValueError: race version must look like vN, got 'best'",
    "race.no_units": "ValueError: race has no (task, trial) units",
    "race.no_versions": "ValueError: race_ship_decision: no versions",
    "race.shape_mismatch": (
        "ValueError: race arms must share the (task, trial) shape, got [(1,), (2,)]"
    ),
    "race.unknown_ship_rule": (
        "ValueError: unknown EVO_RACE_SHIP_RULE 'nonsense'" + _SHIP_RULE_TAIL
    ),
    "gate_candidate.alpha_out_of_range": "ValueError: EVO_PROMOTE_ALPHA 2.0 out of range (0, 1)",
    "gate_candidate.non_finite_delta": "ValueError: delta and tolerance must be finite",
    "gate_candidate.unknown_metric": (
        "ValueError: unknown EVO_GATE_METRIC 'nonsense'" + _METRIC_TAIL
    ),
    "tau_band.negative_scale": (
        "ValueError: EVO_GATE_TAU_SCALE must be finite and non-negative, got -0.5"
    ),
    "tau_band.non_finite_scale": (
        "ValueError: EVO_GATE_TAU_SCALE must be finite and non-negative, got inf"
    ),
    "validate_gate_environment.alpha_out_of_range": (
        "ValueError: EVO_PROMOTE_ALPHA 2.0 out of range (0, 1)"
    ),
    "validate_gate_environment.bad_tau_scale": (
        "ValueError: EVO_GATE_TAU_SCALE must be finite and non-negative, got -1.0"
    ),
    "validate_gate_environment.non_finite_delta": (
        "ValueError: EVO_PROMOTE_DELTA and EVO_PROMOTE_TOLERANCE must be finite"
    ),
    "validate_gate_environment.trainval_not_allowed": (
        "ValueError: unknown EVO_GATE_METRIC 'solve_count_trainval'" + _METRIC_TAIL
    ),
    "validate_gate_environment.unknown_metric": (
        "ValueError: unknown EVO_GATE_METRIC 'nonsense'" + _METRIC_TAIL
    ),
}


def test_error_case_registry_and_golden_table_agree():
    assert set(_ERROR_CASES) == set(GOLDEN_ERROR_MESSAGES), (
        "gate error cases and the golden table drifted apart"
    )


def test_only_the_oversize_joint_runtime_errors_are_undriven():
    """Ties section 5's fixtures back to section 1's raise inventory.

    A ``raise`` added to the module that no fixture drives fails here, so "every gate
    error message is locked" states an accurate, maintained limitation rather than an
    unchecked claim.
    """
    driven = "\n".join(GOLDEN_ERROR_MESSAGES.values())
    undriven = tuple(text for text in _raise_message_fragments() if text not in driven)
    assert undriven == GOLDEN_UNDRIVEN_RAISE_FRAGMENTS, (
        f"the set of raise fragments no fixture drives changed; actual: {undriven!r}"
    )


@pytest.mark.parametrize("name", sorted(_ERROR_CASES))
def test_gate_error_message_is_locked(name: str, monkeypatch: pytest.MonkeyPatch):
    overrides, thunk = _ERROR_CASES[name]
    _clear_gate_env(monkeypatch, overrides)
    actual = thunk()
    assert actual == GOLDEN_ERROR_MESSAGES[name], (
        f"gate error message for {name} drifted; actual: {actual!r}"
    )
