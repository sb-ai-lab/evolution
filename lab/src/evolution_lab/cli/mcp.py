"""``lab mcp``: serve the Lab MCP server on stdio."""
from __future__ import annotations

from typing import TextIO

from .errors import UsageError
from evolution_lab.projects.endpoint import resolve_socket

_HINT = "lab mcp needs the MCP extra: pip install 'evolution-lab[mcp]'\n"


def run_mcp(args, *, stderr: TextIO) -> int:
    """Resolve the socket as every adapter does, then hand over to the stdio server.

    Only the resolution's ``ValueError`` (a too-long path) and a relative socket (a relative ``LAB_HOME`` or
    ``XDG_DATA_HOME``) are usage errors; the running server's are not. A relative socket would depend on the
    working directory (PDR-0061), so it is refused with the CLI client's wording.
    """
    try:
        socket = resolve_socket(args.socket, args.home)
    except ValueError as error:
        raise UsageError(str(error)) from None
    if not socket.is_absolute():
        raise UsageError("socket path must be absolute")
    socket_path = str(socket)
    timeout = args.timeout or 60.0
    try:
        from evolution_lab.mcp.server import serve_stdio
    except ImportError as error:
        if error.name is not None and (error.name == "mcp" or error.name.startswith("mcp.")):
            stderr.write(_HINT)
            return 2
        raise
    return serve_stdio(socket_path, timeout=timeout, autonomy=args.autonomy,
                       approval_timeout=args.approval_timeout)
