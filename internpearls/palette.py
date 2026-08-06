"""The add-on's colours, in two sets, chosen by Anki's current theme.

Two literal sets rather than one set of theme-neutral colours, because there is no such
thing: measured against Anki's own window colours, no single value clears WCAG AA on both a
light and a dark background, so one set always loses. Tuning each theme separately is what
lets every role stay legible and keep its meaning.

Literal hex rather than palette() references, because the review rows are Qt rich text
inside a QLabel, and that subset has no palette functions. The choice therefore has to
happen in Python, here, and reach the stylesheet as a resolved value.

Every value is measured, not picked by eye. tests/test_palette.py holds the arithmetic.
"""

LIGHT = {
    "why":        "#226034",
    "accent":     "#1d4ed8",
    "dim":        "#5a6672",
    "caret":      "#3a444e",
    "muted":      "#63676c",
    "row_rule":   "#cccccc",
    "cell_rule":  "#a9b4ba",
    "panel_rule": "#c9c9c9",
    "dosing_bg":  "#eef2f7",
    "dosing_fg":  "#334155",
    "new_bg":     "#9dc0ee",
    "new_fg":     "#10305f",
    "updated_bg": "#efc277",
    "updated_fg": "#5e3103",
    "retired_bg": "#bcc5cd",
    "retired_fg": "#282d31",
    "moved_bg":   "#c9b3ea",
    "moved_fg":   "#3a1a63",
    "warning":    "#b33427",
}

DARK = {
    "why":        "#5fb87a",
    "accent":     "#8ab4ff",
    "dim":        "#a3b0b8",
    "caret":      "#c3cdd4",
    "muted":      "#9aa0a6",
    "row_rule":   "#4a4a4d",
    "cell_rule":  "#6b7378",
    "panel_rule": "#585a5f",
    "dosing_bg":  "#2a3440",
    "dosing_fg":  "#cbd5e1",
    "new_bg":     "#2c5285",
    "new_fg":     "#dbe9ff",
    "updated_bg": "#6b4f22",
    "updated_fg": "#ffe4b5",
    "retired_bg": "#4d545c",
    "retired_fg": "#dde4ea",
    "moved_bg":   "#5a4080",
    "moved_fg":   "#ecdcff",
    "warning":    "#f0968c",
}


def is_dark():
    """Whether Anki is in night mode right now.

    Read live rather than cached at import, so a dialog opened after the reader flips the
    theme picks up the new set. A dialog already on screen keeps its colours until it is
    reopened, same as Anki's own.

    Falls back to the light set if the theme manager is unavailable, which is the safer
    direction: light colours on a light window is the historical behaviour.
    """
    try:
        from aqt.theme import theme_manager
        return bool(theme_manager.night_mode)
    except Exception:
        return False


def colors():
    """The active colour set."""
    return DARK if is_dark() else LIGHT
