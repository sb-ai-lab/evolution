"""Deterministic source-trace selector for §5.2 Table 3 conditions.

Produces a list of ``(task_id, source_model_id, attempt_seed)`` of length
EXACTLY ``n_traces`` for the given ``(task_diversity, source_pool)``. The
list is the produce-time *spec* — Evolution's adapter reads it back and
runs one solve per entry.

Task-diversity conditions (paper §5.2):

* ``T1`` — single (task, model) pair. The list holds ``n_traces`` rows
  with the same task_id; the model_id rotates over ``source_pool`` only
  if it has length 1 (``Single``). For ``Cross`` the pool is split round-
  robin so the same task gets attempts on each model.
* ``T2`` — multi-task within one role. Tasks come from the role's train
  stream; attempts are spread evenly.
* ``T3`` — multi-task across the anchor pair (two roles, same skill).

Determinism rule: same ``(task_diversity, role_or_pair, source_pool,
n_traces, seed)`` ⇒ same list. The selector uses
``random.Random(seed)`` and sorts the input lists before sampling.

Premortem-derived invariant (load-bearing): the list length MUST equal
``n_traces`` exactly. Callers verify ``len(spec) == n_traces`` and refuse
to run otherwise — varying N silently confounds H1.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass

# Held-out invariant: the GPT-4o family is reserved for Gen_M eval and must
# never appear in a source pool.
_HELDOUT_FAMILY_TOKENS: tuple[str, ...] = ("gpt-4o", "gpt4o")


@dataclass(frozen=True)
class TraceSpec:
    """One produce-time trace assignment."""

    task_id: str
    source_model_id: str
    attempt_seed: int

    def to_jsonable(self) -> dict:
        return {
            "task_id": self.task_id,
            "source_model_id": self.source_model_id,
            "attempt_seed": self.attempt_seed,
        }


def _enforce_heldout(source_pool: list[str]) -> None:
    """Raise if the held-out GPT-4o family slipped into the pool."""
    for m in source_pool:
        ml = m.lower()
        for tok in _HELDOUT_FAMILY_TOKENS:
            if tok in ml:
                raise ValueError(
                    f"held-out invariant: {m!r} is reserved for Gen_M eval and "
                    "must not appear in any source_pool"
                )


def _round_robin(items: list[str], length: int) -> list[str]:
    if not items:
        raise ValueError("round_robin called with empty pool")
    return [items[i % len(items)] for i in range(length)]


def _model_distribution(pool: list[str], n_traces: int) -> list[str]:
    """Even (round-robin) distribution over the source pool.

    For ``len(pool)==3, n_traces=24`` returns a list with 8 per model.
    Order rotates through the pool so the same model isn't clustered.
    """
    if not pool:
        raise ValueError("source pool empty")
    base = n_traces // len(pool)
    extra = n_traces - base * len(pool)
    counts = [base + (1 if i < extra else 0) for i in range(len(pool))]
    out: list[str] = []
    while sum(counts) > 0:
        for i, _ in enumerate(pool):
            if counts[i] > 0:
                out.append(pool[i])
                counts[i] -= 1
                if len(out) == n_traces:
                    return out
    return out  # unreachable but keeps the type-checker happy


def _t1_spec(task_id: str, source_pool: list[str], n_traces: int, seed: int) -> list[TraceSpec]:
    """T1: same task, varying attempt seeds, model pool round-robin."""
    rng = random.Random(seed)
    model_seq = _model_distribution(sorted(source_pool), n_traces)
    rng.shuffle(model_seq)
    out = [
        TraceSpec(
            task_id=task_id,
            source_model_id=m,
            attempt_seed=seed + i,
        )
        for i, m in enumerate(model_seq)
    ]
    return out


def _t2_spec(
    role_train_tasks: list[str],
    source_pool: list[str],
    n_traces: int,
    seed: int,
) -> list[TraceSpec]:
    """T2: multiple tasks within one role; trace count fixed at N.

    With 2 train_a + train_b tasks per cell on the §5.2 MVP corpus the
    role-train set is small (~2 tasks). We round-robin the tasks and the
    models independently; attempt seeds are unique within (task, model)
    so a single (task, model) pair can be hit multiple times deterministi-
    cally.
    """
    if not role_train_tasks:
        raise ValueError("T2: empty role_train_tasks")
    rng = random.Random(seed)
    tasks = sorted(role_train_tasks)
    pool = sorted(source_pool)
    task_seq = _round_robin(tasks, n_traces)
    rng.shuffle(task_seq)
    model_seq = _model_distribution(pool, n_traces)
    rng.shuffle(model_seq)
    seen_pair: dict[tuple[str, str], int] = {}
    out: list[TraceSpec] = []
    for i, (t, m) in enumerate(zip(task_seq, model_seq, strict=True)):
        key = (t, m)
        seen_pair[key] = seen_pair.get(key, -1) + 1
        attempt_seed = seed + 1000 * seen_pair[key] + i
        out.append(TraceSpec(task_id=t, source_model_id=m, attempt_seed=attempt_seed))
    return out


def _t3_spec(
    anchor_pair_train_tasks: list[str],
    source_pool: list[str],
    n_traces: int,
    seed: int,
) -> list[TraceSpec]:
    """T3: tasks across the anchor pair (same skill, two roles).

    Identical structure to T2 except the task pool spans roles.
    """
    if not anchor_pair_train_tasks:
        raise ValueError("T3: empty anchor_pair_train_tasks")
    return _t2_spec(
        sorted(anchor_pair_train_tasks),
        source_pool=source_pool,
        n_traces=n_traces,
        seed=seed,
    )


def select_training_subset(
    *,
    task_diversity: str,
    source_pool: list[str],
    n_traces: int,
    seed: int = 0,
    t1_task_id: str | None = None,
    role_train_tasks: list[str] | None = None,
    anchor_pair_train_tasks: list[str] | None = None,
) -> list[TraceSpec]:
    """Resolve a §5.2 task_diversity × source_pool spec to a length-N list.

    Args:
      task_diversity: ``"T1" | "T2" | "T3"``. Caller is responsible for
        only passing T3 to anchor-skill cells.
      source_pool: list of model IDs. ``Single`` ⇒ length-1; ``Cross`` ⇒
        length-3.
      n_traces: paper-mandated trace budget. Must equal len(returned list).
      seed: RNG seed for deterministic sampling.
      t1_task_id: required for T1 — the single task to repeat.
      role_train_tasks: required for T2 — train task IDs from the cell's
        role.
      anchor_pair_train_tasks: required for T3 — train task IDs from both
        roles in the anchor pair.

    Raises ``ValueError`` if held-out invariant violated, or if the
    required arg for the chosen task_diversity is missing.
    """
    _enforce_heldout(list(source_pool))
    if n_traces <= 0:
        raise ValueError(f"n_traces must be positive, got {n_traces}")
    td = task_diversity.upper()
    if td == "T1":
        if not t1_task_id:
            raise ValueError("T1 requires t1_task_id")
        spec = _t1_spec(t1_task_id, list(source_pool), n_traces, seed)
    elif td == "T2":
        if role_train_tasks is None:
            raise ValueError("T2 requires role_train_tasks")
        spec = _t2_spec(list(role_train_tasks), list(source_pool), n_traces, seed)
    elif td == "T3":
        if anchor_pair_train_tasks is None:
            raise ValueError("T3 requires anchor_pair_train_tasks")
        spec = _t3_spec(list(anchor_pair_train_tasks), list(source_pool), n_traces, seed)
    else:
        raise ValueError(f"unknown task_diversity {task_diversity!r}; expected T1/T2/T3")
    if len(spec) != n_traces:
        raise AssertionError(
            f"selector produced {len(spec)} entries; expected exactly {n_traces} "
            f"(task_diversity={task_diversity}, pool_size={len(source_pool)})"
        )
    return spec


def specs_to_jsonable(specs: Iterable[TraceSpec]) -> list[dict]:
    return [s.to_jsonable() for s in specs]


__all__ = [
    "TraceSpec",
    "select_training_subset",
    "specs_to_jsonable",
]
