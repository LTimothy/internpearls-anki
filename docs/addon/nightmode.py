"""Night mode dimming: bright images only, or every web view, while Anki's own
Night Mode is on. Never Day mode, never native Qt windows (a CSS filter cannot
reach them). The rules also need the ip-dim class, which a head script in every
web view sets from the on/off switch and the hours, so both apply live."""
from aqt import mw
from aqt.qt import QApplication
from aqt.webview import AnkiWebView

from .config import ADDON_PACKAGE, _cfg
from .logic import night_mode_css, night_mode_dim_args, night_mode_dim_script

_toggle_action = None


def _dim_args(cfg):
    return night_mode_dim_args(cfg["dim_images_night_mode"], cfg["dim_night_mode_scheduled"],
                               cfg["dim_night_mode_from"], cfg["dim_night_mode_to"])


def dim_images_in_night_mode(text, card, kind):
    """aqt.gui_hooks.card_will_show: append the image rule to every card, both
    sides, every deck. Reads config live so a percent change applies on the next
    card. In content scope the web view hook below owns the rule and this appends
    nothing, so a card is never dimmed twice."""
    cfg = _cfg()
    if cfg["dim_night_mode_scope"] != "images":
        return text
    return text + night_mode_css(cfg["dim_images_night_mode_percent"], "images")


def dim_webviews_in_night_mode(web_content, context):
    """aqt.gui_hooks.webview_will_set_content: the ip-dim script in every web
    view's head, plus in content scope the body rule (reviewer, deck list,
    overview, editor). Percent and scope show on the next load."""
    cfg = _cfg()
    web_content.head += night_mode_dim_script(_dim_args(cfg))
    if cfg["dim_night_mode_scope"] == "content":
        css = night_mode_css(cfg["dim_images_night_mode_percent"], "content")
        web_content.head += f"<style>{css}</style>"


def register_toggle_action(action):
    """Called once by __init__.py right after building the menu."""
    global _toggle_action
    _toggle_action = action
    action.setCheckable(True)
    action.setChecked(_cfg()["dim_images_night_mode"])
    # Qt flips the check before toggle_dimming runs; put it back if that bailed.
    action.triggered.connect(lambda *_: action.setChecked(_cfg()["dim_images_night_mode"]))


def apply_dimming():
    """Push the switch and hours to every open web view and the menu check."""
    cfg = _cfg()
    if _toggle_action is not None:
        _toggle_action.setChecked(cfg["dim_images_night_mode"])
    js = f"window.ipDim&&ipDim.set({_dim_args(cfg)})"
    for widget in QApplication.allWidgets():
        if isinstance(widget, AnkiWebView):
            widget.eval(js)


def toggle_dimming():
    """Experimental > Dim in Night Mode."""
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf["dim_images_night_mode"] = not _cfg()["dim_images_night_mode"]
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)
    apply_dimming()
