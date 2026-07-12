"""Dims bright white-background images across every deck while Anki's Night Mode
is on.
"""
from .config import _cfg
from .logic import night_mode_image_css


def dim_images_in_night_mode(text, card, kind):
    """aqt.gui_hooks.card_will_show callback: appends the dimming CSS (if the
    Settings toggle is on) to every card's rendered HTML, question and answer
    side, across every deck and note type. Reads the config live on each call,
    so flipping the Settings checkbox takes effect on the very next card shown.
    """
    return text + night_mode_image_css(_cfg()["dim_images_night_mode"])
