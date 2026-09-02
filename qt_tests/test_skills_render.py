"""Real-Qt confirmation for ai_dialog._skills_html (View skills' rendering fix).

The plain suite (tests/test_ai_flows.py) pins the string transform itself; this
renders that HTML through a real QTextDocument, the same rich-text engine
_ask_scrollable's QLabel uses, and reads back what it actually produces:
confirming by rendering, not by assuming a plain "\\n" -> "<br>" swap is enough.
"""
from PyQt6.QtGui import QTextDocument

import harness
from internpearls import ai_dialog


def _rendered_plain_text(html_body):
    doc = QTextDocument()
    doc.setHtml(html_body)
    return doc.toPlainText()


def test_skill_lines_render_as_separate_lines_not_one_run_on_blob():
    harness.bootstrap()
    parts = ["Bundled: InternPearls authoring (ships with the add-on)", "",
            "One fact per card.\nPrefer cloze for lists.\nEscape < and > in values."]
    rendered = _rendered_plain_text(ai_dialog._skills_html(parts))
    lines = [l for l in rendered.splitlines() if l.strip()]
    # Every non-blank source line survives as its OWN rendered line: the
    # defect this fixes was every "\n" collapsing into one run-on line.
    assert lines == ["Bundled: InternPearls authoring (ships with the add-on)",
                     "One fact per card.", "Prefer cloze for lists.",
                     "Escape < and > in values."]


def test_literal_angle_brackets_in_skill_text_render_as_text_not_markup():
    harness.bootstrap()
    # The real bundled skill text says this almost verbatim; a naive RichText
    # render would parse <table> as a real (empty, unclosed) table element and
    # swallow it rather than showing the sentence a reviewer asked to read.
    parts = ["Comparisons of two things are an HTML <table>. "
            "Four or more grouped causes are a <ul>."]
    rendered = _rendered_plain_text(ai_dialog._skills_html(parts))
    assert "<table>" in rendered
    assert "<ul>" in rendered


def test_the_actual_bundled_skill_text_renders_readably():
    """The real thing, not a hand-picked excerpt: the bundled skill file is known
    to carry raw '<table>'/'<ul>'/'&lt;' tokens (see SKILL.md), so rendering it
    end to end is the strongest version of "confirm by rendering"."""
    harness.bootstrap()
    from internpearls import ai_logic
    parts = ["Bundled: InternPearls authoring (ships with the add-on)", "",
            ai_logic.load_bundled_skill()]
    rendered = _rendered_plain_text(ai_dialog._skills_html(parts))
    # The skill text is many lines; a collapsed blob would render as one.
    assert len(rendered.splitlines()) > 10
    assert "<table>" in rendered
