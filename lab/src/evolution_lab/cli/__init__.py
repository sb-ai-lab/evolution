"""Local service command line interface."""

__all__ = ["main"]


def main(argv=None, *, stdout=None, stderr=None, stdin=None, client_factory=None) -> int:
    """Run one explicit command (lazy: the parser loads on the first call, not on package import)."""
    from .parser import main as run

    return run(argv, stdout=stdout, stderr=stderr, stdin=stdin, client_factory=client_factory)
