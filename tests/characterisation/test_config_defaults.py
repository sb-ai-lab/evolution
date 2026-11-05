"""Characterisation lock: what every ``evolution.config`` knob reads and returns.

``evolution.config`` accessors read ``os.environ`` on each call, so their *unset*
value is the behaviour every run inherits when an operator sets nothing, and
their parsing decides what a set value actually means. A repackaging that drops
an accessor, points it at a different ``EVO_*`` variable, nudges a numeric
default, or loosens how a set value is parsed silently changes what the
optimizer does. This lock fails loudly so any such change has to be deliberate.

Five things are frozen:

* ``GOLDEN_ACCESSOR_DEFAULTS`` — the observed value of each accessor with every
  ``EVO_*`` variable unset, recorded as ``"<type name>:<repr>"`` so a bool that
  becomes an int is caught. An accessor that raises with the environment unset
  is recorded as ``"raises <ExceptionType>"`` rather than skipped.
* the *set* of accessor names, enumerated from the module (public module-level
  functions), so a knob cannot be added or removed silently.
* ``GOLDEN_ENV_VARS`` — the ``EVO_*`` names the module reads anywhere, taken
  from whole string literals in its AST, so a rename is caught too.
* ``GOLDEN_ACCESSOR_ENV_VARS`` — the *pairing*: which ``EVO_*`` name(s) each
  accessor reads in its own body. Without this, swapping the variables two
  accessors read leaves both sets above unchanged and passes unnoticed.
* ``GOLDEN_SET_VALUE_BEHAVIOUR`` — the parsed outcome of each knob for
  representative *set* values, including falsy-but-non-empty ones (``"0"``,
  ``"true"``, ``"yes"``), empty strings, untrimmed whitespace, and malformed
  input that must hit the integer fallback or raise. Without this, rewriting
  ``os.environ.get("EVO_REFLECT_BLIND") == "1"`` as ``bool(os.environ.get(...))``
  — which turns *any* non-empty value into blind mode — changes nothing frozen.

What this lock deliberately does NOT cover:

* Only ``evolution.config`` is inspected. ``EVO_*`` variables read directly in
  other modules are outside this lock entirely.
* ``GOLDEN_ACCESSOR_ENV_VARS`` is derived from each accessor's *own* source, so
  an accessor that delegates records no direct variable —
  ``train_optimization_enabled`` maps to ``()`` and its dependency on
  ``EVO_OPTIMIZATION_TARGET`` is locked only by its ``GOLDEN_SET_VALUE_BEHAVIOUR``
  rows. Variables read via a helper called with a non-literal name would be
  invisible here too.
* Only the set values listed in ``GOLDEN_SET_VALUE_BEHAVIOUR`` are frozen; other
  values, and combinations of several ``EVO_*`` variables set at once, are not.
* The warning log emitted for a malformed integer is not asserted — only the
  value the accessor returns.
* Nothing about how callers consume these knobs.

To refresh after an INTENTIONAL change, run the test and copy the actual values
out of the assertion messages (each failing set-value row names its accessor,
variable, raw value and the outcome observed), or print the first three
directly from the repo root:

    python -c "import os; \\
        [os.environ.pop(k) for k in list(os.environ) if k.startswith('EVO_')]; \\
        from tests.characterisation.test_config_defaults import _observed_defaults, \\
            _env_vars_read, _env_vars_by_accessor; \\
        print(_observed_defaults()); print(sorted(_env_vars_read())); \\
        print(_env_vars_by_accessor())"

Update the literals below in the *same* commit as the change.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import textwrap
from pathlib import Path

import pytest

from evolution import config

GOLDEN_ACCESSOR_DEFAULTS = {
    "leak_ngram_min_chars": "int:50",
    "llm_max_tokens": "int:16384",
    "llm_reasoning_effort": "NoneType:None",
    "optimization_target": "str:'general'",
    "reflect_blind": "bool:False",
    "reflect_drop_mixed": "bool:False",
    "reflect_meta_skill_chars": "int:4000",
    "reflect_success_k": "int:3",
    "train_optimization_enabled": "bool:False",
}

GOLDEN_ENV_VARS = frozenset(
    {
        "EVO_LEAK_NGRAM_MIN_CHARS",
        "EVO_LLM_MAX_TOKENS",
        "EVO_LLM_REASONING_EFFORT",
        "EVO_OPTIMIZATION_TARGET",
        "EVO_REFLECT_BLIND",
        "EVO_REFLECT_DROP_MIXED",
        "EVO_REFLECT_META_SKILL_CHARS",
        "EVO_REFLECT_SUCCESS_K",
    }
)

GOLDEN_ACCESSOR_ENV_VARS = {
    "leak_ngram_min_chars": ("EVO_LEAK_NGRAM_MIN_CHARS",),
    "llm_max_tokens": ("EVO_LLM_MAX_TOKENS",),
    "llm_reasoning_effort": ("EVO_LLM_REASONING_EFFORT",),
    "optimization_target": ("EVO_OPTIMIZATION_TARGET",),
    "reflect_blind": ("EVO_REFLECT_BLIND",),
    "reflect_drop_mixed": ("EVO_REFLECT_DROP_MIXED",),
    "reflect_meta_skill_chars": ("EVO_REFLECT_META_SKILL_CHARS",),
    "reflect_success_k": ("EVO_REFLECT_SUCCESS_K",),
    "train_optimization_enabled": (),
}

GOLDEN_SET_VALUE_BEHAVIOUR = (
    ("reflect_blind", "EVO_REFLECT_BLIND", "1", "bool:True"),
    ("reflect_blind", "EVO_REFLECT_BLIND", "0", "bool:False"),
    ("reflect_blind", "EVO_REFLECT_BLIND", "true", "bool:False"),
    ("reflect_blind", "EVO_REFLECT_BLIND", "", "bool:False"),
    ("reflect_drop_mixed", "EVO_REFLECT_DROP_MIXED", "1", "bool:True"),
    ("reflect_drop_mixed", "EVO_REFLECT_DROP_MIXED", "0", "bool:False"),
    ("reflect_drop_mixed", "EVO_REFLECT_DROP_MIXED", "yes", "bool:False"),
    ("reflect_drop_mixed", "EVO_REFLECT_DROP_MIXED", "", "bool:False"),
    ("llm_reasoning_effort", "EVO_LLM_REASONING_EFFORT", "high", "str:'high'"),
    ("llm_reasoning_effort", "EVO_LLM_REASONING_EFFORT", " high ", "str:' high '"),
    ("llm_reasoning_effort", "EVO_LLM_REASONING_EFFORT", "0", "str:'0'"),
    ("llm_reasoning_effort", "EVO_LLM_REASONING_EFFORT", "", "str:''"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "train", "str:'train'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "TRAIN", "str:'train'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", " train ", "str:'train'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "general", "str:'general'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "heldout", "str:'general'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "held-out", "str:'general'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "", "str:'general'"),
    ("optimization_target", "EVO_OPTIMIZATION_TARGET", "bogus", "raises ValueError"),
    ("train_optimization_enabled", "EVO_OPTIMIZATION_TARGET", "train", "bool:True"),
    ("train_optimization_enabled", "EVO_OPTIMIZATION_TARGET", "TRAIN", "bool:True"),
    ("train_optimization_enabled", "EVO_OPTIMIZATION_TARGET", "general", "bool:False"),
    ("train_optimization_enabled", "EVO_OPTIMIZATION_TARGET", "", "bool:False"),
    ("train_optimization_enabled", "EVO_OPTIMIZATION_TARGET", "bogus", "raises ValueError"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "7", "int:7"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "0", "int:0"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "-1", "int:-1"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", " 5 ", "int:5"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "3.5", "int:3"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "abc", "int:3"),
    ("reflect_success_k", "EVO_REFLECT_SUCCESS_K", "", "int:3"),
    ("reflect_meta_skill_chars", "EVO_REFLECT_META_SKILL_CHARS", "12345", "int:12345"),
    ("reflect_meta_skill_chars", "EVO_REFLECT_META_SKILL_CHARS", "0", "int:0"),
    ("reflect_meta_skill_chars", "EVO_REFLECT_META_SKILL_CHARS", "abc", "int:4000"),
    ("reflect_meta_skill_chars", "EVO_REFLECT_META_SKILL_CHARS", "", "int:4000"),
    ("leak_ngram_min_chars", "EVO_LEAK_NGRAM_MIN_CHARS", "80", "int:80"),
    ("leak_ngram_min_chars", "EVO_LEAK_NGRAM_MIN_CHARS", "0", "int:0"),
    ("leak_ngram_min_chars", "EVO_LEAK_NGRAM_MIN_CHARS", "abc", "int:50"),
    ("leak_ngram_min_chars", "EVO_LEAK_NGRAM_MIN_CHARS", "", "int:50"),
)

_ENV_VAR_PATTERN = re.compile(r"EVO_[A-Z0-9_]+")


def _accessor_names() -> set[str]:
    """Public zero-argument accessors defined in ``evolution.config``."""
    names = set()
    for name, obj in vars(config).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != config.__name__:
            continue
        required = [
            parameter
            for parameter in inspect.signature(obj).parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind is not inspect.Parameter.VAR_POSITIONAL
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ]
        if not required:
            names.add(name)
    return names


def _observe(accessor) -> str:
    """Record an accessor's outcome as a comparable string, exceptions included."""
    try:
        value = accessor()
    except Exception as exc:  # noqa: BLE001 - "it raises" is itself a locked outcome
        return f"raises {type(exc).__name__}"
    return f"{type(value).__name__}:{value!r}"


def _observed_defaults() -> dict[str, str]:
    return {name: _observe(getattr(config, name)) for name in sorted(_accessor_names())}


def _env_var_literals(tree: ast.AST) -> set[str]:
    """``EVO_*`` names appearing as *whole* string literals anywhere under ``tree``.

    Whole-literal matching is what makes this a read rather than a mention: a
    docstring that says ``EVO_REFLECT_BLIND=1`` is one long string constant and
    never matches, so prose can neither forge nor mask a variable.
    """
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _ENV_VAR_PATTERN.fullmatch(node.value)
    }


def _env_vars_read() -> set[str]:
    """``EVO_*`` names read anywhere in ``config.py``."""
    return _env_var_literals(ast.parse(Path(config.__file__).read_text(encoding="utf-8")))


def _env_vars_by_accessor() -> dict[str, tuple[str, ...]]:
    """``EVO_*`` names each public accessor reads in its *own* body.

    A delegating accessor maps to ``()`` — the variable its callee reads is not
    attributed to it.
    """
    mapping: dict[str, tuple[str, ...]] = {}
    for name in sorted(_accessor_names()):
        source = textwrap.dedent(inspect.getsource(getattr(config, name)))
        mapping[name] = tuple(sorted(_env_var_literals(ast.parse(source))))
    return mapping


@pytest.fixture
def unset_evo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every ``EVO_*`` variable so this machine's env cannot forge a default."""
    for name in GOLDEN_ENV_VARS | _env_vars_read() | set(os.environ):
        if name.startswith("EVO_"):
            monkeypatch.delenv(name, raising=False)


def test_every_config_accessor_has_a_golden_default():
    """No knob may be added or removed without a recorded default (and vice versa)."""
    assert _accessor_names() == set(GOLDEN_ACCESSOR_DEFAULTS), (
        "evolution.config accessors and the golden default set drifted apart"
    )


def test_config_defaults_match_golden(unset_evo_env):
    observed = _observed_defaults()
    drift = {
        name: actual
        for name, actual in observed.items()
        if name in GOLDEN_ACCESSOR_DEFAULTS and actual != GOLDEN_ACCESSOR_DEFAULTS[name]
    }
    assert not drift, (
        f"config defaults changed; update GOLDEN_ACCESSOR_DEFAULTS in the same commit: {drift}"
    )


def test_config_reads_only_golden_env_vars():
    """A renamed or newly read ``EVO_*`` variable must be an explicit change."""
    assert _env_vars_read() == set(GOLDEN_ENV_VARS), (
        "the EVO_* names read by evolution.config and GOLDEN_ENV_VARS drifted apart"
    )


def test_each_accessor_reads_its_golden_env_var():
    """The accessor -> variable pairing is frozen, not just the two sets."""
    assert _env_vars_by_accessor() == GOLDEN_ACCESSOR_ENV_VARS, (
        "an accessor now reads a different EVO_* variable; update "
        "GOLDEN_ACCESSOR_ENV_VARS in the same commit"
    )


@pytest.mark.parametrize(
    ("accessor_name", "env_name", "raw_value", "expected"), GOLDEN_SET_VALUE_BEHAVIOUR
)
def test_config_parses_set_values_as_golden(
    unset_evo_env,
    monkeypatch: pytest.MonkeyPatch,
    accessor_name: str,
    env_name: str,
    raw_value: str,
    expected: str,
):
    """How a *set* value is parsed is behaviour, and it is frozen too."""
    monkeypatch.setenv(env_name, raw_value)
    observed = _observe(getattr(config, accessor_name))
    assert observed == expected, (
        f"config.{accessor_name}() with {env_name}={raw_value!r} now yields {observed} "
        f"(was {expected}); update GOLDEN_SET_VALUE_BEHAVIOUR in the same commit"
    )
