"""Public-only egress policy for the SpiderFoot network namespace.

The guard runs in a tiny sidecar that owns the collector's network namespace.
SpiderFoot, Maigret, GHunt, and every plugin share that namespace but receive no
network-administration capability. The nftables output chain is therefore the
transport boundary even when a module ignores the Python target validator.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

NFT_TABLE = "recon_egress"
NFT_MARKER = "uc-recon-egress-v1"
MAX_RECON_EGRESS_BYTES = 1_073_741_824
DEFAULT_DATABASE_REFRESH_SECONDS = 30
DEFAULT_GUARD_SOCKET = Path("/var/lib/unifiedcollector/recon-guard/status.sock")
_Resolver = Callable[..., list[tuple]]


class UnsafeReconTarget(ValueError):
    """A recon target is malformed or resolves outside public unicast space."""


# Special-purpose and non-global ranges are denied before the public-unicast
# accept rules. These include the IANA special-purpose ranges plus shared,
# multicast, translation, documentation, and transition ranges that can route
# into non-public destinations. The database exception is installed separately
# as one exact IPv4/IPv6 address and TCP port.
_NON_PUBLIC_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        # Azure's platform virtual IP is globally allocated but also exposes
        # host metadata/control-plane services to guest workloads.
        "168.63.129.16/32",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
    )
)


def is_public_address(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return true only for public, globally routed unicast addresses."""
    try:
        address = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    return not any(
        address.version == network.version and address in network
        for network in _NON_PUBLIC_NETWORKS
    )


def resolve_public_addresses(host: str, *, resolver: _Resolver | None = None) -> tuple[str, ...]:
    """Resolve all address families and reject the whole set if any answer is unsafe."""
    normalized = (host or "").strip().rstrip(".")
    if not normalized or "%" in normalized:
        raise UnsafeReconTarget("invalid hostname")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_address(literal):
            raise UnsafeReconTarget(f"non-public destination: {literal}")
        return (str(literal),)

    if any(char.isspace() for char in normalized) or "/" in normalized or "@" in normalized:
        raise UnsafeReconTarget("invalid hostname")
    lookup = resolver or socket.getaddrinfo
    try:
        records = lookup(normalized, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeReconTarget(f"hostname resolution failed: {normalized}") from exc
    addresses: set[str] = set()
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except (IndexError, ValueError, TypeError) as exc:
            raise UnsafeReconTarget("invalid DNS answer") from exc
        if not is_public_address(address):
            raise UnsafeReconTarget(f"non-public DNS answer for {normalized}: {address}")
        addresses.add(str(address))
    if not addresses:
        raise UnsafeReconTarget(f"hostname has no address: {normalized}")
    return tuple(sorted(addresses))


def _validate_hostname(host: str, resolver: _Resolver | None) -> None:
    if not host or len(host) > 253:
        raise UnsafeReconTarget("invalid hostname")
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeReconTarget("invalid international hostname") from exc
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in ascii_host.split(".")):
        raise UnsafeReconTarget("invalid hostname")
    if not all(all(ch.isalnum() or ch == "-" for ch in label) for label in ascii_host.split(".")):
        raise UnsafeReconTarget("invalid hostname")
    resolve_public_addresses(ascii_host, resolver=resolver)


def validate_recon_target(
    target_type: str,
    target_value: str,
    *,
    resolver: _Resolver | None = None,
) -> bool:
    """Validate target syntax and public address resolution without scope gates."""
    kind = (target_type or "").strip().lower()
    value = (target_value or "").strip()
    if not value:
        raise UnsafeReconTarget("empty recon target")
    if kind in {"ip", "ipv4"}:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise UnsafeReconTarget("invalid IP target") from exc
        if not is_public_address(address):
            raise UnsafeReconTarget(f"non-public destination: {address}")
        return True
    if kind == "url":
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise UnsafeReconTarget("URL must use http or https and include a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeReconTarget("URL userinfo is not allowed")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeReconTarget("invalid URL port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise UnsafeReconTarget("invalid URL port")
        _validate_hostname(parsed.hostname, resolver)
        return True
    if kind == "domain":
        if ":" in value or "/" in value or "@" in value:
            raise UnsafeReconTarget("domain target must be a hostname")
        _validate_hostname(value, resolver)
        return True
    if kind == "email":
        local, separator, host = value.rpartition("@")
        if not separator or not local or not host or "@" in local or any(ch.isspace() for ch in value):
            raise UnsafeReconTarget("invalid email target")
        _validate_hostname(host, resolver)
        return True
    if kind in {"username", "user", "channel"}:
        if len(value) > 256 or any(ch.isspace() for ch in value) or "/" in value or "\\" in value:
            raise UnsafeReconTarget("invalid username target")
        return True
    if kind == "phone":
        digits = value[1:] if value.startswith("+") else value
        if not digits.isascii() or not digits.isdigit() or not 3 <= len(digits) <= 15 or digits.startswith("0"):
            raise UnsafeReconTarget("invalid phone target")
        return True
    raise UnsafeReconTarget(f"unsupported target type: {kind}")


def render_nft_policy(
    database_addresses: Iterable[str], *, max_egress_bytes: int = 1_073_741_824
) -> str:
    """Render a fail-closed nftables policy for an isolated recon namespace."""
    if max_egress_bytes <= 0:
        raise ValueError("recon egress byte budget must be positive")
    db_rules: list[str] = []
    for raw_address in sorted(set(database_addresses)):
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise ValueError(f"invalid database address: {raw_address}") from exc
        family = "ip" if address.version == 4 else "ip6"
        db_rules.append(f"    {family} daddr {address} tcp dport 5432 accept")

    rules = [
        f'table inet {NFT_TABLE} {{',
        f'  comment "{NFT_MARKER}"',
        '  chain output {',
        '    type filter hook output priority 0; policy drop;',
        '    ct state established,related accept',
        '    oifname "lo" accept',
        '    ip daddr 127.0.0.11 udp dport 53 accept',
        '    ip daddr 127.0.0.11 tcp dport 53 accept',
        *db_rules,
    ]
    for network in _NON_PUBLIC_NETWORKS:
        family = "ip" if network.version == 4 else "ip6"
        rules.append(f"    {family} daddr {network} drop")
    rules.extend(
        [
            f'    quota over {max_egress_bytes} bytes drop',
            '    ip6 daddr 2000::/3 accept',
            '    ip daddr 0.0.0.0/0 accept',
            '  }',
            '}',
        ]
    )
    return "\n".join(rules) + "\n"


def _guard_socket_policy_installed(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
            client.sendall(b"check\n")
            return client.recv(16) == b"ok\n"
    except OSError:
        return False


def network_policy_installed(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    use_guard_socket: bool = True,
) -> bool:
    socket_path = os.getenv("RECON_EGRESS_GUARD_SOCKET")
    if use_guard_socket and socket_path:
        return _guard_socket_policy_installed(Path(socket_path))
    try:
        result = runner(
            ["nft", "-nn", "list", "table", "inet", NFT_TABLE],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    output = result.stdout or ""
    return result.returncode == 0 and NFT_MARKER in output and "hook output" in output and "policy drop" in output


def _database_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError(f"cannot resolve recon database host {host}") from exc
    addresses = tuple(sorted({record[4][0].split("%", 1)[0] for record in records}))
    if not addresses:
        raise RuntimeError(f"recon database host has no addresses: {host}")
    return addresses


def install_network_policy(
    database_host: str,
    *,
    database_port: int = 5432,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, ...]:
    addresses = _database_addresses(database_host, database_port)
    exists = runner(
        ["nft", "list", "table", "inet", NFT_TABLE],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    ).returncode == 0
    try:
        max_egress_bytes = int(
            os.getenv("RECON_MAX_EGRESS_BYTES", str(MAX_RECON_EGRESS_BYTES))
        )
    except ValueError as exc:
        raise RuntimeError("RECON_MAX_EGRESS_BYTES must be a positive integer") from exc
    if not 0 < max_egress_bytes <= MAX_RECON_EGRESS_BYTES:
        raise RuntimeError(
            f"RECON_MAX_EGRESS_BYTES must be between 1 and {MAX_RECON_EGRESS_BYTES}"
        )
    policy = render_nft_policy(addresses, max_egress_bytes=max_egress_bytes)
    if exists:
        policy = f"delete table inet {NFT_TABLE}\n" + policy
    result = runner(
        ["nft", "-f", "-"],
        input=policy,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not network_policy_installed(
        runner=runner, use_guard_socket=False
    ):
        raise RuntimeError(f"failed to install public-only recon firewall: {(result.stderr or '')[:300]}")
    return addresses


def _refresh_database_policy(
    database_host: str, known_addresses: tuple[str, ...]
) -> tuple[tuple[str, ...], bool]:
    """Reinstall only when Docker DNS moves the exact database allow-rule."""
    addresses = _database_addresses(database_host, 5432)
    if addresses == known_addresses:
        return known_addresses, False
    return install_network_policy(database_host), True


def _database_refresh_seconds() -> int:
    try:
        value = int(os.getenv("RECON_EGRESS_DATABASE_REFRESH_SECONDS", str(DEFAULT_DATABASE_REFRESH_SECONDS)))
    except ValueError as exc:
        raise RuntimeError("RECON_EGRESS_DATABASE_REFRESH_SECONDS must be an integer") from exc
    if not 5 <= value <= 3600:
        raise RuntimeError("RECON_EGRESS_DATABASE_REFRESH_SECONDS must be between 5 and 3600")
    return value


async def serve_guard(database_host: str) -> None:
    database_addresses = install_network_policy(database_host)
    socket_path = Path(os.getenv("RECON_EGRESS_GUARD_SOCKET", str(DEFAULT_GUARD_SOCKET)))
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shared_gid = int(os.getenv("RECON_EGRESS_GUARD_GID", "1000"))
    except ValueError as exc:
        raise RuntimeError("RECON_EGRESS_GUARD_GID must be numeric") from exc
    os.chown(socket_path.parent, -1, shared_gid)
    os.chmod(socket_path.parent, 0o750)
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise RuntimeError("recon egress guard socket path is not a socket")
        socket_path.unlink()

    async def attest(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"ok\n" if network_policy_installed(use_guard_socket=False) else b"blocked\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(attest, path=str(socket_path))
    os.chown(socket_path, -1, shared_gid)
    os.chmod(socket_path, 0o660)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    logger.info("recon public-only egress policy installed")
    try:
        refresh_seconds = _database_refresh_seconds()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=refresh_seconds)
            except TimeoutError:
                try:
                    database_addresses, refreshed = _refresh_database_policy(
                        database_host, database_addresses
                    )
                    if refreshed:
                        logger.info("recon database allow-rule refreshed after address change")
                except Exception as exc:  # noqa: BLE001 - retain the last known fail-closed policy
                    logger.warning("recon database allow-rule refresh failed: %s", exc)
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install/check the SpiderFoot public-only egress guard")
    parser.add_argument("--database-host", default="postgres")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(0 if network_policy_installed(use_guard_socket=False) else 1)
    if args.serve:
        logging.basicConfig(level=logging.INFO)
        asyncio.run(serve_guard(args.database_host))
        return
    parser.error("choose --check or --serve")


if __name__ == "__main__":
    main()
