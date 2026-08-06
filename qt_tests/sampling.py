"""Read colour and geometry back out of a render.

Everything here must be platform-neutral. CI runs ubuntu with Qt 6.10 from pip;
development runs macOS with the Qt 6.9 Anki bundles. Font metrics differ between them,
so a pixel count or a widget width is not portable. Assert presence and relationships
(this colour appears; these edges align; this ratio clears 4.5), never magnitudes.

Renders are pixel-deterministic within one machine, which is tempting and a trap: it
makes an exact-magnitude assertion look reliable right up until CI runs it.
"""
from collections import Counter

import harness


def _channel(value):
    v = value / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(colour):
    """WCAG relative luminance of a QColor, 0.0 (black) to 1.0 (white)."""
    return (0.2126 * _channel(colour.red())
            + 0.7152 * _channel(colour.green())
            + 0.0722 * _channel(colour.blue()))


def contrast_ratio(a, b):
    """WCAG contrast ratio between two QColors: 1.0 identical, 21.0 black on white.

    WCAG AA wants 4.5 for body text. That is the number this suite enforces.
    """
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def colour_counts(image, rect=None):
    """Counter of {"#rrggbb": pixel count} over the image, or just within rect."""
    if rect is None:
        rect = image.rect()
    counts = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            counts[image.pixelColor(x, y).name()] += 1
    return counts


def widget_rect(dialog, widget):
    """The widget's rect in the dialog's own coordinates, clipped to the dialog and to
    every ancestor QScrollArea's own viewport along the way.

    mapTo is pure frame-to-frame addition: it knows nothing about clipping, so a widget
    scrolled out of a QScrollArea's viewport (a row below the fold in a long list) still
    reports a rect that can fall inside the dialog's own outer bounds. Without the
    second clip, that rect reads as present-but-blank instead of correctly reporting
    empty, which is indistinguishable from a real dropped declaration to a caller like
    test_declared.py.
    """
    _, q = harness.bootstrap()
    top_left = widget.mapTo(dialog, q.QPoint(0, 0))
    rect = q.QRect(top_left, widget.size()).intersected(dialog.rect())
    parent = widget.parentWidget()
    while parent is not None and parent is not dialog:
        if isinstance(parent, q.QScrollArea):
            viewport = parent.viewport()
            vp_top_left = viewport.mapTo(dialog, q.QPoint(0, 0))
            rect = rect.intersected(q.QRect(vp_top_left, viewport.size()))
        parent = parent.parentWidget()
    return rect


def text_contrast(shot, widget, sample=12):
    """(ratio, foreground, background) for a widget's text against what is really behind
    it, or None when the widget is too small or too plain to read.

    Works on any widget that carries text over its own background: a QLabel, or a flat
    QPushButton (the caret, the link-style buttons). Not native buttons: their offscreen
    rendering is platform chrome, not the add-on's own colours, so it is a false-positive
    source. The caller decides which widgets to pass.

    Background is the widget rect's most common pixel. Foreground is whichever of the
    `sample` most common pixels contrasts that background most, which lands on the glyph
    core rather than an antialiased edge: antialiasing only ever produces colours
    between the glyph and its background, so the extreme is always real paint. Taking
    the extreme is also why this returns one colour rather than every distinct one.

    A consequence worth stating plainly: a widget carrying two colours reports only the
    stronger. So the review row's dim tag lead-in, and a cloze deletion inline in a
    sentence, are NOT measured at those sites. Both colours are still in the ledger and
    still substantiated, because each also paints alone somewhere the suite does look:
    dim is the whole of a changed field's "was" line, and the accent is what a flat
    link-style button carries. Those uses are measured. Inline-span contrast within a
    mixed label remains uncovered by this suite.
    """
    _, q = harness.bootstrap()
    # QLabel and QPushButton say text(); QPlainTextEdit (the feedback digest, which
    # carries its own background and so is exactly the shape of widget this suite
    # exists for) says toPlainText().
    read = getattr(widget, "text", None) or getattr(widget, "toPlainText", None)
    if not widget.isVisible() or not read or not read().strip():
        return None
    rect = widget_rect(shot.dialog, widget)
    if rect.width() < 2 or rect.height() < 2:
        return None
    counts = colour_counts(shot.image, rect)
    if len(counts) < 2:
        return None
    background = q.QColor(counts.most_common(1)[0][0])
    foreground = max((q.QColor(name) for name, _ in counts.most_common(sample)),
                     key=lambda c: contrast_ratio(c, background))
    return contrast_ratio(foreground, background), foreground, background
