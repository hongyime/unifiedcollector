"""
shared/port_finder.py — Auto port failover for Streamlit dashboard services.

Fixes P2.1: Hardcoded dashboard ports cause crash-loop when the primary port
is already bound. This module probes ports in a configurable range and starts
Streamlit on the first available one.

Also provides a PortRegistry that persists allocations to
~/.telegramcollector/ports.json so sibling services and other projects on the
same machine never claim the same host port.
"""

import json
import logging
import os
import socket
import sys
from pathlib import Path

__all__ = [
    "find_free_port",
    "run_streamlit_with_port_failover",
    "allocate_port",
    "release_port",
    "get_allocated_ports",
]

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path.home() / ".telegramcollector" / "ports.json"


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    """Loads the port registry from disk."""
    try:
        if REGISTRY_PATH.exists():
            return json.loads(REGISTRY_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_registry(registry: dict) -> None:
    """Saves the port registry to disk."""
    try:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2))
    except Exception as e:
        logger.warning(f"Could not save port registry: {e}")


def _is_port_free(port: int) -> bool:
    """Returns True if the port is not bound on the system."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_free_port(primary: int, search_range: int = 20) -> int:
    """
    Returns the first free TCP port in [primary, primary + search_range].

    Checks both system-level availability and the registry to avoid conflicts
    with sibling services in this project.

    Raises RuntimeError if no port is free in the range.
    """
    registry = _load_registry()
    allocated_ports = set(registry.values())

    for port in range(primary, primary + search_range + 1):
        if port in allocated_ports:
            continue
        if _is_port_free(port):
            return port

    raise RuntimeError(f"No free port in range {primary}–{primary + search_range}")


def allocate_port(service_name: str, preferred_port: int, search_range: int = 20) -> int:
    """
    Allocates a free port for a named service.

    - Checks system-level port availability
    - Checks the registry to avoid conflicts with sibling services
    - Saves the allocation to ~/.telegramcollector/ports.json
    - Returns the allocated port

    Args:
        service_name: Unique name for this service (e.g. 'collector_dashboard')
        preferred_port: The port to try first
        search_range: How many ports above preferred to try

    Returns:
        The allocated port number

    Raises:
        RuntimeError: If no free port found in range
    """
    registry = _load_registry()
    allocated_ports = set(registry.values())

    for port in range(preferred_port, preferred_port + search_range + 1):
        if port in allocated_ports and registry.get(service_name) != port:
            # Already claimed by another service in this project
            continue
        if _is_port_free(port):
            registry[service_name] = port
            _save_registry(registry)
            if port != preferred_port:
                logger.info(
                    f"Port {preferred_port} unavailable, using {port} for {service_name}"
                )
            return port

    raise RuntimeError(
        f"No free port found for {service_name} in range "
        f"{preferred_port}–{preferred_port + search_range}"
    )


def release_port(service_name: str) -> None:
    """Removes a service's port allocation from the registry."""
    registry = _load_registry()
    registry.pop(service_name, None)
    _save_registry(registry)


def get_allocated_ports() -> dict:
    """Returns all currently allocated ports from the registry."""
    return _load_registry()


def run_streamlit_with_port_failover(
    app_path: str,
    primary_port: int,
    service_name: str = None,
    search_range: int = None,
    extra_args: list = None,
) -> None:
    """
    Finds a free port (avoiding conflicts with other projects and sibling
    services), then exec()s streamlit on that port. Never returns on success.

    Args:
        app_path: Path to the Streamlit app file
        primary_port: Preferred port to try first
        service_name: Registry key for this service (defaults to 'dashboard_<port>')
        search_range: Ports above primary to probe (defaults to DASHBOARD_PORT_SEARCH_RANGE)
        extra_args: Additional args to pass to streamlit
    """
    from shared.config import settings

    if search_range is None:
        search_range = getattr(settings, "DASHBOARD_PORT_SEARCH_RANGE", 20)
    if service_name is None:
        service_name = f"dashboard_{primary_port}"

    port = allocate_port(service_name, primary_port, search_range)

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        app_path,
        f"--server.port={port}",
        "--server.address=0.0.0.0",
    ]
    if extra_args:
        cmd.extend(extra_args)

    logger.info(f"Starting {service_name} on port {port}")
    os.execv(sys.executable, cmd)
