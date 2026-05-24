"""Tests for documentation fixes: BUG-7 (POST /send-media), BUG-8 (messages.status), BUG-9 (shared/)."""
from __future__ import annotations

import os

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
README_PATH = os.path.join(REPO_ROOT, "README.md")
PRD_PATH = os.path.join(REPO_ROOT, "PRD.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# BUG-7: POST /send-media documented in README and PRD
# ---------------------------------------------------------------------------

class TestSendMediaDocumented:
    def test_readme_http_contracts_has_send_media(self):
        content = _read(README_PATH)
        assert "/send-media" in content

    def test_readme_send_media_has_x_signature(self):
        content = _read(README_PATH)
        assert "X-Signature" in content

    def test_readme_send_media_has_target_chat_jid(self):
        content = _read(README_PATH)
        assert "target_chat_jid" in content

    def test_readme_send_media_has_file_path(self):
        content = _read(README_PATH)
        assert "file_path" in content

    def test_readme_send_media_has_media_storage_path_constraint(self):
        content = _read(README_PATH)
        assert "MEDIA_STORAGE_PATH" in content

    def test_prd_section5_has_send_media(self):
        content = _read(PRD_PATH)
        # POST /send-media must appear in §5 Internal API Contract
        idx_5 = content.find("## 5)")
        assert idx_5 != -1, "Section 5 not found in PRD"
        idx_6 = content.find("## 6)", idx_5)
        section5 = content[idx_5:idx_6] if idx_6 != -1 else content[idx_5:]
        assert "/send-media" in section5


# ---------------------------------------------------------------------------
# BUG-8: messages.status queue documented in PRD §3.3 and README
# ---------------------------------------------------------------------------

class TestMessagesStatusDocumented:
    def test_prd_section33_has_messages_status(self):
        content = _read(PRD_PATH)
        idx_33 = content.find("### 3.3")
        assert idx_33 != -1, "Section 3.3 not found in PRD"
        # Grab until next section heading
        idx_next = content.find("## 4)", idx_33)
        section33 = content[idx_33:idx_next] if idx_next != -1 else content[idx_33:]
        assert "messages.status" in section33

    def test_readme_broker_section_has_messages_status(self):
        content = _read(README_PATH)
        assert "messages.status" in content

    def test_prd_messages_status_mentions_producer(self):
        content = _read(PRD_PATH)
        idx = content.find("messages.status")
        assert idx != -1
        surrounding = content[max(0, idx - 50):idx + 300]
        assert "wa-client-ts" in surrounding

    def test_prd_messages_status_mentions_consumer(self):
        content = _read(PRD_PATH)
        idx = content.find("messages.status")
        assert idx != -1
        surrounding = content[max(0, idx - 50):idx + 300]
        assert "collector" in surrounding


# ---------------------------------------------------------------------------
# BUG-9: shared/ package documented in README and PRD §3.1
# ---------------------------------------------------------------------------

class TestSharedPackageDocumented:
    def test_readme_architecture_table_has_shared_row(self):
        content = _read(README_PATH)
        assert "shared/" in content

    def test_readme_shared_mentions_circuit_breaker(self):
        content = _read(README_PATH)
        idx = content.find("shared/")
        assert idx != -1
        surrounding = content[idx:idx + 300]
        assert "circuit_breaker" in surrounding or "circuit breaker" in surrounding.lower()

    def test_prd_section31_has_shared_entry(self):
        content = _read(PRD_PATH)
        idx_31 = content.find("### 3.1")
        assert idx_31 != -1, "Section 3.1 not found in PRD"
        idx_32 = content.find("### 3.2", idx_31)
        section31 = content[idx_31:idx_32] if idx_32 != -1 else content[idx_31:]
        assert "shared/" in section31

    def test_prd_shared_lists_key_modules(self):
        content = _read(PRD_PATH)
        idx = content.find("shared/")
        assert idx != -1
        surrounding = content[idx:idx + 400]
        for module in ("circuit_breaker", "task_supervisor", "dlq", "config", "db", "redis_client", "observability"):
            assert module in surrounding, f"Module '{module}' not found near shared/ entry"
