#!/usr/bin/env python3
"""Loopback CONNECT proxy for the separately supervised collector browser.

The browser's cgroup may only reach this proxy or loopback.  Every non-local
HTTPS connection therefore has to pass this allowlist before a TCP connection
is opened, even if a CDP session detaches.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any


LOG = logging.getLogger("uc_browser_egress_proxy")
MAX_HEADER_BYTES = 16 * 1024


def allowed_host(host: str, allowed_hosts: list[str]) -> bool:
    """Return whether a normalized hostname matches the explicit policy."""
    name = host.strip().lower().rstrip(".")
    if not name or any(char.isspace() for char in name):
        return False
    for rule in allowed_hosts:
        normalized = str(rule).strip().lower().rstrip(".")
        if normalized.startswith("*."):
            suffix = normalized[2:]
            if suffix and name.endswith("." + suffix):
                return True
        elif name == normalized:
            return True
    return False


def valid_host_rule(rule: str) -> bool:
    """Reject empty, whitespace, and malformed hostname policy entries."""
    name = rule.strip().lower().rstrip(".")
    if name.startswith("*."):
        name = name[2:]
    return bool(name) and "." in name and all(
        label and all(char.isalnum() or char == "-" for char in label)
        for label in name.split(".")
    )


def load_policy(path: Path) -> tuple[list[str], int]:
    """Load the intentionally small, hostname-only transport policy."""
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("policy must be an object")
    hosts = document.get("allowed_hosts")
    limit = document.get("max_connections", 16)
    if not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) for host in hosts):
        raise ValueError("policy.allowed_hosts must be a non-empty string list")
    if not isinstance(limit, int) or not 1 <= limit <= 64:
        raise ValueError("policy.max_connections must be an integer from 1 to 64")
    if any(not valid_host_rule(host) for host in hosts):
        raise ValueError("policy contains an invalid hostname")
    return hosts, limit


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


class ConnectProxy:
    def __init__(self, allowed_hosts: list[str], max_connections: int) -> None:
        self.allowed_hosts = allowed_hosts
        self._slots = asyncio.Semaphore(max_connections)

    async def handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            try:
                header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=10)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._reply(client_writer, 400, "invalid proxy request")
                return
            if len(header) > MAX_HEADER_BYTES:
                await self._reply(client_writer, 431, "request headers too large")
                return
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", "replace").split()
            if len(request_line) != 3 or request_line[0] != "CONNECT":
                await self._reply(client_writer, 405, "CONNECT required")
                return
            host, separator, port_text = request_line[1].rpartition(":")
            if not separator or not host or not port_text.isdecimal() or int(port_text) != 443:
                await self._reply(client_writer, 403, "HTTPS CONNECT to port 443 required")
                return
            if not allowed_host(host, self.allowed_hosts):
                LOG.warning("blocked host=%s", host.lower().rstrip("."))
                await self._reply(client_writer, 403, "host not admitted")
                return
            if self._slots.locked():
                await self._reply(client_writer, 429, "connection budget exhausted")
                return
            async with self._slots:
                try:
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(host, 443), timeout=15,
                    )
                except OSError:
                    LOG.warning("unavailable host=%s", host.lower().rstrip("."))
                    await self._reply(client_writer, 502, "upstream unavailable")
                    return
                LOG.info("admitted host=%s", host.lower().rstrip("."))
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await asyncio.gather(
                    _relay(client_reader, upstream_writer),
                    _relay(upstream_reader, client_writer),
                )
        finally:
            client_writer.close()
            await client_writer.wait_closed()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        writer.write(f"HTTP/1.1 {status} {message}\r\nConnection: close\r\n\r\n".encode("ascii"))
        await writer.drain()


async def serve(policy: Path, host: str, port: int) -> None:
    allowed_hosts, max_connections = load_policy(policy)
    proxy = ConnectProxy(allowed_hosts, max_connections)
    server = await asyncio.start_server(proxy.handle, host=host, port=port, limit=MAX_HEADER_BYTES)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOG.info("listening on %s with %d admitted host rules", sockets, len(allowed_hosts))
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9225)
    parser.add_argument("--check-policy", action="store_true")
    args = parser.parse_args()
    try:
        allowed_hosts, limit = load_policy(args.policy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid policy: {exc}")
    if args.check_policy:
        print(f"policy OK: {len(allowed_hosts)} host rules, max_connections={limit}")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(args.policy, args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
