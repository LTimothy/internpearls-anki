"""Constants, config access, and persistent add-on state.

Everything here is either a constant, a path, or a thin read/write over Anki's add-on
config and our own JSON state files. No dialogs, no network, no collection access.
"""
import json
import os

from aqt import mw

ADDON_VERSION = "0.32.0"   # MAJOR.MINOR.PATCH, see README "Versioning"
# Highest manifest.json `schema` value this add-on version knows how to read. The
# deck-repo side bumps its manifest `schema` only for a breaking shape change (see its
# CLAUDE.md); when it does, an add-on release that understands the new shape must bump
# this in lockstep BEFORE that manifest ships, so an older, still-installed add-on
# refuses to sync against it instead of guessing. Today's manifest schema is 2, and has
# been since before this constant existed, so 2 is also the correct floor for every
# earlier add-on release — this only starts mattering the next time schema goes to 3.
SUPPORTED_MANIFEST_SCHEMA = 2
ANKI_REPO = "LTimothy/internpearls-anki"   # public add-on repo (used for self-update)
APP_NAME = "Intern Pearls"   # every dialog's title bar, so it never just says "Anki"
EXPORT_DECK = "Intern Pearls::Intern Custom"   # the deck Export Intern Pearls deck scopes to
DECK_BACKUPS_KEEP = 10   # how many automatic Intern Pearls deck backups to retain
# Where "Reconcile my decks" archives retired cards, and the tag that marks a card as
# already archived (so a re-run doesn't touch it again). The deck leaf is appended under
# the configured export_deck root; the tag leaf under the configured scope_tag.
RETIRED_DECK_LEAF = "Retired"
RETIRED_TAG_LEAF = "retired"
# "Clean up duplicate cards" archives the losing copy of a sync duplicate to the same
# Retired deck as above, but under its own tag leaf, so the two kinds of archive stay
# distinguishable and a duplicate-cleanup re-run can tell what it already handled.
DUPLICATE_TAG_LEAF = "retired-duplicate"
_DIR = os.path.dirname(__file__)

EXAMPLE_REPO = "LTimothy/internpearls-example-deck"   # public demo deck source
EXAMPLE_SCOPE_TAG = "ExampleDeck"                     # the example deck's base_tag
# The parent deck: the example repo ships more than one deck under it, and a deck
# export scoped to the parent includes the children, so backups cover them all.
EXAMPLE_DECK_NAME = "Example Decks"

# Anki's add-on manager wipes and re-extracts everything in this folder on every add-on
# update, except a "user_files" subfolder, which it explicitly backs up and restores
# around the reinstall. Our own sync state has to live there or every add-on update
# resets it, making Sync think every deck is new again.
_USER_FILES = os.path.join(_DIR, "user_files")
os.makedirs(_USER_FILES, exist_ok=True)
INSTALLED = os.path.join(_USER_FILES, "installed.json")
# Small add-on state that isn't a user setting (so it doesn't belong in config.json) but
# must still survive an add-on update — currently just which add-on version we've already
# nagged about, so the startup notice fires once per release, not every launch.
STATE = os.path.join(_USER_FILES, "state.json")

AUTO_SYNC_INTERVAL_FLOOR_MIN = 1     # refuse to poll more often than this, however configured
AUTO_SYNC_INTERVAL_DEFAULT_MIN = 15  # used when the setting is missing or unreadable

# One-time migration: earlier versions wrote this next to __init__.py, so an add-on
# update would have already wiped it. Move it over if it's still there from a
# same-version reinstall.
for _name in ("installed.json",):
    _old, _new = os.path.join(_DIR, _name), os.path.join(_USER_FILES, _name)
    if os.path.exists(_old) and not os.path.exists(_new):
        try:
            os.rename(_old, _new)
        except OSError:
            pass

TARGET_FIELDS = {
    "Study Deck - Basic":    ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"],
    "Study Deck - Cloze":    ["Text", "Why", "Image", "Dosing", "Notes"],
    "Study Deck - Image ID": ["Image", "Prompt", "Answer", "Why", "Notes"],
}

# Anki resolves a submodule's __name__ to the add-on's config by its top-level package
# name, so passing this to getConfig/writeConfig from any module here reads and writes
# the same config.json.
ADDON_PACKAGE = __name__.split(".")[0]


def _cfg():
    c = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    return {
        "protected":   c.get("protected_fields", ["Notes"]),
        "scope_tag":   c.get("scope_tag", "InternPearls"),
        "decks_dir":   c.get("decks_dir", ""),
        "gh_repo":     c.get("github_decks_repo", ""),
        "gh_ref":      c.get("github_ref", "main"),
        "gh_token":    c.get("github_token", ""),
        "export_deck": c.get("export_deck", EXPORT_DECK),
        "excluded":    c.get("excluded_decks", []),
        "notify_addon_updates": c.get("notify_addon_updates", True),
        "auto_update_addon":    c.get("auto_update_addon", False),
        "auto_sync_decks":      c.get("auto_sync_decks", False),
        "auto_sync_interval_minutes": c.get("auto_sync_interval_minutes",
                                            AUTO_SYNC_INTERVAL_DEFAULT_MIN),
        "dim_images_night_mode": c.get("dim_images_night_mode", False),
        "collect_feedback": c.get("collect_card_feedback", False),
    }


def _load_json(path, default):
    try:
        with open(path, encoding="utf8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, indent=2)
