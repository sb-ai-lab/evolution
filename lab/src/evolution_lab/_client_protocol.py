"""Owner-side primitives for the private in-process H1 supervisor protocol."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

from evolution_lab import _supervisor


ChannelClosed = _supervisor.ChannelClosed
ProtocolError = _supervisor.ProtocolError


def supervisor_invocation(
    supervisor_path: Path,
    command_read_fd: int,
    event_write_fd: int,
    operation_id: str,
    core_argv: Sequence[str],
    *, ownership: str,
) -> tuple[str, ...]:
    """Build the secret-free H1 argv; the caller supplies ``pass_fds``."""
    if ownership not in {"subreaper", "group_tree_marker"}:
        raise ValueError("unknown ownership mode")
    path = Path(supervisor_path).absolute()
    core = tuple(core_argv)
    if command_read_fd <= 2 or event_write_fd <= 2 or command_read_fd == event_write_fd:
        raise ValueError("H1 pipe descriptors must be distinct private descriptors")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    if not core or any(not isinstance(item, str) for item in core):
        raise ValueError("core_argv must be a non-empty string sequence")
    return (
        sys.executable,
        "-I",
        str(path),
        "--ownership",
        ownership,
        _supervisor.H1_MODE,
        str(command_read_fd),
        str(event_write_fd),
        operation_id,
        "--",
        *core,
    )


async def _write_all(fd: int, data: bytes) -> None:
    loop = asyncio.get_running_loop()
    original_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    offset = 0
    try:
        while offset < len(data):
            try:
                written = os.write(fd, data[offset:])
            except BlockingIOError:
                writable = loop.create_future()

                def wake() -> None:
                    if not writable.done():
                        writable.set_result(None)

                loop.add_writer(fd, wake)
                try:
                    await writable
                finally:
                    loop.remove_writer(fd)
                continue
            except OSError as exc:
                raise ChannelClosed("configuration channel write failed") from exc
            if written == 0:
                raise ChannelClosed("configuration channel closed during write")
            offset += written
    finally:
        try:
            os.set_blocking(fd, original_blocking)
        except OSError:
            pass


class CommandChannel:
    """Own the owner's command-write FD until :meth:`close` is called."""

    def __init__(self, fd: int) -> None:
        if fd <= 2:
            raise ValueError("command FD must be private")
        self._fd: int | None = fd
        self._config_started = False

    def _open_fd(self) -> int:
        if self._fd is None:
            raise ChannelClosed("command channel is closed")
        return self._fd

    async def send_config(
        self,
        operation_id: str,
        environment: Mapping[str, str],
        *,
        grace_seconds: float,
    ) -> None:
        """Asynchronously send the potentially-large first configuration frame.

        Cancellation stops further writes. The caller must then close this channel
        and reap the supervisor; a partially written frame never authorizes spawn.
        """
        if self._config_started:
            raise ProtocolError("configuration write already started")
        self._config_started = True
        message = _supervisor.make_config(
            operation_id, dict(environment), grace_seconds
        )
        frame = _supervisor.encode_frame(message)
        await _write_all(self._open_fd(), frame)

    def _send(self, operation_id: str, kind: str) -> None:
        if not self._config_started:
            raise ProtocolError("configuration must be sent before commands")
        _supervisor.write_atomic_frame(
            self._open_fd(), _supervisor.make_command(operation_id, kind)
        )

    def send_go(self, operation_id: str) -> None:
        """Issue one atomic GO write; caller latches attempted-GO before calling."""
        self._send(operation_id, "GO")

    def send_cancel(self, operation_id: str) -> None:
        self._send(operation_id, "CANCEL")

    def send_stop(self, operation_id: str) -> None:
        self._send(operation_id, "STOP")

    def send_force(self, operation_id: str) -> None:
        self._send(operation_id, "FORCE")

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class EventChannel:
    """Own one nonblocking event-read FD and validate every complete event."""

    def __init__(self, reader, transport, operation_id: str) -> None:
        self._reader = reader
        self._transport = transport
        self._operation_id = operation_id

    async def read(self) -> dict | None:
        try:
            header = await self._reader.readexactly(_supervisor._HEADER.size)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None
            raise ProtocolError("incomplete event frame header") from exc
        length = _supervisor._HEADER.unpack(header)[0]
        if length == 0:
            raise ProtocolError("empty event frame")
        try:
            payload = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError("incomplete event frame payload") from exc
        message = _supervisor.validate_event(_supervisor.decode_payload(payload))
        if message["operation_id"] != self._operation_id:
            raise ProtocolError("event operation_id mismatch")
        return message

    def close(self) -> None:
        self._transport.close()


async def open_event_channel(fd: int, operation_id: str) -> EventChannel:
    """Adopt ``fd`` into the event loop; ownership transfers on entry."""
    if fd <= 2:
        raise ValueError("event FD must be private")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    stream = os.fdopen(fd, "rb", buffering=0)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, stream
        )
    except BaseException:
        stream.close()
        raise
    return EventChannel(reader, transport, operation_id)


__all__ = [
    "ChannelClosed",
    "CommandChannel",
    "EventChannel",
    "ProtocolError",
    "open_event_channel",
    "supervisor_invocation",
]
