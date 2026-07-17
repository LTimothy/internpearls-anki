"""The suite's own foundation: that these tests are testing what they claim to be.

Every other file here assumes the add-on's modules hold real PyQt6 widgets. If that
assumption ever silently breaks, every assertion in this directory would keep passing
against mock widgets and prove nothing. So it gets asserted rather than assumed.
"""
import pytest

import harness

ALL_SCENES = sorted(harness.SCENES)


def test_addon_modules_bind_real_pyqt6():
    """The guard against the failure this whole directory exists to avoid: mock Qt
    winning the import race and every paint assertion passing on a fake widget."""
    import internpearls.review as review
    import internpearls.ui as ui
    assert "PyQt6" in ui.QLabel.__module__, (
        f"internpearls.ui holds {ui.QLabel.__module__}, not PyQt6. The mock Qt won "
        "the import race; see harness.py's docstring.")
    assert "PyQt6" in review.QLabel.__module__, review.QLabel.__module__


@pytest.mark.parametrize("scene", ALL_SCENES)
def test_every_scene_renders_something(shot, scene):
    s = shot(scene)
    assert not s.image.isNull(), f"scene {scene!r} grabbed a null image"
    assert s.image.width() > 0 and s.image.height() > 0
    # The branding check applies only to QDialog scenes. QMessageBox does not
    # round-trip setWindowTitle through windowTitle() (it manages its own title, and
    # on macOS an alert dialog gets no title bar at all), so windowTitle() reads back
    # empty even though about() and configure_source() set it correctly. Those two are
    # QMessageBox scenes; they still render real content, which the image checks above
    # prove non-vacuously (their whole message is one rich-text QLabel the contrast and
    # layout sweeps measure).
    _, q = harness.bootstrap()
    if not isinstance(s.dialog, q.QMessageBox):
        assert s.dialog.windowTitle(), f"scene {scene!r} opened a dialog with no title"
