import unittest

from src.core.console import normalize_console_text


class ConsoleOutputTests(unittest.TestCase):
    def test_normalizes_ui_symbols_for_readable_console_output(self):
        text = "✅ Ready • Item 1️⃣ ⚠️"
        self.assertEqual(normalize_console_text(text), "[OK] Ready - Item 1. [WARN]")

    def test_preserves_non_mapped_text(self):
        text = "Group name: Привет"
        self.assertEqual(normalize_console_text(text), text)


if __name__ == "__main__":
    unittest.main()
