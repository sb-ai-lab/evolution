"""Shared CLI usage-error type.

Kept out of ``parser.py`` so ``project.py`` can import it without a circular import
(``parser.py`` imports from ``project.py``, and ``project.py`` needs ``UsageError``).
"""
from __future__ import annotations


class UsageError(ValueError):
    pass
