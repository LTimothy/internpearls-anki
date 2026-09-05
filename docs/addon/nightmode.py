"""Night mode dimming: bright images only, or every web view, while Anki's own
Night Mode is on. Never Day mode, never native Qt windows (a CSS filter cannot
reach them)."""
from .config import _cfg
from .logic import night_mode_css


def dim_images_in_night_mode(text, card, kind):
    """aqt.gui_hooks.card_will_show: append the image rule to every card, both
    sides, every deck. Reads config live so a change applies on the next card.
    In content scope the web view hook below owns the rule and this appends
    nothing, so a card is never dimmed twice."""
    cfg = _cfg()
    if cfg["dim_night_mode_scope"] != "images":
        return text
    return text + night_mode_css(cfg["dim_images_night_mode"],
                                 cfg["dim_images_night_mode_percent"], "images")


def dim_webviews_in_night_mode(web_content, context):
    """aqt.gui_hooks.webview_will_set_content: in content scope, put the body
    rule in every web view's head (reviewer, deck list, overview, editor).
    Fires when a screen loads, so a changed setting shows on the next load."""
    cfg = _cfg()
    if cfg["dim_night_mode_scope"] != "content":
        return
    css = night_mode_css(cfg["dim_images_night_mode"],
                         cfg["dim_images_night_mode_percent"], "content")
    if css:
        web_content.head += f"<style>{css}</style>"
