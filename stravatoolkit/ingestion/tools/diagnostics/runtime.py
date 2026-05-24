from __future__ import annotations

import warnings
from dataclasses import dataclass
from importlib import metadata


REQUESTS_DEPENDENCY_WARNING_RE = r".*doesn't match a supported version!.*"

_dependency_audit_emitted = False


@dataclass(frozen=True, slots=True)
class RequestsDependencyHealth:
    requests: str | None
    urllib3: str | None
    chardet: str | None
    charset_normalizer: str | None
    issue: str


def bootstrap_requests_dependency_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=REQUESTS_DEPENDENCY_WARNING_RE,
        category=Warning,
    )


def emit_requests_dependency_health_once() -> RequestsDependencyHealth | None:
    global _dependency_audit_emitted

    if _dependency_audit_emitted:
        return None
    _dependency_audit_emitted = True

    health = get_requests_dependency_health()
    if health is None:
        return None

    versions = (
        f"requests {health.requests or 'missing'}, "
        f"urllib3 {health.urllib3 or 'missing'}, "
        f"chardet {health.chardet or 'not installed'}, "
        f"charset-normalizer {health.charset_normalizer or 'not installed'}"
    )
    print(
        "[env-warning] Shared Python dependency drift detected: "
        f"{versions}. {health.issue}. Toolkit will continue. "
        "Safest fix: use a project virtual environment or align the global package versions."
    )
    return health


def get_requests_dependency_health() -> RequestsDependencyHealth | None:
    requests_version = _installed_version("requests")
    urllib3_version = _installed_version("urllib3")
    chardet_version = _installed_version("chardet")
    charset_normalizer_version = _installed_version("charset-normalizer")

    if requests_version is None or urllib3_version is None:
        return None

    urllib3_tuple = _parse_version_tuple(urllib3_version)
    if urllib3_tuple is None or urllib3_tuple < (1, 21, 1):
        return RequestsDependencyHealth(
            requests=requests_version,
            urllib3=urllib3_version,
            chardet=chardet_version,
            charset_normalizer=charset_normalizer_version,
            issue="requests expects urllib3 >= 1.21.1",
        )

    if chardet_version is not None:
        chardet_tuple = _parse_version_tuple(chardet_version)
        if chardet_tuple is None or not ((3, 0, 2) <= chardet_tuple < (6, 0, 0)):
            return RequestsDependencyHealth(
                requests=requests_version,
                urllib3=urllib3_version,
                chardet=chardet_version,
                charset_normalizer=charset_normalizer_version,
                issue="requests treats the installed chardet version as unsupported",
            )

    if chardet_version is None and charset_normalizer_version is not None:
        charset_tuple = _parse_version_tuple(charset_normalizer_version)
        if charset_tuple is None or not ((2, 0, 0) <= charset_tuple < (4, 0, 0)):
            return RequestsDependencyHealth(
                requests=requests_version,
                urllib3=urllib3_version,
                chardet=chardet_version,
                charset_normalizer=charset_normalizer_version,
                issue="requests treats the installed charset-normalizer version as unsupported",
            )

    if chardet_version is None and charset_normalizer_version is None:
        return RequestsDependencyHealth(
            requests=requests_version,
            urllib3=urllib3_version,
            chardet=chardet_version,
            charset_normalizer=charset_normalizer_version,
            issue="requests could not find chardet or charset-normalizer",
        )

    return None


def _installed_version(distribution_name: str) -> str | None:
    try:
        return metadata.version(distribution_name)
    except metadata.PackageNotFoundError:
        return None


def _parse_version_tuple(value: str) -> tuple[int, int, int] | None:
    parts = value.split(".")[:3]
    if len(parts) < 3:
        parts.extend(["0"] * (3 - len(parts)))
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None
