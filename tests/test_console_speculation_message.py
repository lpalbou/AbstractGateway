"""The consoles show WHY MTP did not run in words (the outcome's `message`), not only its slug."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_web_console_prefers_the_outcome_message():
    source = (ROOT / "src" / "abstractgateway" / "console.py").read_text(encoding="utf-8")
    assert "value.message ? `: ${value.message}`" in source
    # The capability note: the gateway's sentence, else a plain one; the reason slug only under
    # Technical details (tests/test_console_model_options.py drives it).
    assert "if (!caps.supported) return caps.message || MTP_UNSUPPORTED;" in source
    assert "showModelOptionNote(status, mtpCardNote(caps), caps?.reason" in source


def test_terminal_console_prefers_the_outcome_message():
    source = (ROOT / "console-tui" / "src" / "worker.rs").read_text(encoding="utf-8")
    block = source[source.index('"MTP not used{}"'):][:600]
    assert '.get("message")' in block and block.index('"message"') < block.index('"reason"')
