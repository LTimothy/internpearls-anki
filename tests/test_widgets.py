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


def test_chip_html_carries_a_foreground_with_its_background():
    """The v0.32.1 rule, in markup rather than a stylesheet, so the setStyleSheet lint
    cannot see it. Matched on "; color:" because "background-color:" contains "color:"."""
    from internpearls import widgets
    for kind in widgets.CHIPS:
        markup = widgets.chip_html(kind)
        span = markup[markup.index("background-color"):]
        span = span[:span.index(">")]
        assert "; color:" in span, f"{kind} chip sets a background with no foreground"


def test_chip_html_is_empty_for_an_unknown_kind():
    from internpearls import widgets
    assert widgets.chip_html(None) == ""
    assert widgets.chip_html("nonsense") == ""


def test_chip_html_carries_every_kinds_label():
    from internpearls import widgets
    for kind, label in widgets.CHIPS.items():
        assert label in widgets.chip_html(kind)


def test_section_header_returns_a_label_with_the_given_text():
    from aqt.qt import QLabel
    from internpearls import widgets
    header = widgets.section_header("Sample Section Heading")
    assert isinstance(header, QLabel)
    assert header.text() == "Sample Section Heading"


def test_simple_row_carries_its_chip_inside_the_same_paragraph_as_the_primary_text():
    """Beside it as its own widget would start each row's text at a different x
    depending on whether a chip is present, the same defect review._row_html avoids."""
    from aqt.qt import QLabel
    from internpearls import widgets
    row = widgets.simple_row("new", "Sample row text")
    labels = [c for c in row._layout._children if isinstance(c, QLabel)]
    assert len(labels) == 1
    assert "NEW" in labels[0].text()
    assert "Sample row text" in labels[0].text()


def test_simple_row_with_no_chip_omits_the_marker():
    from internpearls import widgets
    row = widgets.simple_row(None, "Plain row text")
    label = row._layout._children[0]
    assert "NEW" not in label.text()
    assert "Plain row text" in label.text()


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
