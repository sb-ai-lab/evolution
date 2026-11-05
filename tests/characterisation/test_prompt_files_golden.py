"""Characterisation lock: the prompt bytes that actually reach the LLM.

Prompt wording is content-addressed input to the optimizer, so a repackaging that perturbs a
single byte silently changes measured fitness. Two surfaces are locked here; neither is
covered by ``tests/test_prompts_golden.py``.

**1. Resolved prompt text.** ``test_prompts_golden.py::test_every_prompt_file_has_a_golden_hash``
hashes the *raw bytes on disk*, which still contain literal ``{{include:_blocks/...}}``
markers. What a model actually sees is what :func:`evolution.prompts.load_prompt` returns
*after* include expansion. A change to the include mechanism (a dropped block, a moved splice
point, added stripping) therefore passes the raw-byte lock while rewriting the prompt.

*Which ``GOLDEN_LOADED`` entries carry new information.* Exactly seven prompt files
contain include markers, so exactly seven resolved hashes differ from their raw-byte
counterparts in ``tests/test_prompts_golden.py::GOLDEN_FILES``. These are the load-bearing
entries -- they are locked nowhere else in the suite:

* ``agentic_wrapper_hermes``   (leak_rules + generality_rules + refinement_rules)
* ``agentic_wrapper_session``  (leak_rules + generality_rules + refinement_rules)
* ``merge_edits``              (leak_rules)
* ``propose_edits``            (leak_rules + generality_rules + refinement_rules)
* ``propose_edits_train``      (leak_rules + generality_rules + refinement_rules)
* ``reflect_failure``          (leak_rules + generality_rules)
* ``reflect_success``          (generality_rules)

The remaining hashes are byte-identical to their ``GOLDEN_FILES`` counterparts. They are
kept deliberately, as cheap defence in depth: they cost one line each, they let
``test_prompt_file_inventory_matches_golden`` cover the whole prompt tree (so a *new*
include-bearing prompt cannot appear without landing here), and they pin the loader's view of
the prompts that are never exported as ``evolution.prompts`` constants (``propose_edits``,
``propose_edits_train``, ``merge_edits``, ``system_edit``, ``system_reflect``,
``reflect_failure``, ``reflect_success``, ``agentic_wrapper_session``,
``agentic_wrapper_hermes``). Do not delete this module as "redundant with ``GOLDEN_FILES``":
the seven prompts above would become unlocked.

Every measurement here runs against a cold ``evolution.prompts._prompt_cache`` (see
``_cold_prompt_cache``). ``load_prompt`` memoises by name in a module global, so a lock that
reads through a cache some earlier test warmed can compare against text that is no longer the
text on disk -- i.e. pass while the prompt it names is broken.

**2. The skill-injection wrapper.** A skill body reaches the solver through an f-string that is
duplicated verbatim in three backends. ``tests/test_skill_injection.py`` re-types that f-string
inside the test instead of calling the backends, so it would keep passing if production changed.
``GOLDEN_INJECTION`` locks the rendered result of the real ``complete_with_skills`` bodies, and
``test_skill_injection_surface_matches_golden`` walks ``evolution.llm`` *recursively* so a
fourth injecting backend cannot hide in a subpackage.

To refresh after an INTENTIONAL change, in the same commit as the change:

* one resolved prompt (a fresh interpreter starts with a cold cache)::

      python -c "import hashlib; from evolution import prompts; \\
          print(hashlib.sha256(prompts.load_prompt('propose_edits').encode()).hexdigest())"

* the injection samples: copy the rendered text out of the assertion failure, or::

      python -m pytest tests/characterisation/test_prompt_files_golden.py -q

  and paste the ``actual`` value the failure reports.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import pkgutil
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import evolution.llm as llm_pkg
from evolution import prompts
from evolution.core.models import Skill, SkillFrontmatter
from evolution.llm.agent_backend import AgentBackend
from evolution.llm.gigachat_backend import GigaChatBackend
from evolution.llm.litellm_backend import LiteLLMBackend

GOLDEN_LOADED = {
    "tree_reflector_v5": "c7d586cbdc0e786519674ba8cf82520e5ad70e5233cab341f91a7cb70a590c85",
    "tree_editor_v2": "836e783fb3ae2bed5e536ddee987adb7d2c4193fec9bb788ca17471d3cbcddd5",
    "_blocks/blind_evidence": "d9337f8b80f2a8390eee8a321556f13bbf7b4a3cc5b99848e80154ff357871e0",
    "_blocks/generality_rules": "b48cde3a10e6b4c961d5a65b52f646d48ed17f87adfc985ac194cebe21a95647",
    "_blocks/leak_rules": "51d790862b121ffa3f16c8fb4d63ed754ec6d4899cc15ad4d63435618b9ab637",
    "_blocks/oracle_evidence": "2aaa4de7841f075db3eb25cbe47a82528874788fd2802ac7fa657ce91d30064f",
    "_blocks/refinement_rules": "db5646e86efc34d47259e44db04751bf4b52f4d1a329fe1c76ba96b15a7b611c",
    "_blocks/runtime_path_contract_native": (
        "81b99cc75e000bb44a15cca543db0f06c838a082868af0deaf856178215e21ff"
    ),
    "_blocks/runtime_path_contract_neutral": (
        "d163155f6fe2eb11bd77160a1066285a190e0b5c5bbce28eb9594018984b4ce8"
    ),
    "agentic_wrapper_hermes": "1a65dbc7dd40be2896eda1d8fcbb334c4ad8216d399d30775752aaf3cf71ab73",
    "agentic_wrapper_session": "b2863c6a5bd80e293d447e8545ef5fcbdaf486131ad565823b915c877fff23a6",
    "legacy/codex_file_edit_prompt": (
        "a262a0d6b5573ae52495d6c6b02b48a9cc16b6046493cd8dc466232c33bbf165"
    ),
    "legacy/codex_file_edit_system": (
        "f199f141e26486a79f3b1ea4099c552b7336cd820c8089bfc2692c6c0e9f8af1"
    ),
    "legacy/compact_prompt": "b56f6430dcf42680cc49595075fc88effc5b9357eb0f4b93eaa377c5d9b9b8f0",
    "legacy/compact_system": "9fb4d063723ee2aa7d52589524cdb8141e5d3725b080b89861ea3dc0ded340b4",
    "legacy/reflect_prompt": "98930f1f4e01ac3254829d6cc2aaf7e8b949e5c7c701719f02eb30e37fa2b8b0",
    "legacy/reflect_success_prompt": (
        "c1e8d0d0dd6fb9f8abda5e13d0ae2d6ec30789d561def162eee1d1907f07643f"
    ),
    "legacy/reflect_system_blind": "619b133fa35ab20def42ec3932723351963375c7453dd3781aa002f27f797a68",
    "legacy/reflect_system_oracle": (
        "dad9e20f183ca69402b6a1d2a8ae99a97360f79c1a51901064e0deffa80a226b"
    ),
    "legacy/rewrite_prompt": "1fc43a533e8cc94490aa2b007cc1a6780a4ffff0dbde98d25cda4f654229f347",
    "legacy/rewrite_system": "375330564b449ac0ddfce91b423d06cdc9e3a92d850bd8cea44344480d26baa8",
    "legacy/session_prompt": "a99b7ee8907369dddac4b59a1901a04103455fec9732e5395ab37d8b08c8dd7d",
    "legacy/session_system_blind": "f982493e632cb469f6201f065fbc57a9fcdbe709c2615b3bc863118a24baae13",
    "legacy/session_system_oracle": (
        "2067f058e0e7bb69655a65a9fc5f79a8569624f8f588473f6e59929726947808"
    ),
    "merge_edits": "c1bddf017dae2312baa32f4ec6e7e6647993a950428b3480e695ac42f85dc654",
    "propose_edits": "592a54c7e410e01ca4e688faf102933e468cea51bc57a2234ef0ecb675411e28",
    "propose_edits_train": "419f10b9ae55b67ce195bc0eb8bf14dcc3331f22c1def3ad4854b1f66bc3ec5a",
    "reflect_failure": "e66525ccccf1dbf90c195ab4a827a24949e29b2d248af5bfe6c97f11a1c21099",
    "reflect_success": "99065a9e506e8b155c8ebab62efc7840a46c5cf1dc0e78f4737d6af52321dbc4",
    "system_edit": "f7ec520ab80bb5b5b05d2f97ce96e8ef8654f508e3fb3c22c4184ec60f1ab370",
    "system_reflect": "02cf7ea35c2c06dee7a7c6e171d3ae90bdae9c5730ca436c9b8c46b5b72f18ae",
    "tree_agent_editor": "d963c377fd7ea5738ac916e2a07e3ea35d64d93f82b398c8ca5e891735ca01f0",
    "tree_agent_grouper": "792344112c760507d0b0dd1fa5156c751ed9e82bc1ae9ca14520a30af9fcc1db",
    "tree_editor": "b8c6e0bb9fbac484a5c0a3499b7b3f01e7236bc9aaeb651e3e45b030a48787b4",
    "tree_editor_continuation": (
        "957993f53cca67557545ba70b0fb0bb4727bd28d921152772d47b218dff45ba2"
    ),
    "tree_agent_reflector": "535d68226ccce548f9b07cc50f998128b496902b03a0ccbc3d4a8149418eca01",
    "tree_reflector": "e036a5be22825d8d463fdf7eded5dbebbc540417d2d234c06309ffcc92aa1b2f",
    "tree_agent_reflector_v3": "71e3f08ca3ef58bc380bf37a7d230c352b9c7b3a1ec1ff3a1d62c66c464b5954",
    "tree_reflector_v3": "f61c4132d454adfe263f5d6fecbb32627f4e0fca4e81c43a8095069f2eb33937",
    "tree_reflector_v4": "7993bdf31771d40efa88682a54408ba37b7b38030bd02fea9faa60792a75900d",
    "tree_grouping_model": "4a97ce28fe6e6a025df1face409532cfc23d001fcc29050fa47d25b37d268e82",
    "tree_grouping_model_v2": "551213198461b1cb36f060e2530ae1d92dff9bdb02fec6d98f13bad33a853aad",
}

INCLUDE_BEARING_PROMPTS = frozenset(
    {
        "agentic_wrapper_hermes",
        "agentic_wrapper_session",
        "merge_edits",
        "propose_edits",
        "propose_edits_train",
        "reflect_failure",
        "reflect_success",
    }
)


@pytest.fixture(autouse=True)
def _cold_prompt_cache():
    """Guarantee every test in this module reads prompts off disk, and leaves no crumbs.

    ``evolution.prompts.load_prompt`` memoises into a module global that no test owns. Warmed
    by an earlier test in the same session, it can hand this lock text that is no longer on
    disk -- a false pass, which is the one failure mode a characterisation lock may not have.
    Autouse so it cannot be forgotten by a future test added below; clearing on the way out
    keeps this module from perturbing anyone else's view of the cache.
    """
    prompts.clear_cache()
    yield
    prompts.clear_cache()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load_prompt_cold(name: str) -> str:
    """``load_prompt`` with the memo dropped immediately before the read."""
    prompts.clear_cache()
    return prompts.load_prompt(name)


def _prompt_names_on_disk() -> set[str]:
    """Every shipped prompt file as the name ``load_prompt`` expects."""
    return {
        path.relative_to(prompts._PROMPTS_DIR).with_suffix("").as_posix()
        for path in prompts._PROMPTS_DIR.rglob("*.md")
        if path.name != "README.md"
    }


def test_prompt_file_inventory_matches_golden():
    """No prompt file may be added or deleted without touching GOLDEN_LOADED."""
    assert _prompt_names_on_disk() == set(GOLDEN_LOADED), (
        "src/evolution/prompts/**/*.md and GOLDEN_LOADED drifted apart"
    )


def test_loaded_prompt_text_matches_golden():
    drift = {}
    for name, expected in GOLDEN_LOADED.items():
        actual = _sha256(_load_prompt_cold(name))
        if actual != expected:
            drift[name] = actual
    assert not drift, (
        f"Resolved prompt text changed; update GOLDEN_LOADED in the same commit: {drift}"
    )


def test_include_bearing_prompts_are_the_documented_seven():
    """The module docstring names the 7 prompts only this file locks; keep that claim true.

    If a prompt gains or loses an ``{{include:}}`` marker, the set of hashes that carry
    information beyond ``GOLDEN_FILES`` changes, and the docstring's list of load-bearing
    entries -- the reason this module is not redundant -- goes stale.
    """
    with_includes = {
        name
        for name in GOLDEN_LOADED
        if prompts._INCLUDE_RE.search((prompts._PROMPTS_DIR / f"{name}.md").read_text("utf-8"))
    }
    assert with_includes == INCLUDE_BEARING_PROMPTS, (
        "the set of include-bearing prompts changed; update INCLUDE_BEARING_PROMPTS and the "
        "list in this module's docstring in the same commit"
    )


def test_raw_byte_lock_still_covers_the_same_inventory():
    """The raw-byte lock lives in ``tests/test_prompts_golden.py``; keep it wired up.

    Line-ending and encoding changes are invisible to ``load_prompt`` (``read_text`` performs
    universal-newline translation), so that test is not redundant with this one. It is
    deliberately *not* duplicated here -- a second copy of the same hashes would double the
    refresh cost of every intentional prompt edit. This guard fails loudly if a repackaging
    moves, renames, or narrows it.

    A prompt may be retired from the byte lock only by being named in that module's
    ``HISTORICAL_PROMPT_FILES``. That set is part of the covered inventory here, so
    retiring one is a visible edit in two files; dropping a hash without declaring
    the retirement still fails. The private prompt-authoring README has its own raw-byte pin in
    ``tests/test_private_docs_contract.py`` and is deliberately outside this public inventory.
    """
    module = importlib.import_module("tests.test_prompts_golden")
    golden_files = getattr(module, "GOLDEN_FILES", None)
    assert golden_files is not None, (
        "tests/test_prompts_golden.py::GOLDEN_FILES is gone; the raw prompt bytes are now "
        "unlocked -- restore it or move the raw-byte hashes into this module"
    )
    historical = getattr(module, "HISTORICAL_PROMPT_FILES", frozenset())
    covered = {
        Path(relpath).with_suffix("").as_posix()
        for relpath in (set(golden_files) | set(historical))
    }
    assert covered == set(GOLDEN_LOADED), (
        "GOLDEN_FILES + HISTORICAL_PROMPT_FILES and GOLDEN_LOADED cover different prompt files"
    )


FAKE_SKILL_A_NAME = "fake-skill-a"
FAKE_SKILL_A_BODY = "FAKE BODY LINE 1\nFAKE BODY LINE 2"
FAKE_SKILL_B_NAME = "fake-skill-b"
FAKE_SKILL_B_BODY = "FAKE BODY 2ND SKILL"
FAKE_SYSTEM = "FIXED SYSTEM PROMPT"

GOLDEN_INJECTION = {
    "one_skill_with_system": (
        "FIXED SYSTEM PROMPT\n"
        "\n"
        '<skill_content name="fake-skill-a">\n'
        "FAKE BODY LINE 1\n"
        "FAKE BODY LINE 2\n"
        "</skill_content>"
    ),
    "two_skills_no_system": (
        '<skill_content name="fake-skill-a">\n'
        "FAKE BODY LINE 1\n"
        "FAKE BODY LINE 2\n"
        "</skill_content>\n"
        "\n"
        '<skill_content name="fake-skill-b">\n'
        "FAKE BODY 2ND SKILL\n"
        "</skill_content>"
    ),
}

INJECTING_BACKENDS = (AgentBackend, GigaChatBackend, LiteLLMBackend)

NON_INJECTING_BACKENDS = {
    # Protocol declaration only -- the body is `...`.
    "evolution.llm.backend.LLMBackend",
    # Records the call and never renders a wrapper.
    "evolution.llm.mock_backend.MockBackend",
}


def _fake_skill(name: str, body: str) -> Skill:
    return Skill(
        frontmatter=SkillFrontmatter(name=name, description="fixed fake description"),
        body=body,
        path=Path("/fake/skills") / name,
        skill_md_path=Path("/fake/skills") / name / "SKILL.md",
    )


class _SystemPromptRecorder:
    """Stands in for ``self`` so the real ``complete_with_skills`` body runs offline.

    ``complete_with_skills`` renders the wrapper and hands it to ``self.complete``; recording
    that argument captures the production f-string without constructing a backend or touching
    the network.
    """

    def __init__(self) -> None:
        self.system: str | None = None

    async def complete(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        seed: int | None = None,
    ) -> None:
        self.system = system
        return None


def _render_injection(backend_cls: Any, skills: list[Skill], system: str | None) -> str:
    recorder = _SystemPromptRecorder()
    asyncio.run(
        backend_cls.complete_with_skills(
            recorder,
            [{"role": "user", "content": "fixed user message"}],
            skills,
            system,
        )
    )
    assert recorder.system is not None, f"{backend_cls.__name__} never called self.complete"
    return recorder.system


def _walk_llm_modules() -> tuple[list[ModuleType], list[str]]:
    """Import ``evolution.llm`` and every module beneath it, recursively.

    ``pkgutil.iter_modules`` stops at the top level, so a backend that the merge parks in a
    subpackage would drop out of the locked surface -- exactly the case the surface assertion
    exists to catch. An optional backend whose SDK is absent must not take the lock down with
    it, so an unimportable module is skipped and its name returned for the caller to report.
    """
    skipped: dict[str, str] = {}

    def _record_walk_error(name: str) -> None:
        skipped.setdefault(name, "subpackage failed to import")

    modules: list[ModuleType] = [llm_pkg]
    walked = pkgutil.walk_packages(
        llm_pkg.__path__, prefix=f"{llm_pkg.__name__}.", onerror=_record_walk_error
    )
    for module_info in walked:
        try:
            modules.append(importlib.import_module(module_info.name))
        except Exception as exc:
            skipped.setdefault(module_info.name, f"{type(exc).__name__}: {exc}")
    return modules, [f"{name} ({reason})" for name, reason in sorted(skipped.items())]


def _classes_defining_complete_with_skills() -> tuple[set[str], list[str]]:
    """Qualified names of every class reachable from ``evolution.llm`` that defines the wrapper.

    A class is keyed by ``obj.__module__`` -- where it is *defined* -- not by the module it was
    found in. Re-exports therefore collapse onto the definition they alias (importing a backend
    into a package ``__init__`` adds nothing), while a genuine definition is still reported when
    the only module that could be imported reaches it through such an alias.
    """
    modules, skipped = _walk_llm_modules()
    found = {
        f"{obj.__module__}.{obj.__qualname__}"
        for module in modules
        for obj in vars(module).values()
        if inspect.isclass(obj) and "complete_with_skills" in obj.__dict__
    }
    return found, skipped


def test_skill_injection_surface_matches_golden():
    """A backend gaining or losing its own skill-injection wrapper must be deliberate."""
    expected = {f"{cls.__module__}.{cls.__qualname__}" for cls in INJECTING_BACKENDS}
    expected |= NON_INJECTING_BACKENDS
    found, skipped = _classes_defining_complete_with_skills()
    if skipped:
        warnings.warn(
            f"evolution.llm modules not scanned for complete_with_skills: {skipped}",
            stacklevel=2,
        )
    assert found == expected, (
        "evolution.llm gained or lost a complete_with_skills implementation; add it to "
        f"INJECTING_BACKENDS (and lock its rendering) or to NON_INJECTING_BACKENDS. "
        f"unscanned modules: {skipped}"
    )


@pytest.mark.parametrize("backend_cls", INJECTING_BACKENDS, ids=lambda c: c.__name__)
def test_one_skill_with_system_injection_matches_golden(backend_cls):
    skills = [_fake_skill(FAKE_SKILL_A_NAME, FAKE_SKILL_A_BODY)]
    actual = _render_injection(backend_cls, skills, FAKE_SYSTEM)
    assert actual == GOLDEN_INJECTION["one_skill_with_system"], (
        f"{backend_cls.__name__} changed the skill-injection wrapper; update "
        f"GOLDEN_INJECTION in the same commit. actual={actual!r}"
    )


@pytest.mark.parametrize("backend_cls", INJECTING_BACKENDS, ids=lambda c: c.__name__)
def test_two_skills_without_system_injection_matches_golden(backend_cls):
    skills = [
        _fake_skill(FAKE_SKILL_A_NAME, FAKE_SKILL_A_BODY),
        _fake_skill(FAKE_SKILL_B_NAME, FAKE_SKILL_B_BODY),
    ]
    actual = _render_injection(backend_cls, skills, None)
    assert actual == GOLDEN_INJECTION["two_skills_no_system"], (
        f"{backend_cls.__name__} changed the multi-skill join or the no-system branch; "
        f"update GOLDEN_INJECTION in the same commit. actual={actual!r}"
    )
