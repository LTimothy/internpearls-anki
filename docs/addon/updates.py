"""Add-on self-update: version fetch, package download, and the manual check.

The background once-per-launch check lives in background.py; the fetch/download
helpers here are its single source of truth too, so the manual and background paths
can't drift apart.
"""
import json
import os
import tempfile

from aqt import mw
from aqt.utils import openLink

from .config import ADDON_VERSION, ANKI_REPO, STATE, _load_json
from .logic import version_at_least
from .net import _BG_TIMEOUT, _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, _gh_public_raw
from .ui import _ask, _info, _safe, _warn

# The "Check for add-on updates" QAction, set once by __init__.py right after building
# the menu. Mutated from here and from background.py's startup check so a known
# update stays visible on the menu item itself after the startup tooltip's 8 seconds
# pass — previously an update notice had nowhere to live once that tooltip faded short
# of digging into Advanced, which is the "hidden" problem this exists to fix.
_update_action = None


def register_update_action(action):
    """Called once by __init__.py right after building the menu. Also seeds the label
    from whatever version state.json last recorded an update-check learning about, so a
    pending update discovered on a previous launch is visible immediately on this one,
    before this session's own background or manual check has even run.
    """
    global _update_action
    _update_action = action
    cached = _load_json(STATE, {}).get("last_notified_addon_version")
    if cached:
        _refresh_update_action_label(cached)


def _refresh_update_action_label(latest):
    """Show the known-latest version on the menu item itself, or reset to the plain
    label once we're caught up to it. No-op before the menu exists (register_update_action
    hasn't run yet) — callable safely from anywhere that learns a version number.
    """
    if _update_action is None:
        return
    if latest and not version_at_least(ADDON_VERSION, latest):
        _update_action.setText(f"Check for add-on updates (v{latest} available)")
    else:
        _update_action.setText("Check for add-on updates")


def _fetch_addon_version_info(timeout=_CONNECT_TIMEOUT):
    """Fetch and parse the public add-on repo's version.json via the Contents API (see
    _gh_public_raw for why not the raw CDN link). Raises on any failure.

    Single source of truth for this fetch so the manual "Check for add-on updates"
    action and the background checks can't drift apart.
    """
    return json.loads(_gh_public_raw("version.json", timeout=timeout))


def _download_addon_package(timeout=_DOWNLOAD_TIMEOUT):
    """Download the current .ankiaddon package to a temp file and return its path."""
    data = _gh_public_raw("internpearls.ankiaddon", timeout=timeout)
    path = os.path.join(tempfile.gettempdir(), "internpearls.ankiaddon")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _addon_update_work(auto_update):
    """Background-safe: fetch the public repo's version info, and if `auto_update` is on
    and a newer version exists, also download the package. No Qt or mw.col access, so
    it's safe to run off the main thread. Raises on a fetch failure; the caller decides
    what "stay quiet" means for that.
    """
    info = _fetch_addon_version_info(timeout=_BG_TIMEOUT)
    package_path = None
    latest = info.get("version", "")
    if auto_update and latest and not version_at_least(ADDON_VERSION, latest):
        package_path = _download_addon_package(timeout=_DOWNLOAD_TIMEOUT)
    return {"info": info, "package_path": package_path}


@_safe
def check_updates():
    """Compare our version to version.json in the public add-on repo; offer to update."""
    try:
        latest = _fetch_addon_version_info()
    except Exception as e:
        _warn(f"Couldn't check for updates: {e}")
        return
    _refresh_update_action_label(latest.get("version", ""))

    if version_at_least(ADDON_VERSION, latest.get("version", "0")):
        _info(f"Intern Pearls Deck Tools is up to date (v{ADDON_VERSION}).")
        return
    if not _ask(f"Update available: v{latest['version']} "
                f"(you have v{ADDON_VERSION}). Download and install now?"):
        return
    try:
        mw.addonManager.install(_download_addon_package())
        _info("Updated. Please restart Anki.")
    except Exception as e:
        _warn(f"Auto-install failed ({e}).<br>Opening the download page instead.")
        openLink(f"https://github.com/{ANKI_REPO}")
