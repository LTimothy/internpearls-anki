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

from .config import ADDON_VERSION, ANKI_REPO
from .logic import version_at_least
from .net import _BG_TIMEOUT, _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, _gh_public_raw
from .ui import _ask, _info, _safe, _warn


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
