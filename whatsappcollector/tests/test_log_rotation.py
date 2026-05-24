"""
Property test for log rotation configuration

Property 19: Log Rotation Uniformity
**Validates: Requirements 10.1, 10.3**

FOR ALL services in docker-compose.yml, the logging config SHALL have
max-size="50m" and max-file="5".
"""

import os
import yaml
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Path to docker-compose.yml at workspace root
COMPOSE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-compose.yml")


def load_compose():
    with open(COMPOSE_FILE) as f:
        return yaml.safe_load(f)


def get_service_names():
    compose = load_compose()
    return list(compose.get("services", {}).keys())


SERVICE_NAMES = get_service_names()


# --- Concrete test: ALL services at once ---

def test_all_services_have_correct_log_rotation():
    """All 12 services must have max-size=50m and max-file=5."""
    compose = load_compose()
    services = compose.get("services", {})

    failures = []
    for name, config in services.items():
        logging_cfg = config.get("logging")
        if logging_cfg is None:
            failures.append(f"{name}: missing logging config")
            continue
        if logging_cfg.get("driver") != "json-file":
            failures.append(f"{name}: driver={logging_cfg.get('driver')!r}, expected 'json-file'")
        options = logging_cfg.get("options", {})
        if options.get("max-size") != "50m":
            failures.append(f"{name}: max-size={options.get('max-size')!r}, expected '50m'")
        if options.get("max-file") != "5":
            failures.append(f"{name}: max-file={options.get('max-file')!r}, expected '5'")

    assert not failures, "Log rotation config failures:\n" + "\n".join(failures)


# --- Property test: each service individually via hypothesis ---

@given(service_name=st.sampled_from(SERVICE_NAMES))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_service_log_rotation_uniformity(service_name):
    """
    Property 19: Log Rotation Uniformity
    **Validates: Requirements 10.1, 10.3**

    FOR ALL services in docker-compose.yml, the logging config SHALL have
    driver="json-file", max-size="50m", and max-file="5".
    """
    compose = load_compose()
    services = compose.get("services", {})

    assert service_name in services, f"Service {service_name!r} not found in docker-compose.yml"

    config = services[service_name]
    logging_cfg = config.get("logging")

    assert logging_cfg is not None, f"Service {service_name!r} has no logging config"
    assert logging_cfg.get("driver") == "json-file", (
        f"Service {service_name!r}: driver={logging_cfg.get('driver')!r}, expected 'json-file'"
    )

    options = logging_cfg.get("options", {})
    assert options.get("max-size") == "50m", (
        f"Service {service_name!r}: max-size={options.get('max-size')!r}, expected '50m'"
    )
    assert options.get("max-file") == "5", (
        f"Service {service_name!r}: max-file={options.get('max-file')!r}, expected '5'"
    )
