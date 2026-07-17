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
    assert s.dialog.windowTitle(), f"scene {scene!r} opened a dialog with no title"
