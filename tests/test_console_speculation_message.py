"""The consoles show WHY MTP did not run in words (the outcome's `message`), not only its slug."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_web_console_prefers_the_outcome_message():
    source = (ROOT / "src" / "abstractgateway" / "console.py").read_text(encoding="utf-8")
    assert "value.message ? `: ${value.message}`" in source
    assert "caps.message || caps.reason" in source


def test_terminal_console_prefers_the_outcome_message():
    source = (ROOT / "console-tui" / "src" / "worker.rs").read_text(encoding="utf-8")
    block = source[source.index('"MTP not used{}"'):][:600]
    assert '.get("message")' in block and block.index('"message"') < block.index('"reason"')
