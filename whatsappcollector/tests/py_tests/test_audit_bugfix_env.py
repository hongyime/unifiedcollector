"""Tests for environment/config fixes: BUG-1 (stale artifact), BUG-6 (workflow files), BUG-10 (docker-compose.dev.yml)."""
from __future__ import annotations

import os

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# BUG-1: Stale build artifact removed
# ---------------------------------------------------------------------------

class TestStaleArtifactRemoved:
    def test_history_sync_js_does_not_exist(self):
        artifact = os.path.join(REPO_ROOT, "services", "wa-client-ts", "build", "history_sync.js")
        assert not os.path.exists(artifact), (
            f"Stale build artifact still exists: {artifact}"
        )


# ---------------------------------------------------------------------------
# BUG-6: Workflow files contain whatsappcollector-specific content
# ---------------------------------------------------------------------------

class TestWorkflowFilesRewritten:
    def test_start_backend_has_docker_compose(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "start-backend.md"))
        assert "docker compose" in content

    def test_start_backend_has_wa_client_ts(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "start-backend.md"))
        assert "wa-client-ts" in content

    def test_start_backend_has_migrations(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "start-backend.md"))
        assert "migrations" in content.lower() or "run_migrations" in content

    def test_start_backend_no_ticketremaster(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "start-backend.md"))
        assert "ticketremaster-b" not in content

    def test_start_backend_no_flask_run(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "start-backend.md"))
        assert "flask run" not in content

    def test_system_has_docker_compose(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "system.md"))
        assert "docker compose" in content

    def test_system_has_whatsappcollector_keywords(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "system.md"))
        assert "wa-client-ts" in content or "whatsappcollector" in content

    def test_system_no_dejavista(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "system.md"))
        assert "DejaVista" not in content
        assert "dejavista" not in content.lower()

    def test_system_no_chrome_extensions(self):
        content = _read(os.path.join(REPO_ROOT, ".agent", "workflows", "system.md"))
        assert "chrome://extensions" not in content


# ---------------------------------------------------------------------------
# BUG-10: docker-compose.dev.yml uses correct service names
# ---------------------------------------------------------------------------

class TestDockerComposeDevFixed:
    def _load_compose(self):
        path = os.path.join(REPO_ROOT, "docker-compose.dev.yml")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_no_stale_wa_client_ts_service(self):
        data = self._load_compose()
        services = data.get("services", {})
        assert "wa-client-ts" not in services, (
            "Legacy service 'wa-client-ts' still present in docker-compose.dev.yml"
        )

    def test_no_processor_py_service(self):
        data = self._load_compose()
        services = data.get("services", {})
        assert "processor-py" not in services, (
            "Legacy service 'processor-py' still present in docker-compose.dev.yml"
        )

    def test_wa_client_ts_1_present(self):
        data = self._load_compose()
        services = data.get("services", {})
        assert "wa-client-ts-1" in services, (
            "Service 'wa-client-ts-1' missing from docker-compose.dev.yml"
        )

    def test_wa_client_ts_1_has_src_volume(self):
        data = self._load_compose()
        svc = data["services"]["wa-client-ts-1"]
        volumes = svc.get("volumes", [])
        assert any("wa-client-ts/src" in str(v) for v in volumes)

    def test_wa_client_ts_1_has_debug_inspector_port(self):
        data = self._load_compose()
        svc = data["services"]["wa-client-ts-1"]
        ports = svc.get("ports", [])
        assert any("9229" in str(p) for p in ports)

    def test_collector_present(self):
        data = self._load_compose()
        assert "collector" in data.get("services", {})

    def test_collector_has_debug_port(self):
        data = self._load_compose()
        svc = data["services"]["collector"]
        ports = svc.get("ports", [])
        assert any("5678" in str(p) for p in ports)
