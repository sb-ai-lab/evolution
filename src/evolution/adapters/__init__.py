"""Framework adapters for the procedural-memory adaptive-agent benchmark.

Each adapter is a thin "drive native + harvest" wrapper over a third-party
framework. The 4way-sbbench-pilot then evaluates each framework through
its **own native test runtime** — Evolution via a single-shot uniform
solver, Hermes via in-process ``AIAgent.run_conversation``, Memento via
the ``memento agent`` subprocess, and MemP via its step-env episode loop
with ``Memory.retrieve``. The comparison is therefore framed as
end-to-end adaptive-agent performance, not a normalized skill-artifact
comparison: differences in retrieval, prompting, memory-update policy,
tool loop, and runtime robustness are *part of* the measured system.

Adapters in this package:

* :mod:`evolution.adapters.evolution_adapter` — preloaded curated
  skills plus an ``evo reflect → rewrite → commit`` evolve round.
* :mod:`evolution.adapters.hermes_adapter` — drives in-process
  ``AIAgent`` per train task; harvests skills tagged by the
  ``skill_manage`` event log; optional synchronous
  ``run_curator_review`` consolidation pass.
* :mod:`evolution.adapters.memento_adapter` — drives ``memento agent
  --message`` per train task with the agent's full Intent → Plan →
  Execute → Reflect → Finalize loop; the DreamLoop daemon is NOT
  exercised (no public one-shot trigger).
* :mod:`evolution.adapters.memp_adapter` — drives MemP's
  ``run_episode`` per train task, then ``Memory.process_trajectory_item``
  + ``process_trajectory_item_reflect`` to build the persisted
  procedural memory.

The shared ``BaselineMode`` helpers remain only as explicit composition
utilities; publication runners should
record any curated/generated blending mode in their manifests. Frontmatter
compat is kept as a *minimal* layer (``_minimal_frontmatter_compat``) that
only enforces SkillStore-required fields and preserves the original SKILL.md
alongside as ``SKILL.md.original`` whenever it modifies anything.
"""

from __future__ import annotations
