"""The v0.62.0 fix for a drawn SVG's review thumbnail: a viewBox-only root used to
rasterize with a white square in one corner and the drawing spilling past it, since
Qt's SVG image plugin sizes a root with no absolute width/height against a default
viewport rather than the viewBox. review._svg_thumbnail rasterizes with QSvgRenderer
directly instead, painting its own white background at the viewBox's own size.
"""
import internpearls.review as review
from PyQt6.QtGui import QImage


def test_svg_thumbnail_has_no_white_square_on_a_viewbox_only_svg(tmp_path):
    # No width/height on the root, only a viewBox: exactly the shape that used to
    # rasterize wrong. A filled rect covers the whole viewBox in solid red.
    svg = tmp_path / "drawn.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        '<rect x="0" y="0" width="200" height="200" fill="red"/>'
        '</svg>', encoding="utf8")
    png_path = review._svg_thumbnail(str(svg))
    assert png_path, "QtSvg is available in this venv; the thumbnail must render"
    image = QImage(png_path)
    assert not image.isNull()
    # The drawing fills the whole viewBox, so a point well inside it and the
    # top-left corner must read the same fill, not a white square in the corner.
    inside = image.pixelColor(image.width() // 2, image.height() // 2)
    corner = image.pixelColor(1, 1)
    assert inside.red() > 200 and inside.green() < 60, "the red fill did not paint"
    assert corner.red() > 200 and corner.green() < 60, (
        "the corner is not the drawing's own fill: a mis-sized background rect "
        "left a white square there")


def test_svg_thumbnail_paints_its_own_white_background(tmp_path):
    # A drawing that only covers part of the viewBox: the untouched area must be
    # the add-on's own white fill, not left transparent (which would show as
    # whatever the review row's own background is, including in Night Mode).
    svg = tmp_path / "partial.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        '<circle cx="100" cy="100" r="20" fill="blue"/>'
        '</svg>', encoding="utf8")
    png_path = review._svg_thumbnail(str(svg))
    assert png_path
    image = QImage(png_path)
    corner = image.pixelColor(2, 2)
    assert (corner.red(), corner.green(), corner.blue()) == (255, 255, 255)
    assert corner.alpha() == 255


def test_svg_thumbnail_returns_none_for_a_non_svg_file(tmp_path):
    not_svg = tmp_path / "junk.svg"
    not_svg.write_text("not svg at all", encoding="utf8")
    assert review._svg_thumbnail(str(not_svg)) is None


def test_image_tag_uses_the_rasterized_thumbnail_for_an_svg(tmp_path):
    svg = tmp_path / "drawn.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 50">'
        '<rect width="50" height="50" fill="green"/></svg>', encoding="utf8")
    tag = review._image_tag(str(svg))
    assert tag is not None
    assert str(svg) + ".png" in tag
