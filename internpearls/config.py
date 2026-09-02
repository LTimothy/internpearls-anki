"""Constants, config access, and persistent add-on state.

Everything here is either a constant, a path, or a thin read/write over Anki's add-on
config and our own JSON state files. No dialogs, no network, no collection access.
"""
import json
import os
import tempfile

from aqt import mw

ADDON_VERSION = "0.53.3"   # MAJOR.MINOR.PATCH, see README "Versioning"
# Highest manifest.json `schema` value this add-on version knows how to read. The
# deck-repo side bumps its manifest `schema` only for a breaking shape change (see its
# own notes); when it does, an add-on release that understands the new shape must bump
# this in lockstep BEFORE that manifest ships, so an older, still-installed add-on
# refuses to sync against it instead of guessing. Today's manifest schema is 3, and this
# add-on release understands it; bump this again in lockstep the next time the deck-repo
# side bumps schema.
SUPPORTED_MANIFEST_SCHEMA = 3
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
# Notes she has written about new cards, saved as she types rather than only when the
# review dialog closes. Feedback is the one thing in a run that clicking Update again
# cannot reproduce, and it used to live only in memory until the digest at the very end,
# so anything that ended a run early (a crash, a force quit, an error mid-import) threw
# it away silently. {guid: {note, deck, front}}; cleared once the digest has been shown.
FEEDBACK = os.path.join(_USER_FILES, "card_feedback.json")
# What the deck source last shipped for each preserved field, so _restore can tell
# "she edited this" from "the deck author changed this" instead of freezing the
# field forever. {guid: {field: value}}. See collection._restore.
SHIPPED = os.path.join(_USER_FILES, "shipped_fields.json")
# The declined-card registry: {guid: {state, front, deck, decided, hash}}.
# Lives in user_files/ like the feedback log and for the same reason: it must
# survive add-on updates and reinstalls.
DECLINED = os.path.join(_USER_FILES, "declined.json")

AUTO_SYNC_INTERVAL_FLOOR_MIN = 1     # refuse to poll more often than this, however configured
AUTO_SYNC_INTERVAL_DEFAULT_MIN = 15  # used when the setting is missing or unreadable
# And a ceiling, for the same reason the floor exists. The poll interval becomes a
# QTimer interval in milliseconds, which is a C int: a hand-edited 99999999 overflows it
# and every launch opens Anki's raw add-on error dialog instead of starting the timer.
# A week is far past any useful cadence and leaves the millisecond value an order of
# magnitude inside the limit.
AUTO_SYNC_INTERVAL_CEILING_MIN = 7 * 24 * 60

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
        "ai_backend":            c.get("ai_backend", ""),
        "ai_cli_path":           c.get("ai_cli_path", ""),
    }


def _load_json(path, default, strict=False):
    """Read a JSON file, falling back to `default` when it isn't there.

    `strict` re-raises a parse error instead of swallowing it, so a caller that already
    knows the file exists (a configured deck source's manifest.json) can tell a corrupt
    file from an absent one and say which. An absent file still returns `default` even
    under strict. It is off by default, so every existing caller behaves exactly as it
    did: a missing or unreadable installed.json still reads as "nothing installed yet".
    """
    try:
        with open(path, encoding="utf8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        if strict:
            raise
        return default


def _save_json(path, data):
    """Write JSON atomically: serialize into a temp file in the same directory, then
    os.replace it over the target.

    A plain open(path, "w") truncates before writing, so anything that interrupts the
    write (a crash, a full disk, Anki being force-quit mid-sync) leaves a truncated or
    empty file where installed.json or the feedback log used to be. os.replace is
    atomic on every platform Anki runs on, so a reader only ever sees the old file or
    the complete new one.

    The target's directory is created first: mkstemp raises FileNotFoundError outright
    if it's missing, which would turn a single absent directory into a crash on every
    state write this add-on makes.
    """
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_declined():
    """The declined-card registry: {guid: {state, front, deck, decided, hash}}.
    Lives in user_files/ like the feedback log and for the same reason: it must
    survive add-on updates and reinstalls.

    A whole file that parsed as valid JSON but isn't an object (a hand-edited array or
    bare string) reads as an empty registry, exactly as review.load_saved_feedback
    treats its own file. Without this the sync's own `set(...)` over it succeeds, the
    imports run, and the first reader that calls .items() raises after the collection
    has already been written."""
    reg = _load_json(DECLINED, {})
    return reg if isinstance(reg, dict) else {}


def save_declined(reg):
    _save_json(DECLINED, reg)


# Rolling per-backend usage counters for AI generation: {kind: [{ts, tokens}]},
# pruned to 7 days. Also holds a "durations" key, {"<kind>-<mode>": [seconds]},
# the last 10 run durations per backend+mode (see ai_logic.record_duration),
# used to show a learned time estimate on the generation progress page.
AI_USAGE = os.path.join(_USER_FILES, "ai_usage.json")


def load_ai_usage():
    reg = _load_json(AI_USAGE, {})
    return reg if isinstance(reg, dict) else {}


def save_ai_usage(reg):
    _save_json(AI_USAGE, reg)


# The consented deck-source skill: {text, version, hash, consented_on,
# enabled}. Content is only ever written here after explicit consent.
DECK_SKILL = os.path.join(_USER_FILES, "deck_skill.json")


def load_deck_skill():
    d = _load_json(DECK_SKILL, None)
    return d if isinstance(d, dict) else None


def save_deck_skill(d):
    _save_json(DECK_SKILL, d)
