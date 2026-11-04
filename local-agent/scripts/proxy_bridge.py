#!/usr/bin/env python3
"""TCP relay: docker0 (172.17.0.1:7898) -> host proxy (127.0.0.1:7897).

The Mihomo proxy listens on loopback only and its TUN does not capture traffic from the Docker
bridge, so the Hermes sandbox has no internet. This relay gives the sandbox egress *only* through
the proxy. It binds the bridge IP alone (not 0.0.0.0) and accepts only clients from 172.17.0.0/16.
"""
import asyncio
import ipaddress
import sys

LISTEN = ("172.17.0.1", 7898)
UPSTREAM = ("127.0.0.1", 7897)
ALLOWED = ipaddress.ip_network("172.17.0.0/16")


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    peer = cw.get_extra_info("peername")[0]
    if ipaddress.ip_address(peer) not in ALLOWED:
        cw.close()
        return
    try:
        ur, uw = await asyncio.open_connection(*UPSTREAM)
    except OSError:
        cw.close()
        return
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))


async def main() -> None:
    server = await asyncio.start_server(handle, *LISTEN, reuse_address=True)
    print(f"relay {LISTEN[0]}:{LISTEN[1]} -> {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
