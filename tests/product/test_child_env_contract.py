"""The environment contract for untrusted solution and verifier children.

Generated code and verifiers run under the operator's host account, so the set
of variables that reaches them is a security boundary, not a convenience. This
file pins that set by behaviour: widening it should require editing a literal
here, in a diff a reviewer can see.

``docs/child-process-environment.md`` states the contract in prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

from evolution.eval.child_env import (
    _RUNTIME_ENV_KEYS,
    PROVIDER_SECRET_NAMES,
    minimal_child_env,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "evolution"

# Every name a child may inherit when the caller names nothing extra.
FORWARDED_BY_DEFAULT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "TMPDIR",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "LD_LIBRARY_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
)

# Host and session state a child has no business seeing.
HOST_STATE = {
    "HOME": "/home/operator",
    "USER": "operator",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "AWS_SECRET_ACCESS_KEY": "host-credential",
    "KUBECONFIG": "/home/operator/.kube/config",
    "EVO_PROJECT_DIR": "/home/operator/project",
}


def _full_source_env() -> dict[str, str]:
    """An environment holding every name the contract has an opinion about.

    Seeded from the *live* ``_RUNTIME_ENV_KEYS`` as well as the golden set. Built
    from the golden alone, a name newly added to production could never appear in
    the source mapping, so ``minimal_child_env`` could not emit it and the
    exact-set assertion below would keep passing — the widening this file exists
    to catch would be invisible to it.
    """
    env = {name: f"value-of-{name}" for name in FORWARDED_BY_DEFAULT | _RUNTIME_ENV_KEYS}
    env.update({name: "provider-secret" for name in PROVIDER_SECRET_NAMES})
    env.update(HOST_STATE)
    return env


def test_default_forwarded_set_is_pinned() -> None:
    assert set(minimal_child_env(_full_source_env())) == FORWARDED_BY_DEFAULT


def test_no_provider_credential_is_forwarded_by_default() -> None:
    forwarded = minimal_child_env(_full_source_env())

    assert PROVIDER_SECRET_NAMES.isdisjoint(forwarded)


def test_no_host_or_session_state_is_forwarded_by_default() -> None:
    forwarded = minimal_child_env(_full_source_env())

    assert set(HOST_STATE).isdisjoint(forwarded)


def test_absent_names_are_omitted_rather_than_blanked() -> None:
    forwarded = minimal_child_env({"PATH": "/usr/bin"})

    assert forwarded == {"PATH": "/usr/bin"}


def test_allow_is_the_only_widening_seam() -> None:
    forwarded = minimal_child_env(_full_source_env(), allow={"EVO_PROJECT_DIR"})

    assert forwarded["EVO_PROJECT_DIR"] == "/home/operator/project"
    assert "KUBECONFIG" not in forwarded


def test_allow_can_readmit_a_named_provider_credential() -> None:
    """A child whose job is to call a provider needs that one key, and no other."""
    forwarded = minimal_child_env(_full_source_env(), allow={"OPENAI_API_KEY"})

    assert forwarded["OPENAI_API_KEY"] == "provider-secret"
    assert "ANTHROPIC_API_KEY" not in forwarded


def test_pythonpath_reaches_children_so_import_time_writes_are_load_bearing() -> None:
    """``PYTHONPATH`` is forwarded, which is why import purity is a measurement gate.

    A module-scope ``os.environ["PYTHONPATH"] = ...`` anywhere in the import
    graph silently re-points every untrusted child at another checkout. The runs
    still complete and still write results. ``test_import_purity.py`` is the
    other half of this contract.
    """
    forwarded = minimal_child_env({"PYTHONPATH": "/checkout/src"})

    assert forwarded["PYTHONPATH"] == "/checkout/src"


#: Modules that spawn the untrusted children this contract is about. Other
#: packages spawn trusted utilities (git, streamlit) and are out of scope.
UNTRUSTED_SPAWN_MODULES = (
    "eval/runner.py",
    "eval/solver.py",
    "eval/layouts/evo.py",
    "eval/layouts/sb_bench.py",
)

SPAWN_FUNCTIONS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``subprocess.*`` / ``asyncio.create_subprocess_*`` call in *tree*."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attribute = node.func.attr
        is_subprocess = (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and attribute in SPAWN_FUNCTIONS
        )
        if is_subprocess or attribute.startswith("create_subprocess"):
            calls.append(node)
    return calls


def test_every_untrusted_spawn_passes_an_explicit_environment() -> None:
    """A spawn with no ``env=`` inherits the operator's whole environment.

    This is the shape the source-text scan below cannot see: there is nothing to
    grep for when the argument is simply absent.
    """
    implicit = []
    for relative in UNTRUSTED_SPAWN_MODULES:
        path = SRC / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            if not any(keyword.arg == "env" for keyword in call.keywords):
                implicit.append(f"{relative}:{call.lineno}")

    assert implicit == []


def _env_kwargs_naming_the_process_environment() -> list[str]:
    offenders = []
    for relative in UNTRUSTED_SPAWN_MODULES:
        path = SRC / relative
        source = path.read_text(encoding="utf-8")
        for call in _spawn_calls(ast.parse(source, filename=str(path))):
            for keyword in call.keywords:
                if keyword.arg != "env":
                    continue
                segment = ast.get_source_segment(source, keyword.value) or ""
                if "os.environ" in segment:
                    offenders.append(f"{relative}:{keyword.value.lineno}")
    return offenders


def test_no_untrusted_spawn_names_the_process_environment_inline() -> None:
    """Narrow by construction: this reads the ``env=`` expression as source text.

    It catches ``env=os.environ`` and ``env={**os.environ, ...}`` written at the
    call. It does **not** catch an environment assembled into a variable earlier,
    returned by a helper, or passed positionally to ``os.execve``. Those shapes
    exist elsewhere in the tree — ``eval/workspace_guard.py`` deliberately builds
    ``dict(os.environ)`` for the in-process guard — so this is one layer of a
    contract, not proof that no child ever sees the raw environment.
    """
    assert _env_kwargs_naming_the_process_environment() == []
