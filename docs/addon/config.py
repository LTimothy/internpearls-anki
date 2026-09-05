"""Constants, config access, and persistent add-on state.

Everything here is either a constant, a path, or a thin read/write over Anki's add-on
config and our own JSON state files. No dialogs, no network, no collection access.
"""
import json
import os
import tempfile

from aqt import mw

from .logic import clamp_night_mode_dim_percent

ADDON_VERSION = "0.63.0"   # MAJOR.MINOR.PATCH, see README "Versioning"
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

# Night mode image dimming, as a percentage the Experimental > Night mode dimming
# dialog exposes. Floor/ceiling mirror clamp_night_mode_dim_percent's own defaults;
# kept here too since AUTO_SYNC's floor/default/ceiling above follow the same pattern.
# 30 is not an arbitrary starting point: it's the fixed dim level this replaced (a
# flat brightness(0.7) in every build before this became configurable), so a user who
# already had the boolean on keeps today's exact appearance until they touch the slider.
NIGHT_MODE_DIM_PERCENT_FLOOR = 0
NIGHT_MODE_DIM_PERCENT_DEFAULT = 30
NIGHT_MODE_DIM_PERCENT_CEILING = 90

# "images" dims bright images only (the original behaviour); "content" dims the whole
# web view body instead. An unrecognized or missing value falls back to "images" rather
# than raising, same as every other _cfg() key.
NIGHT_MODE_SCOPE_DEFAULT = "images"

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

# The largest exact card count the wizard's Advanced panel will take, and the
# ceiling the assistant is held to when it picks the number itself. Duplicated
# from ai_logic.AUTO_COUNT_CEILING rather than imported: this module is the
# constants/config layer and stays free of ai_logic's own imports, the same
# reason the backend kinds below are not imported from ai_cli.
AI_COUNT_CEILING = 40

# Keep in sync with ai_cli.BACKENDS' keys. Not imported from there to avoid coupling
# this module (constants/config only) to ai_cli's subprocess machinery.
_AI_BACKEND_KINDS = ("claude", "codex", "agy")


def _ai_map(value):
    """ai_model/ai_effort are stored per backend kind, not as one flat value: a
    value set while one backend is active must never leak into another backend's
    argv (see ai_dialog.py's session init and ai_cli.build_argv). No shipped
    release ever wrote the old flat-string shape, so there's no migration to do,
    but tolerate one anyway by treating it as empty rather than leaking it into
    the first backend that happens to be active."""
    if not isinstance(value, dict):
        value = {}
    return {kind: value.get(kind, "") if isinstance(value.get(kind), str) else ""
            for kind in _AI_BACKEND_KINDS}


def _ai_bool_map(value, default=True):
    if not isinstance(value, dict):
        value = {}
    return {kind: bool(value.get(kind, default)) for kind in _AI_BACKEND_KINDS}


def _cli_path_map(value, preferred):
    """ai_cli_path was a single string through v0.55.1, meaning "the path for
    the preferred backend". Fold that shape into the per-backend map once, on
    read, so nobody loses a configured path on upgrade."""
    if isinstance(value, str):
        m = {kind: "" for kind in _AI_BACKEND_KINDS}
        if value and preferred in m:
            m[preferred] = value
        return m
    return _ai_map(value)


def _default_count(value):
    """ai_default_count as a number the spin box can take, or 0 for automatic.
    A hand-edited string, a negative, or anything past the ceiling reads as
    automatic rather than as a number nothing here could honour."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= AI_COUNT_CEILING else 0


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
        "dim_images_night_mode_percent": clamp_night_mode_dim_percent(
            c.get("dim_images_night_mode_percent", NIGHT_MODE_DIM_PERCENT_DEFAULT),
            NIGHT_MODE_DIM_PERCENT_FLOOR, NIGHT_MODE_DIM_PERCENT_DEFAULT,
            NIGHT_MODE_DIM_PERCENT_CEILING),
        "dim_night_mode_scope": (c.get("dim_night_mode_scope")
                                 if c.get("dim_night_mode_scope") in ("images", "content")
                                 else NIGHT_MODE_SCOPE_DEFAULT),
        "ai_backend":            c.get("ai_backend", ""),
        "ai_cli_path":           _cli_path_map(c.get("ai_cli_path"), c.get("ai_backend", "")),
        "ai_backend_enabled":    _ai_bool_map(c.get("ai_backend_enabled")),
        "ai_model":              _ai_map(c.get("ai_model")),
        "ai_effort":             _ai_map(c.get("ai_effort")),
        # Seeds for the wizard's Advanced controls, never written back by it.
        # 0 (or absent, or anything that isn't a positive number) means
        # "automatic": no count is sent and the assistant decides, up to
        # ai_logic.AUTO_COUNT_CEILING.
        "ai_default_count":      _default_count(c.get("ai_default_count")),
        "ai_default_depth":      (c.get("ai_default_depth")
                                  if c.get("ai_default_depth") in ("thorough", "quick")
                                  else "auto"),
        # Duplicate-scan pairs the reader has said aren't duplicates, keyed by
        # dupes.pair_key so a rescan never re-offers them. Config-backed, not
        # user_files: it's a preference about which pairs to show, not sync
        # bookkeeping the add-on itself needs to survive an update.
        "dupes_ignored": list(c.get("dupes_ignored", [])),
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


def add_dupes_ignored(key):
    """Add one pair_key to the duplicate-scan ignore list, saved to the add-on's own
    config (see `_cfg`'s `dupes_ignored`)."""
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    ignored = set(conf.get("dupes_ignored", []))
    ignored.add(key)
    conf["dupes_ignored"] = sorted(ignored)
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


# Rolling per-backend usage counters for AI generation: {kind: [{ts, tokens}]},
# pruned to 7 days. Also holds a "durations" key, {"<kind>-<mode>": [seconds]},
# the last 10 run durations per backend+mode (see ai_logic.record_duration),
# used to show a learned time estimate on the generation progress page.
AI_USAGE = os.path.join(_USER_FILES, "ai_usage.json")

# The raw stream from the most recent AI generation run, overwritten every run
# (never appended): the one piece of evidence a failed run leaves behind, since
# the wizard itself only ever showed the final error. See ai_cli._run_argv.
AI_LAST_RUN_LOG = os.path.join(_USER_FILES, "ai_last_run.log")


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


# The learner's own standing instructions, plain text, sent after the bundled and deck
# skills on every run. Lives in user_files so it survives add-on updates.
USER_SKILL = os.path.join(_USER_FILES, "user_skill.md")


def load_user_skill():
    try:
        with open(USER_SKILL, encoding="utf8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def save_user_skill(text):
    if not (text or "").strip():
        try:
            os.remove(USER_SKILL)
        except FileNotFoundError:
            pass
        return
    dirname = os.path.dirname(USER_SKILL) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            fh.write(text)
        os.replace(tmp, USER_SKILL)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
