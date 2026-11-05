"""Rough 95% bootstrap interval for small-n pass-rate samples.

Per memory ``feedback_bootstrap_rough.md``: with n<30 the result must be
labelled "rough 95% bootstrap interval", never "CI". When n<3 we return
None; callers render that as ``N/A``.

Standardised signature for the experiment family. The promoted version
matches the cleanest existing implementation
(``5p2-table3-pilot/merge_report.py``), with named ``confidence`` for
flexibility.
"""

from __future__ import annotations

import random
from collections.abc import Iterable


def bootstrap_ci(
    values: Iterable[float],
    *,
    n_resamples: int = 1000,
    seed: int = 42,
    confidence: float = 0.95,
) -> tuple[float, float] | None:
    """Resample with replacement; return (lo, hi) of the empirical mean
    distribution at the requested confidence. Returns ``None`` when
    ``len(values) < 3`` (caller renders as ``N/A``).
    """
    xs = [float(v) for v in values]
    if len(xs) < 3:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means: list[float] = []
    for _ in range(n_resamples):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_idx = max(0, int(alpha * len(means)))
    hi_idx = min(len(means) - 1, int((1.0 - alpha) * len(means)))
    return (means[lo_idx], means[hi_idx])


__all__ = ["bootstrap_ci"]
