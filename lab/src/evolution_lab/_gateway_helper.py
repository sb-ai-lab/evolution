"""Installed gateway child. All credentials arrive through the private CONFIG pipe."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import stat
import struct

from .owner.protocol import MAX_CONFIG_BYTES, encode_frame, read_frame


async def control_frame(reader):
    size = struct.unpack('!I', await reader.readexactly(4))[0]
    if not 0 < size <= MAX_CONFIG_BYTES:
        raise ValueError('invalid control frame')
    return json.loads(await reader.readexactly(size))


async def serve(config):
    path = Path(config['control_path'])
    if not path.is_absolute():
        raise ValueError('control socket path must be absolute')
    parent = path.parent.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.getuid()):
        raise ValueError('control socket requires a private owned directory')
    import uvicorn
    from .budget import create_app, gateway_from_policy
    from .execution_policy import parse_policy

    durability = config['durability']
    if durability not in ('relaxed', 'strict'):
        raise ValueError('invalid durability')
    policy = parse_policy(config['policy'])
    gateway = gateway_from_policy(policy, upstream_keys=config['upstream_keys'], tokens=config['tokens'], durability=durability)
    server = uvicorn.Server(uvicorn.Config(create_app(gateway), host=policy.gateway.host,
        port=policy.gateway.port, log_config=None, log_level='critical', access_log=False))

    async def control(reader, writer):
        try:
            async with asyncio.timeout(2):
                request = await control_frame(reader)
                if (not isinstance(request, dict) or set(request) != {'credential', 'uses'}
                        or not isinstance(request['credential'], str)
                        or not hmac.compare_digest(request['credential'], config['control_credential'])):
                    return
                uses = request['uses']
                if uses is not None:
                    if not isinstance(uses, list) or any(not isinstance(use, str) for use in uses):
                        return
                    gateway.set_uses(frozenset(uses))
                writer.write(encode_frame({'ready': server.started and not server.should_exit}, max_bytes=4096))
                await writer.drain()
        except (ValueError, TypeError, asyncio.IncompleteReadError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    previous_umask = os.umask(0o077)
    try:
        control_server = await asyncio.start_unix_server(control, path=str(path), limit=MAX_CONFIG_BYTES)
    finally:
        os.umask(previous_umask)
    try:
        await server.serve()
    finally:
        control_server.close()
        await control_server.wait_closed()
        gateway.ledger.close_anchor()


def main():
    try:
        descriptor = int(os.environ.pop('LAB_GATEWAY_CONFIG_FD'))
        os.set_inheritable(descriptor, False)
        try:
            config = read_frame(descriptor)
        finally:
            os.close(descriptor)
        asyncio.run(serve(config))
        return 0
    except BaseException:
        # Arbitrary dependency/config exceptions must never print credential-bearing objects.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
