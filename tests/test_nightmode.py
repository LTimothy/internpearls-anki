"""Tests for internpearls/nightmode.py: which hook owns the dimming rule in each
scope, and that both hooks are actually registered on import.
"""
import types


def _cfg_with(anki, **over):
    base = {"dim_images_night_mode": True, "dim_images_night_mode_percent": 50,
           "dim_night_mode_scope": "images"}
    base.update(over)
    anki.mw._config = base


def test_card_hook_appends_image_rule_in_images_scope(anki):
    from internpearls import nightmode
    _cfg_with(anki, dim_night_mode_scope="images")
    out = nightmode.dim_images_in_night_mode("<b>q</b>", None, "reviewQuestion")
    assert out.startswith("<b>q</b>")
    assert "img" in out and "body.nightMode" not in out


def test_card_hook_appends_nothing_in_content_scope(anki):
    from internpearls import nightmode
    _cfg_with(anki, dim_night_mode_scope="content")
    assert nightmode.dim_images_in_night_mode("<b>q</b>", None, "reviewQuestion") == "<b>q</b>"


def test_webview_hook_injects_root_rule_only_in_content_scope(anki):
    from internpearls import nightmode
    wc = types.SimpleNamespace(head="", body="", css=[], js=[])
    _cfg_with(anki, dim_night_mode_scope="content")
    nightmode.dim_webviews_in_night_mode(wc, None)
    assert "<style>" in wc.head and "html.night-mode.ip-dim" in wc.head
    wc2 = types.SimpleNamespace(head="", body="", css=[], js=[])
    _cfg_with(anki, dim_night_mode_scope="images")
    nightmode.dim_webviews_in_night_mode(wc2, None)
    assert "<style>" not in wc2.head


def test_webview_hook_always_injects_the_switch_script(anki):
    """Even switched off, so the menu switch can turn an open screen on."""
    from internpearls import nightmode
    _cfg_with(anki, dim_images_night_mode=False, dim_night_mode_scheduled=True,
              dim_night_mode_from=22, dim_night_mode_to=6)
    wc = types.SimpleNamespace(head="", body="", css=[], js=[])
    nightmode.dim_webviews_in_night_mode(wc, None)
    assert "ipDim.set(false,true,22,6)" in wc.head


def test_card_hook_appends_rule_even_when_switched_off(anki):
    from internpearls import nightmode
    _cfg_with(anki, dim_images_night_mode=False)
    assert "html.ip-dim" in nightmode.dim_images_in_night_mode("q", None, "reviewQuestion")


def test_toggle_flips_config_menu_check_and_open_web_views(anki):
    from aqt.qt import QAction
    from aqt.webview import AnkiWebView
    from internpearls import nightmode
    _cfg_with(anki, dim_images_night_mode=False)
    action = QAction("On")
    nightmode.register_toggle_action(action)
    assert action.isChecked() is False
    view = AnkiWebView()
    nightmode.toggle_dimming()
    assert anki.mw._config["dim_images_night_mode"] is True
    assert action.isChecked() is True
    assert view.evals[-1] == "window.ipDim&&ipDim.set(true,false,21,7)"
    nightmode.toggle_dimming()
    assert anki.mw._config["dim_images_night_mode"] is False
    assert action.isChecked() is False
    assert view.evals[-1].startswith("window.ipDim&&ipDim.set(false,")


def test_scope_config_defaults_and_clamps(anki):
    from internpearls.config import _cfg
    anki.mw._config = {}
    assert _cfg()["dim_night_mode_scope"] == "images"
    anki.mw._config = {"dim_night_mode_scope": "nonsense"}
    assert _cfg()["dim_night_mode_scope"] == "images"
    anki.mw._config = {"dim_night_mode_scope": "content"}
    assert _cfg()["dim_night_mode_scope"] == "content"


def test_schedule_config_defaults_and_clamps(anki):
    from internpearls.config import _cfg
    anki.mw._config = {}
    cfg = _cfg()
    assert (cfg["dim_night_mode_scheduled"], cfg["dim_night_mode_from"],
            cfg["dim_night_mode_to"]) == (False, 21, 7)
    anki.mw._config = {"dim_night_mode_from": 30, "dim_night_mode_to": "x"}
    assert (_cfg()["dim_night_mode_from"], _cfg()["dim_night_mode_to"]) == (21, 7)


def test_both_hooks_are_registered():
    """conftest.py deliberately never executes internpearls/__init__.py (see its
    module docstring), so importing internpearls here would only return the bare
    namespace package conftest.py installed, proving nothing. Reads the source
    instead, the same workaround test_sync_flows.py's _StubAction exists for."""
    import os
    import internpearls
    init_path = os.path.join(internpearls.__path__[0], "__init__.py")
    src = open(init_path, encoding="utf8").read()
    assert "gui_hooks.card_will_show.append(dim_images_in_night_mode)" in src
    assert "gui_hooks.webview_will_set_content.append(dim_webviews_in_night_mode)" in src
    assert "setConfigUpdatedAction(ADDON_PACKAGE, lambda _conf: apply_dimming())" in src


def test_menu_check_reverts_when_the_toggle_never_ran(anki):
    """Qt flips a checkable action before its handler; if the handler bails (the
    restart guard after an add-on update), the check must not lie."""
    from aqt.qt import QAction
    from internpearls import nightmode
    _cfg_with(anki, dim_images_night_mode=False)
    action = QAction("On")
    nightmode.register_toggle_action(action)
    action.setChecked(True)     # what Qt does before emitting triggered
    action.triggered.emit(True)
    assert action.isChecked() is False
