"""Tests for internpearls/widgets.py, the shared row and list vocabulary.

mock_anki's aqt stubs are already installed by conftest.py before this module imports,
same as every other test file here.
"""


def _stub_row(built, item):
    """A bare row builder that records what it was asked to build, so the streaming
    list tests measure the container's own batching behaviour rather than a card's
    rendering."""
    from aqt.qt import QWidget
    built.append(item)
    return QWidget()


def _pill(cell):
    """The QLabel inside a chip cell, or None when that cell holds no pill at all."""
    from aqt.qt import QLabel
    if cell._layout is None:
        return None
    return next((c for c in cell._layout._children if isinstance(c, QLabel)), None)


def test_chip_cell_carries_a_foreground_with_its_background():
    """The v0.32.1 rule. Matched on "; color:" because "background-color:" contains
    "color:" and would pass with no foreground set at all, which is exactly the bug."""
    from internpearls import widgets
    for kind in widgets.CHIPS:
        style = _pill(widgets.chip_cell(kind)).styleSheet()
        assert "; color:" in style, f"{kind} chip sets a background with no foreground"


def test_chip_cell_uses_the_active_palette():
    from internpearls import palette, widgets
    active = palette.colors()
    style = _pill(widgets.chip_cell("new")).styleSheet()
    assert active["new_bg"] in style and active["new_fg"] in style


def test_chip_cell_holds_no_pill_for_an_unknown_kind():
    """Still a cell, though: the column has to be reserved on an unchipped row or its
    text starts left of every other row's."""
    from internpearls import widgets
    assert _pill(widgets.chip_cell(None)) is None
    assert _pill(widgets.chip_cell("nonsense")) is None


def test_chip_cell_carries_every_kinds_label():
    from internpearls import widgets
    for kind, label in widgets.CHIPS.items():
        assert _pill(widgets.chip_cell(kind)).text() == label


def test_decision_cell_selected_colour_role_matches_what_the_choice_does():
    """The segmented control's selected colour is its one visual statement of what a
    choice does: accept roles for Import/Apply, updated for Skip/Keep, decline for
    Never. Pinned per state so a palette or mapping edit cannot silently swap them."""
    from internpearls import palette, widgets
    c = palette.colors()
    cells = {
        widgets.decision_cell([("import", "Import"), ("skip", "Skip"),
                               ("never", "Never")], "import", lambda v: None):
            (("import", "accept"), ("skip", "updated"), ("never", "decline")),
        widgets.decision_cell([("apply", "Apply"), ("keep", "Keep mine")],
                              "apply", lambda v: None):
            (("apply", "accept"), ("keep", "updated")),
    }
    for cell, mapping in cells.items():
        for state, role in mapping:
            cell.set_state(state)
            style = cell.buttons[state].styleSheet()
            assert c[f"{role}_bg"] in style and c[f"{role}_fg"] in style, (
                f"selected {state} does not carry the {role} role's colours")
            for other, b in cell.buttons.items():
                if other != state:
                    assert c[f"{role}_bg"] not in b.styleSheet(), (
                        f"unselected {other} still painted with {role}'s background")


def test_decision_cell_renders_as_one_rounded_group_not_separate_buttons():
    """The segmented control reads as a single control, not a row of square, individually
    bordered buttons: only the outer corners round, and every button after the first
    drops its own left border so the shared edge between two buttons paints once."""
    from internpearls import widgets
    options = [("import", "Import"), ("skip", "Skip"), ("never", "Never")]
    cell = widgets.decision_cell(options, "import", lambda v: None)
    buttons = [cell.buttons[v] for v, _ in options]
    first, middle, last = buttons[0], buttons[1], buttons[-1]

    assert "border-left: none" not in first.styleSheet(), (
        "the first button must keep its left border; that's the group's own left edge")
    for b in buttons[1:]:
        assert "border-left: none" in b.styleSheet(), (
            "every button after the first must suppress its own left border, or the "
            "shared edge between two buttons doubles up")

    assert "border-top-left-radius: 0px" in middle.styleSheet(), (
        "an interior button must not round any corner")
    assert "border-top-left-radius: 6px" in first.styleSheet(), (
        "the first button's left corners must round the group's own left edge")
    assert "border-top-right-radius: 6px" in last.styleSheet(), (
        "the last button's right corners must round the group's own right edge")
    assert "border-top-right-radius: 0px" in first.styleSheet(), (
        "the first button's right corners belong to the seam with its neighbour, not "
        "the group's outer edge")


def test_decision_cell_height_does_not_shift_between_selected_and_unselected():
    """A selected button's bold font-weight must not make that one button taller than
    its unselected neighbours, which would read as the row bobbing on every click."""
    from internpearls import widgets
    options = [("import", "Import"), ("skip", "Skip")]
    cell = widgets.decision_cell(options, "import", lambda v: None)
    selected, unselected = cell.buttons["import"], cell.buttons["skip"]

    def height_rule(style):
        return [line.strip() for line in style.split(";")
               if "min-height" in line or "max-height" in line]

    assert height_rule(selected.styleSheet()) == height_rule(unselected.styleSheet()), (
        "selected and unselected buttons must pin the same fixed height")


def test_section_header_returns_a_label_with_the_given_text():
    from aqt.qt import QLabel
    from internpearls import widgets
    header = widgets.section_header("Sample Section Heading")
    assert isinstance(header, QLabel)
    assert header.text() == "Sample Section Heading"


def test_simple_row_puts_its_chip_in_a_cell_ahead_of_the_primary_text():
    """The chip is its own fixed-width column, never folded into the primary label:
    inside that paragraph it would push the text right by however wide its own word
    happens to be."""
    from aqt.qt import QLabel
    from internpearls import widgets
    row = widgets.simple_row("new", "Sample row text")
    cell, primary = row._layout._children[0], row._layout._children[1]
    assert _pill(cell).text() == "NEW"
    assert isinstance(primary, QLabel) and primary.text() == "Sample row text"


def test_simple_row_with_no_chip_still_reserves_the_column():
    from internpearls import widgets
    row = widgets.simple_row(None, "Plain row text")
    cell, primary = row._layout._children[0], row._layout._children[1]
    assert _pill(cell) is None, "an unchipped row must paint no pill"
    assert primary.text() == "Plain row text"


def test_simple_row_can_decline_the_card_columns():
    """A section where nothing is chipped and nothing expands has nothing to line up
    against, so it declines the columns and starts its text at the row's own left edge.
    The end-of-run result screen is one such section, and so is the update screen's own
    per-deck summary, while the card sections below that summary are not. That is why
    this is the caller's choice rather than something read off `chip_kind` being
    None."""
    from aqt.qt import QLabel
    from internpearls import widgets
    row = widgets.simple_row(None, "Plain row text", card_columns=False)
    first = row._layout._children[0]
    assert isinstance(first, QLabel) and first.text() == "Plain row text", (
        "a row that declined the columns still reserved one ahead of its text")


def test_row_text_indent_covers_the_caret_and_the_chip_column():
    """What an expanded card body indents by. Compared against its own parts rather
    than a pixel count: the chip column is measured at the running platform's font, so
    the total is not portable but its composition is."""
    from internpearls import widgets
    assert widgets.row_text_indent() == (
        widgets.CARET_W + widgets.CARET_GAP + widgets.chip_column_width()
        + widgets.CARET_GAP)


def test_streaming_list_builds_only_its_first_batch():
    """The property the whole design rests on: opening the screen must not cost one
    widget per pending card."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)), batch=50)
    assert len(built) == 50
    assert lst.shown() == 50 and lst.total() == 500


def test_streaming_list_appends_when_scrolled_near_the_end():
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)), batch=50)
    lst._extend()          # what the scroll handler calls
    assert lst.shown() == 100 and len(built) == 100


def test_streaming_list_stops_at_the_end_and_never_overruns():
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(70)), batch=50)
    lst.fill_all()
    assert lst.shown() == 70 and len(built) == 70
    lst.fill_all()
    assert lst.shown() == 70, "filling an exhausted list must not rebuild rows"


def test_streaming_list_connects_to_the_real_scrollbar_signal():
    """The production path must wire the real vertical scrollbar's valueChanged
    signal, not merely offer _extend() for a caller to invoke by hand."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)), batch=50)
    bar = lst.verticalScrollBar()
    assert bar.valueChanged._slots, "nothing connected to the scrollbar's valueChanged"


def test_streaming_list_does_not_extend_when_the_scrollbar_sits_near_the_top():
    """Driving the real scrollbar signal, not calling _extend() by hand: this is what
    actually exercises _maybe_extend's threshold comparison. mock_anki's viewport
    always reports a height of 0, so "near the end" is exactly maximum() == value();
    leaving plenty of room below the thumb must not trigger a build."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)), batch=50)
    bar = lst.verticalScrollBar()
    bar.setMaximum(1000)
    bar.setValue(0)   # miles from the bottom
    assert lst.shown() == 50 and len(built) == 50, \
        "scrolling near the top must not build another batch"


def test_streaming_list_extends_by_one_batch_when_the_scrollbar_reaches_the_bottom():
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)), batch=50)
    bar = lst.verticalScrollBar()
    bar.setMaximum(1000)
    bar.setValue(1000)   # thumb at the very end
    assert lst.shown() == 100 and len(built) == 100, \
        "reaching the bottom must build exactly one more batch"


def _grown(lst, viewport_height, row_height):
    """Give a mock list synthetic geometry: a viewport of `viewport_height`, and rows
    whose container reports `row_height` per built row.

    mock_anki has no layout engine, so every widget reports height 0 and sizeHint 0.
    These two stand-ins are what let this suite drive _fill_viewport's own arithmetic;
    qt_tests/test_streaming.py does the same thing against real geometry.
    """
    import types
    lst.viewport().height = lambda: viewport_height
    lst._rows_container.sizeHint = lambda: types.SimpleNamespace(
        width=lambda: 0, height=lambda: lst.shown() * row_height)


def test_streaming_list_fills_a_viewport_that_outgrew_its_rows():
    """The grow-before-scroll case. Enlarging the dialog leaves the content shorter than
    the viewport, so there is nothing to scroll and valueChanged never fires again: the
    list has to notice on its own or every unbuilt row is stranded."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)),
                                batch=50)
    _grown(lst, viewport_height=1000, row_height=10)   # 50 rows = 500px, half the room
    lst._fill_viewport()
    assert lst.shown() == 150, (
        "the list must build until its rows overflow the viewport, not stop at the one "
        "batch __init__ built against a zero-height viewport")
    assert len(built) == 150


def test_streaming_list_filling_a_viewport_still_leaves_rows_unbuilt():
    """The streaming property itself: filling the viewport is not filling the list."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)),
                                batch=50)
    _grown(lst, viewport_height=300, row_height=10)
    lst._fill_viewport()
    assert lst.shown() < lst.total(), "a viewport fill must not build every row"


def test_streaming_list_filling_stops_at_the_last_item():
    """A viewport taller than the whole list must exhaust it rather than overrun."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(70)),
                                batch=50)
    _grown(lst, viewport_height=100000, row_height=10)
    lst._fill_viewport()
    assert lst.shown() == 70 and len(built) == 70


def test_streaming_list_refills_on_resize():
    """The wiring, not just the arithmetic: a resize is what tells the list its viewport
    grew. Driven through the override rather than the real Qt event, which qt_tests/
    exercises: super().resizeEvent has nothing to call under the mock."""
    from internpearls import widgets
    assert "resizeEvent" in vars(widgets.StreamingList), (
        "nothing refills the list when the dialog is enlarged")
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(500)),
                                batch=50)
    _grown(lst, viewport_height=1000, row_height=10)
    calls = []
    lst._fill_viewport = lambda: calls.append(True)
    widgets.QScrollArea.resizeEvent = lambda self, event: None   # stand in for super()
    try:
        lst.resizeEvent(None)
    finally:
        del widgets.QScrollArea.resizeEvent
    assert calls, "resizeEvent did not refill the viewport"


def test_streaming_list_repeated_bottom_scroll_after_exhaustion_does_not_rebuild_or_overrun():
    """Once every item is already shown, scrolling to the bottom again and again must
    neither rebuild rows the caller already has nor try to build past the item list."""
    from internpearls import widgets
    built = []
    lst = widgets.StreamingList(lambda item: _stub_row(built, item), list(range(70)), batch=50)
    bar = lst.verticalScrollBar()
    bar.setMaximum(1000)
    bar.setValue(1000)
    assert lst.shown() == 70 and len(built) == 70, "the second, partial batch should land"

    bar.setValue(999)   # back off from the bottom, then return: must still be a no-op
    bar.setValue(1000)
    assert lst.shown() == 70 and len(built) == 70, \
        "scrolling to the bottom of an exhausted list must not rebuild or overrun"
