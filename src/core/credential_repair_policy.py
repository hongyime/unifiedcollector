"""Repair-only credential classification for the controlled recovery path."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

HumanBlock = Literal["qr", "2fa", "oauth", "pat", "beeper_token", "ghunt_login"]
HUMAN_BLOCKS = frozenset({"qr", "2fa", "oauth", "pat", "beeper_token", "ghunt_login"})


@dataclass(frozen=True)
class CredentialDecision:
    status: Literal["ok", "repair", "blocked"]
    action: Literal["preserve", "repair", "human_action"]
    reasons: tuple[str, ...]
    rollback_eligible: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def classify_credential(
    *,
    present: bool,
    valid: bool,
    owned: bool,
    exposed: bool = False,
    revoked: bool = False,
    deployment_required: bool = False,
    human_block: HumanBlock | None = None,
) -> CredentialDecision:
    """Classify a credential without reading, rotating, or revoking it.

    A valid owned session is always preserved. Any QR/2FA/OAuth/PAT/Beeper or
    GHunt interaction remains blocked for a human rather than being automated.
    """
    if human_block is not None:
        if human_block not in HUMAN_BLOCKS:
            raise ValueError(f"unknown human block: {human_block}")
        return CredentialDecision("blocked", "human_action", (f"human_{human_block}",), not (exposed or revoked))
    reasons: list[str] = []
    if not present:
        reasons.append("missing")
    if present and not valid:
        reasons.append("expired")
    if not owned:
        reasons.append("misowned")
    if exposed:
        reasons.append("exposed")
    if revoked:
        reasons.append("revoked")
    if deployment_required:
        reasons.append("deployment_required")
    if reasons:
        return CredentialDecision("repair", "repair", tuple(reasons), not (exposed or revoked))
    return CredentialDecision("ok", "preserve", (), True)
