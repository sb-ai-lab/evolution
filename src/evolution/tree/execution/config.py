"""Command, environment and shared measurement configuration values and refusals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evolution.tree.execution.interaction import RolloutBudget

DEFAULT_INPUT_TEXT_KEY = "text"


class ManifestError(ValueError):
    """A closed-code manifest load failure. `str()` is `f"{code}: {detail}"`;
    `detail` names the key path that failed and never carries file contents.

    `detail` is always a key path -- a dotted one, `_ROOT` for the document
    itself, or `_DOCUMENT` for the document's own file -- and never a
    filesystem path or an OS error message. Callers print it: `evo tree
    conformance` echoes it to stderr and `EvolveRefused` carries it as its
    public `path`, so a resolved path here would be a path in a refusal.

    `expected` is the `/14` or `/15` refusal's fixed domain statement
    (`PolicyError.expected`, or a `/15` declaration's sentence), or `None`; like `detail` it never carries a value
    from the document, and it is not part of `str()`."""

    def __init__(self, code: str, detail: str, *, expected: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.expected = expected


@dataclass(frozen=True)
class EnvironmentSection:
    kind: Literal["static", "scripted"]
    input_text_key: str = DEFAULT_INPUT_TEXT_KEY


@dataclass(frozen=True)
class CommandSection:
    argv: tuple[str, ...]
    cwd: Path
    dependency_paths: tuple[Path, ...]
    allowed_env: frozenset[str]
    timeout_seconds: float | None  # scorer only; `None` for the editor


@dataclass(frozen=True)
class EvaluationPolicy:
    repeats: int
    random_seed: int
    max_concurrency: int
    max_scorer_concurrency: int
    metric_primary: str
    budget: RolloutBudget
    sample_retries: int

    @property
    def looks(self) -> tuple[int, ...]:
        return (self.repeats,)
