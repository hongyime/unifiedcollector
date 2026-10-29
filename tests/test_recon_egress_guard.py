from __future__ import annotations

import socket
import subprocess

import pytest

from src.core.recon_egress_guard import (
    MAX_RECON_EGRESS_BYTES,
    UnsafeReconTarget,
    install_network_policy,
    is_public_address,
    network_policy_installed,
    render_nft_policy,
    resolve_public_addresses,
    validate_recon_target,
)


def test_worker_guard_socket_probe_accepts_only_a_live_guard(monkeypatch):
    class Client:
        reply = b"ok\n"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            return None

        def connect(self, path):
            assert path == "/guard/status.sock"

        def sendall(self, payload):
            assert payload == b"check\n"

        def recv(self, _size):
            return self.reply

    client = Client()
    monkeypatch.setenv("RECON_EGRESS_GUARD_SOCKET", "/guard/status.sock")
    monkeypatch.setattr("src.core.recon_egress_guard.socket.socket", lambda *_args: client)
    assert network_policy_installed()
    client.reply = b"blocked\n"
    assert not network_policy_installed()


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "168.63.129.16",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "2002::1",
        "3fff::1",
    ],
)
def test_non_global_addresses_are_rejected(value):
    assert not is_public_address(value)


@pytest.mark.parametrize("value", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_global_unicast_addresses_are_allowed(value):
    assert is_public_address(value)


def test_mixed_dns_answers_are_rejected_as_a_set():
    def resolver(host, port, family, socktype):
        assert host == "mixed.example"
        assert port is None
        assert family == socket.AF_UNSPEC
        assert socktype == socket.SOCK_STREAM
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 0, 0, 0)),
        ]

    with pytest.raises(UnsafeReconTarget, match="non-public DNS answer"):
        resolve_public_addresses("mixed.example", resolver=resolver)


def test_dns_rebinding_is_rejected_on_each_resolution():
    answers = iter(("8.8.8.8", "169.254.169.254"))

    def resolver(_host, _port, family, socktype):
        address = next(answers)
        af = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 0, 0, 0) if af == socket.AF_INET6 else (address, 0)
        return [(af, socket.SOCK_STREAM, 6, "", sockaddr)]

    assert resolve_public_addresses("rebind.example", resolver=resolver) == ("8.8.8.8",)
    with pytest.raises(UnsafeReconTarget, match="non-public DNS answer"):
        resolve_public_addresses("rebind.example", resolver=resolver)


@pytest.mark.parametrize(
    ("target_type", "target_value"),
    [
        ("ipv4", "127.0.0.1"),
        ("ip", "169.254.169.254"),
        ("url", "http://[::1]/admin"),
        ("url", "file:///etc/passwd"),
        ("url", "http://user:pass@example.com/"),
        ("email", "user@127.0.0.1"),
        ("domain", "127.0.0.1"),
    ],
)
def test_invalid_or_non_public_targets_are_rejected(target_type, target_value):
    with pytest.raises(UnsafeReconTarget):
        validate_recon_target(target_type, target_value, resolver=lambda *_: [])


def test_valid_public_target_without_scope_admission_is_accepted():
    def resolver(host, port, family, socktype):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    assert validate_recon_target("domain", "unlisted.example", resolver=resolver)
    assert validate_recon_target(
        "url", "https://unlisted.example/path", resolver=resolver
    )


def test_firewall_policy_drops_by_default_and_denies_private_destinations():
    rules = render_nft_policy(["172.30.0.4"])

    assert 'policy drop' in rules
    assert 'ip daddr 172.30.0.4 tcp dport 5432 accept' in rules
    assert 'ip daddr 127.0.0.11 udp dport 53 accept' in rules
    assert 'ip daddr 169.254.0.0/16 drop' in rules
    assert 'ip daddr 168.63.129.16/32 drop' in rules
    assert 'hook output priority 0; policy drop' in rules
    assert 'ip6 daddr fc00::/7 drop' in rules
    assert 'ip6 daddr 2000::/3 accept' in rules
    assert 'ip daddr 0.0.0.0/0 accept' in rules
    assert 'quota over 1073741824 bytes drop' in rules


def test_firewall_rejects_nonpositive_byte_budget():
    with pytest.raises(ValueError, match="byte budget must be positive"):
        render_nft_policy([], max_egress_bytes=0)


def test_firewall_rejects_attempt_to_raise_hard_egress_budget(monkeypatch):
    monkeypatch.setenv("RECON_MAX_EGRESS_BYTES", str(MAX_RECON_EGRESS_BYTES + 1))
    monkeypatch.setattr(
        "src.core.recon_egress_guard._database_addresses",
        lambda *_args: ("203.0.113.1",),
    )
    calls = []

    def runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="must be between 1"):
        install_network_policy("postgres", runner=runner)
    assert len(calls) == 1
    assert calls[0][:3] == ["nft", "list", "table"]


def test_firewall_install_checks_nft_before_guard_socket_exists(monkeypatch):
    monkeypatch.setenv("RECON_EGRESS_GUARD_SOCKET", "/guard/not-ready.sock")
    monkeypatch.setattr(
        "src.core.recon_egress_guard._database_addresses",
        lambda *_args: ("203.0.113.1",),
    )
    calls = []

    def runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="uc-recon-egress-v1 hook output policy drop",
            stderr="",
        )

    install_network_policy("postgres", runner=runner)
    assert calls[-1] == ["nft", "-nn", "list", "table", "inet", "recon_egress"]
