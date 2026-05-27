import bulk_website_importer


class DummyConfig:
    def _is_duplicate_website(self, name, url):
        return False, ""

    def add_website(self, **kwargs):
        raise AssertionError("Invalid URLs must not be added to the config")

    def toggle_website(self, name):
        raise AssertionError("Invalid URLs must not be toggled")

    def save_config(self):
        return True


def test_process_website_rejects_invalid_urls(monkeypatch):
    dummy_config = DummyConfig()
    monkeypatch.setattr(bulk_website_importer, "get_config", lambda: dummy_config)
    monkeypatch.setattr(
        bulk_website_importer,
        "validate_website_url",
        lambda url: (False, "Invalid URL format"),
    )

    importer = bulk_website_importer.BulkWebsiteImporter()
    importer._process_website(
        {
            "name": "Bad Site",
            "url": "notaurl",
            "source_line": 7,
        },
        auto_enable=True,
        skip_duplicates=True,
    )

    assert importer.imported_count == 0
    assert importer.error_count == 1
    assert importer.results == [
        {
            "line": 7,
            "name": "Bad Site",
            "url": "notaurl",
            "status": "error",
            "reason": "Invalid URL format",
        }
    ]
