import ctypes
import io
import os
import sys
from typing import TextIO


_CONFIGURED = False

_REPLACEMENTS = {
    "\uFE0F": "",
    "✅": "[OK]",
    "❌": "[ERROR]",
    "⚠": "[WARN]",
    "⚙": "[TOOLS]",
    "ℹ": "[INFO]",
    "🚀": "[START]",
    "🔄": "[STEP]",
    "🎉": "[DONE]",
    "📊": "[STATS]",
    "📁": "[DIR]",
    "📂": "[DIR]",
    "📦": "[PKG]",
    "📄": "[FILE]",
    "📋": "[LIST]",
    "📌": "[NOTE]",
    "📈": "[SUMMARY]",
    "📜": "[POLICY]",
    "📸": "[PHOTO]",
    "📞": "[PHONE]",
    "📱": "[PHONE]",
    "💬": "[CHAT]",
    "💡": "[TIP]",
    "👥": "[ACCOUNTS]",
    "👤": "[ACCOUNT]",
    "👋": "[EXIT]",
    "🔧": "[TOOLS]",
    "🔌": "[LOAD]",
    "🔐": "[AUTH]",
    "🔑": "[KEY]",
    "🔗": "[LINK]",
    "🗑": "[DELETE]",
    "📝": "[WRITE]",
    "🔍": "[CHECK]",
    "🔥": "[CORE]",
    "📥": "[IMPORT]",
    "📤": "[EXPORT]",
    "🌐": "[WEB]",
    "🧪": "[TEST]",
    "🗄": "[DB]",
    "🧹": "[CLEAN]",
    "⚡": "[FAST]",
    "🛡": "[SAFE]",
    "⚖": "[BALANCED]",
    "➕": "[ADD]",
    "1️⃣": "1.",
    "1⃣": "1.",
    "2️⃣": "2.",
    "2⃣": "2.",
    "3️⃣": "3.",
    "3⃣": "3.",
    "4️⃣": "4.",
    "4⃣": "4.",
    "5️⃣": "5.",
    "5⃣": "5.",
    "6️⃣": "6.",
    "6⃣": "6.",
    "7️⃣": "7.",
    "7⃣": "7.",
    "8️⃣": "8.",
    "8⃣": "8.",
    "9️⃣": "9.",
    "9⃣": "9.",
    "0️⃣": "0.",
    "0⃣": "0.",
    "🆕": "[NEW]",
    "•": "-",
    "─": "-",
    "━": "-",
    "│": "|",
    "┌": "+",
    "┐": "+",
    "└": "+",
    "┘": "+",
    "→": "->",
    "—": "-",
}


def normalize_console_text(text: object) -> str:
    normalized = str(text)
    for source, target in _REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    return normalized


class SafeConsoleStream(io.TextIOBase):
    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, s: str) -> int:
        return self._stream.write(normalize_console_text(s))

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)

    @property
    def buffer(self):  # type: ignore[override]
        return getattr(self._stream, "buffer", None)

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _set_windows_console_utf8() -> None:
    if os.name != "nt":
        return

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _reconfigure_stream(stream_name: str) -> None:
    stream = getattr(sys, stream_name, None)
    if stream is None:
        return

    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _wrap_stream(stream_name: str) -> None:
    stream = getattr(sys, stream_name, None)
    if stream is None or isinstance(stream, SafeConsoleStream):
        return
    setattr(sys, stream_name, SafeConsoleStream(stream))


def configure_console_output() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _set_windows_console_utf8()
    _reconfigure_stream("stdout")
    _reconfigure_stream("stderr")
    _wrap_stream("stdout")
    _wrap_stream("stderr")
    _CONFIGURED = True
