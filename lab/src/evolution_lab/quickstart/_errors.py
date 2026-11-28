"""The one error type of the quickstart commands."""
from __future__ import annotations


class QuickstartError(Exception):
    """A quickstart command stopped before doing harm.

    ``exit_code`` is 2 for a problem in the user's project or arguments and 3
    for a Lab or environment failure. ``file`` and ``field`` name the place in
    the project when there is one. For a missing secret ``field`` is the
    secret's name and ``role`` is ``target`` or ``scorer``. ``state`` and
    ``run_dir`` are set when the error came after ``lab evaluate`` chose them,
    for example ``unexpected_error`` (exit code 3).
    """

    def __init__(self, code: str, message: str = "", *, file: str | None = None,
                 field: str | None = None, exit_code: int = 2, role: str | None = None,
                 state: str | None = None, run_dir: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.file = file
        self.field = field
        self.exit_code = exit_code
        self.role = role
        self.state = state
        self.run_dir = run_dir
