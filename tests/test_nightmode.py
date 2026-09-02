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


def test_webview_hook_injects_body_rule_only_in_content_scope(anki):
    from internpearls import nightmode
    wc = types.SimpleNamespace(head="", body="", css=[], js=[])
    _cfg_with(anki, dim_night_mode_scope="content")
    nightmode.dim_webviews_in_night_mode(wc, None)
    assert "<style>" in wc.head and "body.nightMode" in wc.head
    wc2 = types.SimpleNamespace(head="", body="", css=[], js=[])
    _cfg_with(anki, dim_night_mode_scope="images")
    nightmode.dim_webviews_in_night_mode(wc2, None)
    assert wc2.head == ""


def test_scope_config_defaults_and_clamps(anki):
    from internpearls.config import _cfg
    anki.mw._config = {}
    assert _cfg()["dim_night_mode_scope"] == "images"
    anki.mw._config = {"dim_night_mode_scope": "nonsense"}
    assert _cfg()["dim_night_mode_scope"] == "images"
    anki.mw._config = {"dim_night_mode_scope": "content"}
    assert _cfg()["dim_night_mode_scope"] == "content"


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
