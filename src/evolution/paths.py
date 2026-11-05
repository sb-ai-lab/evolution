"""Account-home resolution that ignores the ``$HOME`` override.

Some deployments redirect ``$HOME`` to an agent configuration sandbox while
the real toolchain and native-agent authentication live under the OS account
home.
``Path.home()`` / ``expanduser`` honour ``$HOME`` and therefore resolve to the
sandbox; code that means "the real account home" must use :func:`account_home`,
which reads the passwd database and is independent of ``$HOME``.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path


def account_home() -> Path:
    """Return the real OS account home from passwd, independent of ``$HOME``."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)
