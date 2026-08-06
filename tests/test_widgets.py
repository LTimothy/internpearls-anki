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
