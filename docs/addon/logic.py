"""Pure logic for Intern Pearls Deck Tools.

Nothing here imports aqt or anki, so it's testable with plain pytest, no Anki
environment needed. If a function starts needing mw/col, it belongs in __init__.py
instead, not here.
"""
import contextlib
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import unicodedata
import zipfile

from .ai_logic import is_generated_guid
from .dupes import _GREEK

FS = "\x1f"   # Anki's field separator inside a note's flds column

NEWER_APKG_ERROR = ('This .apkg uses Anki\'s newer export format. Re-export it with '
                    '"Support older Anki versions" ticked and try again.')


def plural(count, noun):
    """A count and its noun, agreeing: "1 card", "3 cards", "0 cards".

    Zero takes the plural, the way English does, so a line reads "restored on 0 cards"
    rather than "0 card". Every noun this add-on counts (card, deck, note, review,
    minute) pluralizes with a plain "s", so there is no irregular form to pass in; a
    string needing one can take it up then rather than now.

    Lives here, in the module with no Qt in it, so the wording every screen shares is
    testable without one.
    """
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def clamp_night_mode_dim_percent(percent, floor_percent=0, default_percent=30,
                                 ceiling_percent=90):
    """Sanitize a configured/typed night-mode image-dim percentage: a missing or
    non-numeric value falls back to `default_percent`; anything below `floor_percent`
    (a negative brightening, or 0) is raised to the floor, and anything above
    `ceiling_percent` is lowered to the ceiling: past that point an image reads as
    blacked out rather than dimmed, which defeats the point of dimming it at all.
    Mirrors clamp_interval_minutes's shape above.

    `int()` raises OverflowError rather than ValueError on a non-finite float
    (`float("inf")`, which Python's own `json` module happily parses from a
    hand-edited config despite that not being valid JSON), so that has to be caught
    right alongside the more obvious bad-input cases or a stray Infinity in
    config.json would raise out of every call to _cfg()."""
    try:
        p = int(percent)
    except (TypeError, ValueError, OverflowError):
        p = default_percent
    return min(ceiling_percent, max(floor_percent, p))


def night_mode_dim_factor(percent):
    """The 0..1 brightness multiplier night_mode_image_css's filter applies for a given
    dim percent: 0 -> 1.0 (unchanged), the default 30 -> 0.7. Re-clamps percent the same
    way night_mode_image_css does, so a caller passing a raw config value gets the same
    factor the CSS would use for it.

    Pulled out to its own function so the Experimental > Night mode dimming preview can
    call the exact same arithmetic the real CSS rule uses, rather than reimplementing
    it: two copies of "1 - percent/100" would be free to drift apart the next time
    either one changes.
    """
    percent = clamp_night_mode_dim_percent(percent)
    return round(1 - percent / 100, 2)


def _night_mode_image_rule(factor):
    """The bright-image dimming rule itself, given an already-computed brightness
    factor. Dims rather than inverts, since a full color invert looks wrong on a real
    photo mixed into an otherwise diagram-heavy deck."""
    return ("<style>.nightMode img {{ filter: brightness({b:g}) contrast(0.92); "
           "}}</style>").format(b=factor)


NIGHT_MODE_SCOPES = ("images", "content")


def night_mode_css(enabled, percent, scope="images"):
    """The one CSS source for Night mode dimming. "images" dims bright images
    only (the original behaviour); "content" dims the whole web view body,
    which is every card, the deck list, the overview, and the editor. Anki adds
    the nightMode class to body only in Night Mode, so neither rule ever
    applies in Day mode. Unknown scope, or disabled, is no CSS at all.

    The two scopes return different shapes on purpose: "images" comes back as a
    wrapped `<style>...</style>` block, since the card hook appends it straight
    onto card HTML, while "content" comes back as bare CSS, since the web view
    hook wraps it into the page head itself."""
    if not enabled or scope not in NIGHT_MODE_SCOPES:
        return ""
    factor = night_mode_dim_factor(percent)
    if scope == "content":
        return f"body.nightMode {{ filter: brightness({factor:.2f}); }}"
    return _night_mode_image_rule(factor)


def night_mode_image_css(enabled, percent=30):
    """CSS that dims bright white-background images while Anki's Night Mode is on.

    Anki's own Night Mode already adds a "nightMode" class to the card body, so this
    only needs to define the rule; the browser applies it only when that class is
    present. Dims rather than inverts, since a full color invert looks wrong on a
    real photo mixed into an otherwise diagram-heavy deck.

    `percent` is how much dimmer, 0-90 (see clamp_night_mode_dim_percent: this also
    re-clamps, so a caller can pass a raw config value straight through). The default,
    30, is the fixed dim level this replaced (a flat brightness(0.7)), so leaving the
    percentage untouched keeps today's exact appearance.
    """
    return night_mode_css(enabled, percent, "images")


def version_tuple(v):
    """Parse a version string into a tuple of ints, e.g. "0.10.2" -> (0, 10, 2)."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def version_at_least(current, latest):
    """True if `latest` is not newer than `current`.

    Zero-pads the shorter tuple so "0.5" and "0.5.0" compare equal instead of one
    looking shorter than (and therefore "less than") the other.
    """
    cur_n, latest_n = version_tuple(current), version_tuple(latest)
    width = max(len(cur_n), len(latest_n))
    cur_n = cur_n + (0,) * (width - len(cur_n))
    latest_n = latest_n + (0,) * (width - len(latest_n))
    return latest_n <= cur_n


def manifest_needs_newer_addon(manifest, supported_schema):
    """True if this manifest's format is newer than this add-on version understands.

    The deck-repo side writes a `schema` int into manifest.json, bumped only when the
    manifest's shape changes in a way an older add-on can't safely read (see that
    repo's own notes). Missing `schema` means an old manifest predating this field,
    always readable, so it defaults to 1 (never newer than any real supported_schema).

    A `schema` that isn't a plain int ("3", 2.0, None) counts as newer rather than
    raising: a manifest this add-on can't even parse the version of is exactly the case
    the "update the add-on" path exists for, and comparing it raises TypeError instead.
    """
    if not manifest:
        return False
    schema = manifest.get("schema", 1)
    if not isinstance(schema, int) or isinstance(schema, bool):
        return True
    return schema > supported_schema


def manifest_scope_suggestion(manifest, scope_tag, export_deck):
    """(suggested scope_tag, suggested export_deck) worth offering, or None for each.

    A deck source's manifest may carry the author's own `scope_tag` and `export_deck`
    (schema-additive; older add-ons ignore them), because both config values default
    to the Intern Pearls deck's: without matching them, a subscriber to someone else's
    deck gets no protected-fields snapshot and mis-scoped backups. A value is
    suggested only when it's a non-empty string that differs from what's configured
    now; the caller asks before applying anything. A scope tag is quoted into every
    collection search, so one holding a quote, whitespace or `*` is never offered.
    """
    def pick(key, current):
        v = (manifest or {}).get(key)
        return v if isinstance(v, str) and v and v != current else None

    tag = pick("scope_tag", scope_tag)
    if tag and re.search(r"[\s\"'*]", tag):
        tag = None
    return tag, pick("export_deck", export_deck)


def parse_fields(text, default=("Notes",)):
    """Parse the deck manager's comma-separated "preserved fields" box into a clean list.

    Trims whitespace, drops empties, de-dupes (keeping order). Falls back to `default` if
    nothing usable is left, so the annotation safety net can't be emptied by accident.
    """
    out = []
    for f in (text or "").split(","):
        f = f.strip()
        if f and f not in out:
            out.append(f)
    return out or list(default)


def decks_to_update(manifest, installed, excluded=None, held=None):
    """Decks from the manifest whose version differs from what's already installed.

    `installed` is {deck_name: version_last_applied}. A deck missing from it is new; a
    deck whose version changed needs re-sync; matching versions are skipped. `excluded`
    is an optional collection of deck names the user has opted out of syncing (from the
    deck manager, those are skipped regardless of version). `held` is deck names holding
    cards the learner set aside with "hold for later"; those count as pending even at a
    matching version, so the interactive run offers the held cards again. Auto-sync never
    passes it. Shared by Sync (to know what to apply) and Preview sync (to report the
    same set without touching the collection), so the two can never disagree about
    what's pending.

    An entry missing `name` or `version`, or a non-dict entry, is skipped rather than
    raising: without a name there is nothing to fetch or file cards under, and without a
    version there is nothing to compare against installed.json. One malformed row must
    not stop every other deck in the manifest from syncing.
    """
    excluded = set(excluded or ())
    held = set(held or ())
    out = []
    for d in (manifest or {}).get("decks", []):
        if not isinstance(d, dict):
            continue
        name, version = d.get("name"), d.get("version")
        if not name or version is None or name in excluded:
            continue
        if installed.get(name) != version or name in held:
            out.append(d)
    return out


def deck_status(manifest, installed, excluded=None):
    """One row per available deck for the deck-manager UI.

    Returns dicts with the deck's full `name`, a short display label, its `cards` count,
    whether it's `enabled` (not opted out), and a `state` relative to the collection:
    "new" (never synced), "update" (a newer version is available), or "current" (already
    up to date). Pure so the manager dialog stays a thin rendering layer over it.

    An entry missing `name`, or a non-dict entry, is skipped rather than raising, same as
    `decks_to_update`: Manage decks must not crash on a manifest row Sync already tolerates.
    """
    excluded = set(excluded or ())
    rows = []
    for d in (manifest or {}).get("decks", []):
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        inst, avail = installed.get(name), d.get("version")
        state = "new" if inst is None else ("current" if inst == avail else "update")
        rows.append({
            "name": name,
            "short": name.split("::")[-1],
            "cards": d.get("cards"),
            "enabled": name not in excluded,
            "state": state,
        })
    return rows


def should_notify_update(current, latest, last_notified=None):
    """Decide whether the startup check should surface an "update available" notice.

    True only if `latest` is strictly newer than the installed `current` version AND we
    haven't already notified about `latest` (or anything at least as new) — so each new
    release nags at most once, even across restarts. A missing/blank `latest` (e.g. a
    failed fetch) returns False. Pure so the nag policy is unit-tested, not guessed at.
    """
    if not latest:
        return False
    if version_at_least(current, latest):          # current already >= latest
        return False
    if last_notified and version_at_least(last_notified, latest):
        return False                               # already told them about this one
    return True


def clamp_interval_minutes(minutes, floor_minutes=1, default_minutes=15,
                           ceiling_minutes=7 * 24 * 60):
    """Sanitize a configured poll interval: a missing or non-numeric value falls back to
    `default_minutes`; anything below `floor_minutes` is raised to the floor so a typo
    (or a 0) can't turn into a busy-poll loop against the deck source, and anything
    above `ceiling_minutes` is lowered to the ceiling.

    The ceiling matters as much as the floor: the result becomes a QTimer interval in
    milliseconds, which is a C int, so a hand-edited value big enough to overflow it
    raised out of the startup wiring and gave Anki's raw add-on error dialog on every
    launch until config.json was fixed by hand.
    """
    try:
        m = int(minutes)
    except (TypeError, ValueError, OverflowError):
        m = default_minutes
    return min(ceiling_minutes, max(floor_minutes, m))


def decide_addon_update_action(current, latest, auto_update, notify, last_notified=None):
    """Decide what the background add-on-update check should do.

    Returns one of:
      "none"        - current is already up to date, or nothing should happen.
      "auto_update" - download and install the new version without asking.
      "notify"      - surface a tooltip only, once per release.

    Auto-update takes priority over notify when both are on, since actually installing
    the update makes a plain notice redundant. Notify still respects the once-per-release
    suppression via `should_notify_update`, so turning auto-update off doesn't bring back
    a notice for a version already reported. Pure so this policy is unit-tested rather
    than embedded inside code that also touches the network and the collection.
    """
    if not latest or version_at_least(current, latest):
        return "none"
    if auto_update:
        return "auto_update"
    if notify and should_notify_update(current, latest, last_notified):
        return "notify"
    return "none"


def find_deck_moves_needed(moves_ledger, existing_guid_to_deck, existing_front_to_guid=None):
    """Which of the learner's cards need to move deck to match a pure reorg.

    `moves_ledger` is {guid: {from, to, front?}}: every note the deck repo has ever
    relocated without changing its GUID (see build_all.py's deck_moves.json). `front`,
    when present, is the note's current first field, used to find the learner's card
    even when its GUID no longer matches the ledger's (see below).
    `existing_guid_to_deck` is {guid: current deck name} for the learner's collection.
    `existing_front_to_guid` is {first field: guid} for the learner's collection (optional).

    Normally a card is matched to a ledger entry by GUID. But a card whose deck source
    changed its `id_seed` (say a deck's seed moving from v1 to v2) has a *different*
    GUID in the learner's older collection than the one the ledger is keyed by, so a
    pure GUID match misses it: the card sits stuck at `from` forever, its new deck
    perpetually re-offered because installed_matching_collection never finds a card
    under it. So when the ledger GUID isn't in the learner's collection, fall back to
    matching by `front` (the same signal content-sync's remap_cards trusts; fronts are
    unique across decks by build lint), and act on the learner's GUID for that front. An
    older manifest without `front`, or a caller that passes no `existing_front_to_guid`,
    simply keeps the GUID-only behavior.

    A move only applies if the learner's card is still sitting exactly where the deck
    source last put it (`from`). If it's anywhere else (already at `to` because the
    learner reconciled a previous move, or somewhere of their own choosing because they
    filed it into a custom deck), leave it alone. This is what makes reconciling deck
    moves both idempotent (nothing to do once it's there) and non-destructive of the
    learner's own organization (a deliberate move away from `from` is never overwritten).

    Returns [{guid, from, to}] where `guid` is the learner's own note GUID (so
    apply_deck_moves can find it), sorted by `to` then `from` for stable display.
    """
    out = []
    for guid, move in (moves_ledger or {}).items():
        existing_guid = guid if guid in existing_guid_to_deck else None
        if existing_guid is None and existing_front_to_guid and move.get("front"):
            existing_guid = existing_front_to_guid.get(move["front"])
        if existing_guid is not None and existing_guid_to_deck.get(existing_guid) == move.get("from"):
            out.append({"guid": existing_guid, "from": move["from"], "to": move["to"]})
    out.sort(key=lambda m: (m["to"], m["from"]))
    return out


def fields_to_carry_over(saved, target_current):
    """Which of a retired note's protected-field values to copy onto one of its
    replacement notes.

    `saved` is {field: value} read off the note being retired; `target_current` is
    the same shape for the replacement. Never overwrites a field the replacement
    already has text in — the learner may have already started annotating it, or a
    previous partial run already carried a value over — so this only ever fills in
    a field that's currently blank.

    Matched case-insensitively: the predecessor and replacement note types can spell
    the same field with different casing, and a case-sensitive lookup would read the
    replacement's actual value as missing instead.
    """
    current_lower = {f.lower(): v for f, v in target_current.items()}
    return {f: v for f, v in saved.items()
            if v.strip() and not (current_lower.get(f.lower()) or "").strip()}


def find_retired_in_collection(retired_ledger, existing_guids, existing_front_to_guid=None,
                               live_guids=(), live_fronts=()):
    """The retired cards the learner still has in their collection.

    When a deck splits, merges, or drops a card, the old card's GUID leaves the
    canonical set but the learner's copy of it is never touched by a sync (sync only
    ever adds the replacements), so it lingers in their reviews as a duplicate. The deck
    repo records every such retirement in `retired.json`, shipped to us inside the
    manifest.

    `retired_ledger` is that ledger: {deck_name: {guid: {identity, reason,
    superseded_by, ...}}}. `existing_guids` is the set of note GUIDs the learner has
    under the scope tag. `existing_front_to_guid` is {first field: guid} for those same
    notes (optional).

    Normally a retired card is matched by GUID. But a learner whose copy predates the
    identity the ledger is keyed by (the card was reworded between import and the GUID
    freeze, or its deck source changed `id_seed`) holds a *different* GUID, so a pure
    GUID match misses that copy and the retired card lingers in their reviews forever,
    with nothing to signal it. So when the ledger GUID isn't in the learner's
    collection, fall back to matching by front text (the same signal content-sync's
    remap_cards trusts; fronts are unique across decks by build lint) and report the
    learner's own GUID for it. The front compared is the entry's own `front` if the
    ledger records one, else its `identity`, which for a basic or cloze note is exactly
    the front. Two kinds of entry therefore keep GUID-only behaviour, both by simply not
    matching rather than by matching something wrong: an image note (identity is
    "image||answer", never a first field), and a card whose front was reworded under a
    frozen `id` before it was retired (identity is the pre-reword wording). Recording
    `front` at retirement time closes that second gap without another release here.

    Returns one dict per retired card the learner still has, so the reconcile flow can
    show and archive them:
        {guid, source_guid, deck, identity, reason, superseded_by, replacements_present}
    where `guid` is the learner's own note GUID. `source_guid` is this same entry's
    ledger key, unchanged by the front fallback, so a caller looking something up in a
    manifest map keyed the builder's way (note_protected_fields) has the guid that map
    actually uses, not the learner's resolved one. `replacements_present` is how many of
    `superseded_by` are already in the learner's collection, so the UI can distinguish
    "replaced by cards you already have" from "sync first to get the replacements". It
    stays a GUID-only count: a collection whose GUIDs have drifted reads 0 and gets the
    advisory "sync first" note, which is a cosmetic miss, not a wrong archive. Sorted
    by deck then identity for stable display. Pure: the caller supplies the collection
    maps and does anything collection-touching (tag checks, the archive itself).

    `live_guids` and `live_fronts` are the guids and fronts of the cards in the current
    packages. A learner note that matches either is a live card, whatever the ledger
    says about its identity (a reseed keeps the front), and is never reported.
    """
    existing_guids = set(existing_guids)
    guid_front = {g: f for f, g in (existing_front_to_guid or {}).items()}
    live_guids, live_fronts = set(live_guids), set(live_fronts)
    out = []
    for deck, entries in (retired_ledger or {}).items():
        for guid, info in (entries or {}).items():
            existing_guid = guid if guid in existing_guids else None
            if existing_guid is None and existing_front_to_guid:
                front = info.get("front") or info.get("identity") or ""
                existing_guid = existing_front_to_guid.get(front) if front else None
            if existing_guid is None:
                continue
            if existing_guid in live_guids or guid_front.get(existing_guid) in live_fronts:
                continue
            sup = list(info.get("superseded_by") or [])
            out.append({
                "guid": existing_guid,
                "source_guid": guid,
                "deck": deck,
                "identity": info.get("identity", ""),
                "reason": info.get("reason", ""),
                "superseded_by": sup,
                "replacements_present": sum(1 for g in sup if g in existing_guids),
            })
    out.sort(key=lambda r: (r["deck"], r["identity"]))
    return out


def find_stranded_pairs(superseded, existing_front_to_guid, live_guids=(), live_fronts=()):
    """Pairs where the learner holds BOTH wordings of a card that was reworded once.

    Rewording a front freezes the old wording as the note's `id`, which keeps the GUID
    stable so the reword updates the learner's card in place. That works for anyone
    whose GUID matches. For anyone whose GUID drifted first, the reword matched nothing
    and imported as a second note, leaving the learner with the dead wording (carrying
    whatever review history was built) sitting beside the live one (starting from
    zero). Neither copy alone is right: the old one has the learner's progress but
    stale content, the new one has current content but no progress.

    `superseded` is the manifest's {superseded front: its replacement}, derived by the
    deck source from those same `id` freezes, so the pairing is the deck author's own
    declaration that the two wordings are one card rather than a guess made here.
    `existing_front_to_guid` is {first field: guid} for the learner's collection.

    Only pairs where the learner holds both are returned. Holding just the old wording
    is not a stranding: the import's own front matching merges it, which is the whole
    point of that ladder, and acting here too would fight it. Returns
    [{guid, front, successor_guid, successor_front}] sorted by front for stable display;
    `guid` is the predecessor, the copy that gets emptied out and archived.

    A chain of rewordings (A to B to C) pairs every older wording with the final one, so
    progress flows to the wording that is current, whatever order the pairs are applied
    in. A wording that leads into a cycle has no current successor and reports nothing.
    A predecessor whose guid or front is in `live_guids`/`live_fronts` (the current
    packages) is a live card and is never reported.
    """
    fronts = existing_front_to_guid or {}
    live_guids, live_fronts = set(live_guids), set(live_fronts)
    out = []
    for old_front in (superseded or {}):
        path, new_front = [old_front], superseded[old_front]
        while new_front in superseded and new_front not in path:
            path.append(new_front)
            new_front = superseded[new_front]
        if new_front in path:
            continue
        old_guid, new_guid = fronts.get(old_front), fronts.get(new_front)
        if (old_guid and new_guid and old_guid != new_guid
                and old_guid not in live_guids and old_front not in live_fronts):
            out.append({"guid": old_guid, "front": old_front,
                        "successor_guid": new_guid, "successor_front": new_front})
    out.sort(key=lambda p: p["front"])
    return out


# What a deck package may be before anything in it is unpacked. Real packages run to about
# 40 MB for a deck with pictures and about 200 MB and a thousand files for a whole
# collection export with media, so these leave wide headroom and only stop a package no
# deck needs: one that would fill the disk or memory on the main thread mid-sync.
APKG_MAX_BYTES = 512 * 1024 * 1024          # the .apkg file itself
APKG_MAX_EXPANDED = 2 * 1024 * 1024 * 1024  # every member, unpacked
APKG_MAX_MEMBER = 1024 * 1024 * 1024        # any one member, unpacked
APKG_MAX_MEMBERS = 50000
# Every central directory entry takes at least 46 bytes, so a directory larger than this
# could list more entries than APKG_MAX_MEMBERS. A real one is tens of KB.
_ZIP_ENTRY_MIN = 46
_MEDIA_INDEX_MAX = 32 * 1024 * 1024


class PackageLimitError(RuntimeError):
    """A package refused for its size or shape before anything in it was unpacked."""


def _size_text(n):
    gb = n / (1024 ** 3)
    return f"{gb:g} GB" if gb >= 1 else f"{n / (1024 ** 2):g} MB"


def _zip_directory_size(path):
    """(entry count, directory bytes) from the archive's end records, or None when there
    is no end record (zipfile then reports the file as not a zip).

    Read the way zipfile reads them, so the numbers are the ones it will act on: the
    32-bit end record at the very end when it is there, else the last one in the final
    64 KB, and a zip64 record whenever a zip64 locator sits in front of it, whatever
    the 32-bit fields say (a writer may saturate those). zipfile parses as many bytes of
    directory as the record says, so the directory size is what bounds the work, and a
    record that undercounts its entries can't hide a large one. Where a zip64 record
    could be found two ways (next to its locator, or at the offset the locator names)
    the larger reading wins."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - (22 + 65535))
        fh.seek(start)
        tail = fh.read()
        i = len(tail) - 22
        if not (i >= 0 and tail[i:i + 4] == b"PK\x05\x06" and tail[-2:] == b"\0\0"):
            i = tail.rfind(b"PK\x05\x06")
        if i < 0 or len(tail) < i + 22:
            return None
        entries, cd_size = struct.unpack("<HI", tail[i + 10:i + 16])
        end_at = start + i
        if end_at >= 20:
            fh.seek(end_at - 20)
            locator = fh.read(20)
            if len(locator) == 20 and locator[:4] == b"PK\x06\x07":
                named = struct.unpack("<Q", locator[8:16])[0]
                found = []
                for at in {end_at - 20 - 56, named}:
                    if not 0 <= at <= size - 56:
                        continue
                    fh.seek(at)
                    record = fh.read(56)
                    if record[:4] == b"PK\x06\x06":
                        found.append(struct.unpack("<QQ", record[32:48]))
                if found:
                    entries = max(e for e, _ in found)
                    cd_size = max(c for _, c in found)
        return entries, cd_size


def check_apkg_limits(path):
    """Refuse a package whose size or shape no deck needs, from its zip directory alone.

    Raises PackageLimitError naming what was over; a file that isn't a zip at all raises
    zipfile.BadZipFile as before. The unpacked sizes are the ones the directory declares.
    zipfile never writes more than that for a member and checks its CRC, but Anki's own
    importer reads the stream and ignores them, so a package handed to it directly goes
    through copy_apkg_checked first.
    """
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise PackageLimitError("This .apkg isn't a regular file, so it wasn't opened.")
    if st.st_size > APKG_MAX_BYTES:
        raise PackageLimitError(f"This .apkg is larger than {_size_text(APKG_MAX_BYTES)}, "
                                "more than any deck needs, so it wasn't opened.")
    directory = _zip_directory_size(path)
    if directory is not None:
        entries, cd_size = directory
        if entries > APKG_MAX_MEMBERS:
            raise PackageLimitError(f"This .apkg holds more than {APKG_MAX_MEMBERS:,} "
                                    "files, more than any deck needs, so it wasn't "
                                    "opened.")
        if cd_size // _ZIP_ENTRY_MIN > APKG_MAX_MEMBERS:
            raise PackageLimitError("This .apkg's file list is too large, more than any "
                                    "deck needs, so it wasn't opened.")
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    if len(infos) > APKG_MAX_MEMBERS:
        raise PackageLimitError(f"This .apkg holds more than {APKG_MAX_MEMBERS:,} files, "
                                "more than any deck needs, so it wasn't opened.")
    if any(i.file_size > APKG_MAX_MEMBER for i in infos):
        raise PackageLimitError(f"A file in this .apkg would unpack to more than "
                                f"{_size_text(APKG_MAX_MEMBER)}, so it wasn't opened.")
    if sum(i.file_size for i in infos) > APKG_MAX_EXPANDED:
        raise PackageLimitError(f"This .apkg would unpack to more than "
                                f"{_size_text(APKG_MAX_EXPANDED)}, more than any deck "
                                "needs, so it wasn't opened.")


def copy_apkg_checked(src, out):
    """Copy the package at `src` to `out` member by member through zipfile, after
    check_apkg_limits. zipfile writes no more than each member's declared size and
    raises zipfile.BadZipFile on a CRC mismatch, so the copy holds exactly what the
    limits were checked against, whatever the original's streams say."""
    check_apkg_limits(src)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                                                      compresslevel=1) as zout:
        for info in zin.infolist():
            if info.is_dir():
                continue
            with zin.open(info) as fin, zout.open(info.filename, "w",
                                                  force_zip64=True) as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)


def safe_source_path(path):
    """`path` from a manifest, as a plain relative path that stays inside the source.

    Absolute paths, drive letters, backslashes, '..' segments and NUL are refused with
    a RuntimeError; '.' segments and doubled slashes are dropped. A local folder source
    also checks where the path resolves (sync._local_source_file), since a symlink
    inside the folder can still point out of it.
    """
    shown = repr(path) if not isinstance(path, str) else f'"{path}"'
    bad = RuntimeError(f"the manifest path {shown} isn't a plain path inside the deck "
                       "source, so it wasn't read")
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        raise bad
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise bad
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise bad
    return "/".join(parts)


class _CappedWriter:
    """A file wrapper that refuses to grow past APKG_MAX_MEMBER."""

    def __init__(self, fh):
        self._fh, self._written = fh, 0

    def write(self, data):
        self._written += len(data)
        if self._written > APKG_MAX_MEMBER:
            raise PackageLimitError("This .apkg's collection would unpack to more than "
                                    f"{_size_text(APKG_MAX_MEMBER)}, so it wasn't "
                                    "opened.")
        return self._fh.write(data)


# Unicode's Default_Ignorable_Code_Point ranges: characters a renderer draws as nothing.
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
              (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
              (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
              (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))


def _is_hidden(ch):
    """A character that draws nothing (or reorders what is drawn) but a model still
    reads: Unicode's default-ignorable code points (zero-width, bidi controls, tags,
    variation selectors), any other format character, and control characters other
    than tab and line breaks."""
    if ch in "\t\n\r":
        return False
    o = ord(ch)
    return (unicodedata.category(ch) in ("Cf", "Cc")
            or any(lo <= o <= hi for lo, hi in _IGNORABLE))


def reveal_hidden(text):
    """(`text` with every hidden character written as [U+XXXX], how many there were)."""
    out, count = [], 0
    for ch in text:
        if _is_hidden(ch):
            out.append(f"[U+{ord(ch):04X}]")
            count += 1
        else:
            out.append(ch)
    return "".join(out), count


def strip_hidden(text):
    """`text` without its hidden characters (see reveal_hidden)."""
    return "".join(ch for ch in text if not _is_hidden(ch))


def skill_version(version):
    """A deck skill's version, kept to the characters a version uses. It is the
    source's own string and is shown beside the skill, so nothing in it may read as
    markup."""
    return re.sub(r"[^\w.+-]", "", str(version or ""))[:32]


@contextlib.contextmanager
def _apkg_db(path):
    """Yield (open sqlite connection, is_newer_format) for an .apkg's real collection.

    A package Anki exports today holds its data in a zstd-compressed collection.anki21b
    and ships a near-empty collection.anki2 stub beside it, carrying one placeholder note
    that reads "Please update to the latest Anki version". So the newer member has to
    win, or every reader here sees that stub instead of the deck: the visible symptom is
    an import that matches nothing, imports every card as new, and leaves the
    protected-field restore with no matched note to restore onto.

    Older exports can likewise carry an operative collection.anki21 beside a
    collection.anki2 compatibility copy, so .anki21 wins on that legacy path.

    zstandard is not stdlib and Anki does not ship it, so on a modern package the
    decode is impossible here and the reader stops with NEWER_APKG_ERROR: a loud
    "re-export this file" beats a silent empty result.

    The package's limits are checked first (check_apkg_limits), and a zstd member is
    decoded no further than APKG_MAX_MEMBER, since its zip entry can't say how large
    it decodes to.
    """
    check_apkg_limits(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        newer = "collection.anki21b" in names
        if newer:
            member = "collection.anki21b"
        elif "collection.anki21" in names:
            member = "collection.anki21"
        else:
            member = "collection.anki2"
        if member not in names:
            raise RuntimeError("Unexpected .apkg format (no collection found).")
        with tempfile.TemporaryDirectory() as d:
            z.extract(member, d)
            src = os.path.join(d, member)
            if newer:
                try:
                    import zstandard
                except ImportError:
                    raise RuntimeError(NEWER_APKG_ERROR) from None
                db = os.path.join(d, "decoded.anki2")
                with open(src, "rb") as fh, open(db, "wb") as out:
                    zstandard.ZstdDecompressor().copy_stream(fh, _CappedWriter(out))
            else:
                db = src
            con = sqlite3.connect(db)
            try:
                yield con, newer
            finally:
                con.close()


def _apkg_notetypes(con, newer):
    """{notetype id as str: (notetype name, [field name, ...])} from either format.

    The legacy format keeps all of this in col.models as JSON. The newer schema leaves
    that column empty and splits its content across real tables: `notetypes` holds the
    name and `fields` holds one row per field, both as plain text columns, so a name and
    its field list are recoverable without decoding the protobuf `config` blobs beside
    them (which is where CSS and template HTML live, see apkg_models).
    """
    if newer:
        out = {str(ntid): (name, []) for ntid, name in
               con.execute("select id, name from notetypes")}
        for ntid, name in con.execute(
                "select ntid, name from fields order by ntid, ord"):
            entry = out.get(str(ntid))
            if entry is not None:
                entry[1].append(name)
        return out
    try:
        models = json.loads(con.execute("select models from col").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        return {}
    out = {}
    for mid, m in (models or {}).items():
        ordered = sorted(m.get("flds", []), key=lambda f: f.get("ord", 0))
        out[str(mid)] = (m.get("name", ""), [f.get("name", "") for f in ordered])
    return out


def apkg_notes(path):
    """Return (note_id, fields, guid) for every note in an .apkg file, where `fields` is
    the note's complete field list.

    Every field rather than just the front, because matching and display want different
    things from a note. Matching only ever keys on `fields[0]` (see remap_cards), but a
    caller that shows the card to a person needs the rest: an image note's first field
    is an <img> tag, not a prompt, so field zero alone renders as a broken image instead
    of naming the card. note_display_label picks the right field out of the whole list.

    Both on-disk formats carry the same plain `notes` table, so this reads real notes out
    of a modern package as well as a legacy one.
    """
    with _apkg_db(path) as (con, _newer):
        return [(rid, flds.split(FS), guid) for rid, guid, flds in
                con.execute("select id, guid, flds from notes")]


def apkg_media_index(path):
    """{media filename: zip member} for the pictures an .apkg carries.

    An .apkg stores media as numbered blobs beside a JSON member called "media" mapping
    each number back to the filename a card's <img> tag actually references. A deck with
    no pictures, or one whose index will not parse, returns {} rather than raising: the
    only caller falls back to naming the image, which is what it did before it could
    resolve one at all.
    """
    try:
        check_apkg_limits(path)
        with zipfile.ZipFile(path) as z:
            if "media" not in z.namelist():
                return {}
            if z.getinfo("media").file_size > _MEDIA_INDEX_MAX:
                return {}
            entries = json.loads(z.read("media").decode("utf8"))
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeDecodeError,
            PackageLimitError):
        return {}
    if not isinstance(entries, dict):
        return {}
    return {name: member for member, name in entries.items() if isinstance(name, str)}


def extract_apkg_media(path, index, names, dest):
    """Extract just the named pictures out of an .apkg into `dest`.

    Returns {filename: local path} for the ones that came out. Deliberately not the
    whole archive: a deck can carry a couple of hundred images and a review that opens
    two rows has no reason to pay for the others. A name absent from `index`, or a
    member absent from the zip, is skipped rather than raised, so one stale reference in
    one field cannot blank the row it sits in.
    """
    out = {}
    wanted = [n for n in dict.fromkeys(names) if n in index]
    if not wanted:
        return out
    try:
        check_apkg_limits(path)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            members = set(z.namelist())
            for name in wanted:
                if index[name] not in members:
                    continue
                local = os.path.join(dest, os.path.basename(name))
                if not os.path.exists(local):
                    with z.open(index[name]) as src, open(local, "wb") as fh:
                        fh.write(src.read())
                out[name] = local
    except (OSError, zipfile.BadZipFile, PackageLimitError):
        return out
    return out


def apkg_deck_names(path):
    """Every Anki deck name inside an .apkg, in either on-disk format.

    Newer files hold a zstd-compressed collection.anki21b whose decks table separates
    path segments with \\x1f, and also ship a near-empty legacy collection.anki2 stub,
    so the newer name has to win or the stub reads as an empty file. _apkg_db picks the
    member and decodes it; only the decks table's own shape differs per format here.
    """
    with _apkg_db(path) as (con, newer):
        if newer:
            rows = [r[0] for r in con.execute("select name from decks")]
            return [r.replace("\x1f", "::") for r in rows]
        blob = con.execute("select decks from col").fetchone()[0]
        return [d_["name"] for d_ in json.loads(blob).values()]


def manifest_decks_for(deck_names, manifest_names):
    """Which manifest decks own the given Anki deck names.

    A spec's deck_name is routinely just the parent path, with cards filed under
    deck_name::<subdeck>, so an exact match alone misses every subdeck-based deck.
    Where two manifest names both prefix a deck, the longest one owns it.
    """
    owners = set()
    for deck in deck_names:
        best = None
        for name in manifest_names:
            if deck == name or deck.startswith(name + "::"):
                if best is None or len(name) > len(best):
                    best = name
        if best is not None:
            owners.add(best)
    return [n for n in manifest_names if n in owners]


def apkg_note_details(path, rids=None):
    """Return full, labeled field detail for notes in the .apkg at `path`, as a list of
    {"rid", "guid", "notetype", "fields": [(field_name, value), ...]} in the .apkg's own
    note order. `rids`, if given, limits the result to those note ids.

    Separate from apkg_notes because it costs more and is wanted less often: this reads
    the note types too, and only the review dialog needs it, only when the learner opens
    it. The normal update path never pays for it.

    Field names come from the .apkg's own `col.models` rather than from position,
    because our note types don't agree on layout: index 1 is "Back" on a basic note but
    "Prompt" on an image note, so a positional guess mislabels whole decks. A note whose
    note type isn't described in this .apkg (or an .apkg carrying no models at all)
    falls back to generic "Field N" labels instead of failing, since a plainly-labeled
    preview is worth more to the learner than a raised exception.

    A newer-format .apkg keeps the same information in its `notetypes` and `fields`
    tables, so it labels just as precisely (_apkg_notetypes reads either).
    """
    wanted = None if rids is None else set(rids)
    with _apkg_db(path) as (con, newer):
        names = _apkg_notetypes(con, newer)
        rows = list(con.execute("select id, guid, mid, flds from notes"))

    out = []
    for rid, guid, mid, flds in rows:
        if wanted is not None and rid not in wanted:
            continue
        notetype, field_names = names.get(str(mid), ("", []))
        labeled = [(field_names[i] if i < len(field_names) else f"Field {i + 1}", value)
                   for i, value in enumerate(flds.split(FS))]
        out.append({"rid": rid, "guid": guid, "notetype": notetype, "fields": labeled})
    return out


def apkg_note_types(path):
    """{guid: notetype name} for every note in the .apkg.

    Cheaper than apkg_note_details, which reads every field of every note to render a
    review list; this joins notes to the note-type names alone, which is all the
    note-type-change check needs on the sync path. Reads either on-disk format, since a
    modern package names its note types in the `notetypes` table instead.
    """
    with _apkg_db(path) as (con, newer):
        names = {int(mid): n for mid, (n, _f) in _apkg_notetypes(con, newer).items()}
        return {guid: names.get(mid, "")
                for guid, mid in con.execute("select guid, mid from notes")}


def base_notetype_name(name):
    """A note type's name with Anki's collision suffix stripped.

    Importing a note type whose name matches an existing one with different fields makes
    Anki keep both and append "+" to the newcomer, so a collection that has re-imported a
    deck across several field additions ends up holding "Study Deck - Basic",
    "Study Deck - Basic+", "Study Deck - Basic++" and so on, every one of them ours.
    Measured on a real collection: 595 of 625 notes sat on a suffixed variant and only 30
    on the bare name, so treating a suffix as somebody else's note type would skip
    virtually every card that needs converting.
    """
    return (name or "").rstrip("+") or (name or "")


def plan_notetype_changes(incoming_types, existing_types, managed):
    """Which of the learner's notes have to change note type for this update to land on
    them instead of beside them.

    Converting a Q&A card to a cloze changes its note type, and Anki's importer will not
    move an existing note to a different one. Without this the incoming note imports
    fresh and the learner keeps a stale duplicate holding all their history, which is
    why such a conversion has always had to retire the old card and restart the new one
    from zero.

    Both arguments are keyed by the learner's own note guid: `incoming_types` is what
    this update would make each matched note (the caller resolves the .apkg's own guids
    through remap_cards first, so a note matched by front counts too), `existing_types`
    is what they are now. A change is planned only when both names are in `managed`, so
    a learner's own note types are never touched and an unrecognised incoming type is
    left alone.

    Returns [{guid, old, new}] sorted by guid, for a caller that asks permission first:
    Anki treats this as a schema change, meaning a one-time full AnkiWeb sync.
    """
    out = []
    for guid, new in (incoming_types or {}).items():
        old = (existing_types or {}).get(guid)
        if old is None:
            continue
        old_base, new_base = base_notetype_name(old), base_notetype_name(new)
        if (old_base == new_base or old_base not in managed
                or new_base not in managed):
            continue
        out.append({"guid": guid, "old": old, "new": new})
    out.sort(key=lambda c: c["guid"])
    return out


def cards_lost_in_conversion(card_ords, to_cloze, template_map, template_count):
    """How many of a note's cards (by ordinal) Anki's change-notetype would delete.

    A cloze target keeps every card. Otherwise a card survives only if a template takes
    its ordinal: through `template_map` (Anki's default map between two regular types,
    old ordinal per new template, -1 for none) or, from a cloze, by being below
    `template_count`. Anki may keep one out-of-range cloze card; counting it as lost
    errs toward keeping the note beside.
    """
    if to_cloze:
        return 0
    kept = ({o for o in template_map if o >= 0} if template_map
            else set(range(template_count)))
    return sum(1 for o in card_ords if o not in kept)


def split_notetype_changes(changes):
    """(convert in place, keep beside): a change that would delete any card is never
    converted; the learner's note stays as it is and the new version imports beside it."""
    lossy = [c for c in changes if c.get("drops")]
    return [c for c in changes if not c.get("drops")], lossy


def apkg_models(path):
    """Return {notetype_name: {"css": str, "tmpls": [(name, qfmt, afmt), ...]}} for
    every note type carried by the .apkg at `path`.

    Reads the legacy `col.models` JSON column, the format genanki (and Anki's own
    legacy exporter) writes.

    This is the one reader a newer-format package cannot satisfy: its `notetypes` and
    `templates` tables carry CSS and question/answer HTML inside protobuf-encoded blobs,
    not text, and decoding those would mean a protobuf dependency for a comparison that
    only decides whether to offer a template update. So a modern package raises with the
    re-export instruction instead. Returning {} would read as "no templates differ" and
    silently drop a card-design change on the floor.
    """
    with _apkg_db(path) as (con, newer):
        if newer:
            raise RuntimeError(NEWER_APKG_ERROR)
        models_json = con.execute("select models from col").fetchone()[0]
    return {m["name"]: model_shape(m) for m in json.loads(models_json).values()}


def model_shape(m):
    """Reduce a note-type dict (apkg JSON or mw.col.models form — same keys) to just
    what determines how cards LOOK: CSS plus each template's question/answer HTML.
    Both sides of a template comparison go through this so they can't disagree on
    incidental keys (ids, mod times, field lists — fields are _ensure_notetypes' job).
    """
    return {
        "css": m.get("css", ""),
        "tmpls": [(t.get("name", ""), t.get("qfmt", ""), t.get("afmt", ""))
                  for t in m.get("tmpls", [])],
    }


def changed_templates(incoming, existing):
    """Note-type names present in both mappings whose template HTML or CSS differ.

    `incoming`/`existing` are {name: model_shape(...)}. A note type only the .apkg has
    isn't a template *change* (the import creates it as-is), so it's skipped.
    """
    return [name for name, shape in incoming.items()
            if name in existing and existing[name] != shape]


def note_fields_hash(fields):
    """A short stable digest of a note's field values, for the declined-card
    registry's changed-since-you-declined cue. 16 hex chars is plenty: collisions
    only ever cost one missing cue, never a wrong import."""
    return hashlib.sha256(FS.join(fields).encode("utf8")).hexdigest()[:16]


def change_notes_for(manifest_notes, guid, fields):
    """Return current change context plus durable learner-feedback history.

    A matching hash describes this exact incoming card. A stale learner-feedback hash
    is still useful context when the same GUID is revised again, so it is returned with
    ``historical=True`` for an explicit "earlier feedback" label. A stale maintainer
    note is omitted because it describes an implementation that no longer ships.

    Current feedback sorts ahead of current maintainer context, followed by historical
    feedback. Entries remain newest-first within each group.
    """
    entries = manifest_notes.get(guid) if isinstance(manifest_notes, dict) else None
    if not isinstance(entries, list):
        return []
    h = note_fields_hash(list(fields))
    filtered = []
    for entry in reversed(entries):
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("note"), str)
                or not entry.get("note")):
            continue
        if entry.get("hash") == h:
            filtered.append(entry)
        elif entry.get("kind") == "feedback":
            filtered.append({**entry, "historical": True})

    def display_group(entry):
        if entry.get("historical"):
            return 2
        return 0 if entry.get("kind") == "feedback" else 1

    return sorted(filtered, key=display_group)


def group_change_notes(details, retired):
    """Group pending cards and retired rows that belong to the same real-world change,
    so an update screen can show one explanation over the rows it caused instead of a
    caption repeated on each.

    `details` is a list of dicts, each carrying a `guid` and, optionally, a
    `change_notes` list in `change_notes_for`'s own shape ({kind, note, on, hash}).
    `retired` is a list of dicts, each carrying `superseded_by` (a list of guids) and
    whatever else the caller wants carried through untouched (identity, reason, ...).

    Two things join a set, both already in the data:

    - Two cards carrying an identical (kind, note, on) entry: a deck source can stamp
      the same recorded line on every card one piece of feedback drove, and that is one
      change, not several. Only a card's first matching entry is used to find or start
      its group; a second, unshared note on the same card (a feedback/maintainer
      pairing) is left in `change_notes` for the caller to render on the card itself.
    - A retired row naming one or more of these cards in `superseded_by`: the retired
      card and its replacement(s) are the same change. A retired row naming several
      cards already in different groups merges those groups into one, so a card split
      into two survivors still reads as one change with two children.

    A retired row naming no card present in this batch (retired in one update, its
    replacement shipped in a later one) is not silently dropped: it starts a group of
    its own, carrying no note, so it still renders alone with its own reason.

    Returns [{"note": entry-or-None, "members": [row, ...]}], in the order each group
    was first started. A group's `members` mixes `details` and `retired` entries, in
    the order they were added; a group of one, the ordinary case, carries whatever that
    single row already renders on its own, and it is the caller's choice what wrapping
    (if any) a bigger group deserves. Pure: no Anki, no I/O.
    """
    details = [d for d in (details or []) if isinstance(d, dict) and d.get("guid")]
    retired = [r for r in (retired or []) if isinstance(r, dict)]

    order = []
    groups = {}
    member_group = {}

    def _key(entry):
        return (entry.get("kind"), entry.get("note"), entry.get("on"))

    def _new_group(note):
        gid = len(order)
        order.append(gid)
        groups[gid] = {"note": note, "members": []}
        return gid

    note_group = {}
    for d in details:
        gid = None
        for e in d.get("change_notes") or []:
            if not isinstance(e, dict):
                continue
            k = _key(e)
            if k in note_group:
                gid = note_group[k]
                break
        if gid is None:
            first = next((e for e in (d.get("change_notes") or [])
                         if isinstance(e, dict)), None)
            gid = _new_group(first)
            if first is not None:
                note_group[_key(first)] = gid
        groups[gid]["members"].append(d)
        member_group[d["guid"]] = gid

    for r in retired:
        sup = [g for g in (r.get("superseded_by") or []) if g in member_group]
        gids = list(dict.fromkeys(member_group[g] for g in sup))
        if not gids:
            gid = _new_group(None)
        else:
            gid = gids[0]
            for other in gids[1:]:
                groups[gid]["note"] = groups[gid]["note"] or groups[other]["note"]
                groups[gid]["members"].extend(groups[other]["members"])
                for m in groups[other]["members"]:
                    mg = m.get("guid") if isinstance(m, dict) else None
                    if mg:
                        member_group[mg] = gid
                groups[other]["members"] = None   # merged away, dropped below
        groups[gid]["members"].append(r)

    return [groups[g] for g in order if groups[g]["members"] is not None]


SOURCE_LABEL_MAX = 120


def sort_source_groups(groups):
    """Order update groups and their members naturally by optional source label.

    Keep groups intact, ordered by their earliest label. Equal labels and unlabelled
    rows retain their relative order, with unlabelled rows last. Labels remain opaque
    text except that digit runs compare numerically, so T10Q2 precedes T10Q10.
    Return new lists without changing the input groups or member dictionaries.
    """
    def key(member):
        label = member.get("card_source", "").strip().casefold()
        return (not bool(label), tuple(
            (1, int(part)) if part.isdecimal() else (0, part)
            for part in re.split(r"(\d+)", label)))

    ordered = [dict(group, members=sorted(group["members"], key=key))
               for group in groups]
    return sorted(ordered, key=lambda group: key(group["members"][0]))


def source_label_for(manifest_sources, guid):
    """A deck source's own short label for where a card came from, e.g. `[T10Q2]`, or
    "" when it ships none.

    Opaque on purpose. The string is built by whoever publishes the deck, out of
    whatever a card's provenance means for that deck, without assuming a bank-specific
    shape. Display sorting compares digit runs numerically. Unlike a change note it carries no claim about why the card
    changed, so it needs no hash gate: it is derived from the same build as the
    content it sits next to.

    Anything that is not a plain string is dropped rather than rendered, and a
    runaway label is clipped, since one bad entry in a fetched manifest must not
    break a row or push everything else off it.
    """
    if not isinstance(manifest_sources, dict):
        return ""
    label = manifest_sources.get(guid)
    if not isinstance(label, str):
        return ""
    label = label.strip()
    return label[:SOURCE_LABEL_MAX] if label else ""


def prune_declined(reg, retired_guids, seen, manifest_decks):
    """Drop registry entries that are moot, and re-file the ones whose card moved.

    An entry is moot when its note was retired upstream, or when its guid is in no
    package of the manifest. `seen` is {deck: guids} for the decks actually read this
    run (a package's own guids plus the learner guids they matched), and absence is only
    judged once every deck in `manifest_decks` was read, because a card the source moved
    to a deck this run did not read is still there. An entry whose card turns up in a
    different deck than the one recorded follows it. A hand-edited entry that isn't even
    a dict is left alone unless its guid is retired: there's no "deck" to check it
    against, and a bad entry must degrade gracefully rather than crash a sync. Mutates
    `reg`; returns whether anything changed."""
    complete = set(manifest_decks) <= set(seen)
    changed = False
    for g in list(reg):
        e = reg[g]
        if g in retired_guids:
            del reg[g]
            changed = True
            continue
        if not isinstance(e, dict):
            continue
        homes = [d for d, guids in seen.items() if g in guids]
        if homes:
            if e.get("deck") not in homes:
                e["deck"] = homes[0]
                changed = True
        elif complete and e.get("deck") in seen:
            del reg[g]
            changed = True
    return changed


def declined_guids(registry):
    """Every guid a standing decline suppresses: every key of the registry, whatever
    state its entry holds.

    The single definition of "declined", so the import, the preview counts and the
    note-type-conversion plan can't disagree about which cards are in it. Membership
    alone is what declined_drop tests, so a hand-edited entry whose value isn't even a
    dict still suppresses its card, and anything that counts or plans around a decline
    has to read it the same way rather than filtering on `state`.
    """
    return set(registry or {})


def held_entries(registry):
    """{guid: entry} for every card set aside with "hold for later"."""
    return {g: e for g, e in (registry or {}).items()
            if isinstance(e, dict) and e.get("state") == "held"}


def held_deck_names(registry):
    """The decks that hold at least one held card."""
    return {e["deck"] for e in held_entries(registry).values() if e.get("deck")}


def held_outside_manifest(registry, manifest):
    """Held cards whose deck the source no longer lists. Nothing can offer them
    again, so the interactive run releases them rather than leaving them held."""
    names = {d.get("name") for d in (manifest or {}).get("decks", [])
             if isinstance(d, dict)}
    return [g for g, e in held_entries(registry).items() if e.get("deck") not in names]


def settle_held_outside_manifest(registry, manifest, seen):
    """Held entries filed under a deck the source no longer lists, settled by
    prune_declined's rule: one whose guid is in a package read this run follows it
    there and stays held; a retired one, or one in no package once every manifest deck
    was read, is released; any other waits for the deck it ships in to be read. `seen`
    is {deck: guids} for the decks read this run. Mutates `registry`; returns whether
    anything changed."""
    names = {d.get("name") for d in (manifest or {}).get("decks", [])
             if isinstance(d, dict)}
    retired = manifest_retired_guids(manifest)
    complete = names <= set(seen)
    changed = False
    for guid in held_outside_manifest(registry, manifest):
        homes = [d for d, guids in seen.items() if guid in guids]
        if guid in retired or (complete and not homes):
            del registry[guid]
            changed = True
        elif homes:
            registry[guid]["deck"] = homes[0]
            changed = True
    return changed


def manifest_retired_guids(manifest):
    """Every guid in a manifest's retired ledger ({deck: {guid: ...}} or {deck: [guid]}),
    skipping whatever in it is not that shape."""
    ledger = manifest.get("retired") if isinstance(manifest, dict) else None
    return {g for per_deck in (ledger.values() if isinstance(ledger, dict) else ())
            if isinstance(per_deck, (list, tuple, dict))
            for g in per_deck if isinstance(g, str)}


def migrate_declined(registry):
    """Turn every old "skip" entry into a held one flagged `migrated`, in place, so the
    first run after the change returns those cards at Later rather than Import. Returns
    whether anything changed; a second call changes nothing."""
    changed = False
    for entry in (registry.values() if isinstance(registry, dict) else ()):
        if isinstance(entry, dict) and entry.get("state") == "skip":
            entry["state"] = "held"
            entry["migrated"] = True
            changed = True
    return changed


def later_status(entry, incoming_hash):
    """What a returning held card's row needs to know about its history, as the detail
    keys the review screen reads: `later_wait` (the row starts at Later: a noted card
    whose content has not changed, or a migrated one), `later_note` and
    `later_note_status` ("not_updated" or "updated") for a noted card, and
    `later_migrated`. {} for anything that is not a held entry."""
    if not isinstance(entry, dict) or entry.get("state") != "held":
        return {}
    out = {}
    note = entry.get("note")
    stored = entry.get("hash")
    unchanged = bool(stored) and stored == incoming_hash
    if isinstance(note, str) and note:
        out["later_note"] = note
        out["later_note_status"] = "not_updated" if unchanged else "updated"
    if entry.get("migrated"):
        out["later_migrated"] = True
    if (out.get("later_note_status") == "not_updated") or entry.get("migrated"):
        out["later_wait"] = True
    return out


def later_count(registry, excluded, retired=()):
    """How many Later cards Update my decks can still offer: held entries outside the
    decks named in `excluded`, leaving out the `retired` guids the next run releases."""
    excluded, retired = set(excluded or ()), set(retired or ())
    return sum(1 for g, e in held_entries(registry).items()
               if e.get("deck") not in excluded and g not in retired)


def later_migration_line(n):
    """The update screen's one line over a list holding `n` rows that were Skips
    before Later replaced them, or "" when there are none."""
    if not n:
        return ""
    return (f"Skip is now Later. {plural(n, 'card')} you skipped earlier "
            f"{'is' if n == 1 else 'are'} below, still set to Later.")


def later_nudge_due(count, last_seen):
    """Whether the startup reminder about Later cards should show: only when the count
    has grown past what the learner was last told."""
    return count > (last_seen or 0)


# The standing declines a row's control can show, by row kind. A registry state outside
# these (an old Keep on a card that now arrives as new) is not a decision the row carries.
_ROW_DECLINES = {"new": ("never",), "changed": ("keep", "frozen")}


def carries_decision(entry, kind):
    """Whether a card row arrives already decided by an earlier update's decline."""
    return isinstance(entry, dict) and entry.get("state") in _ROW_DECLINES.get(kind, ())


def holdable_guids(registry, card_kinds, reviewed, decisions=None):
    """The card rows "hold for later" would set aside: a new or changed row the
    learner neither opened nor decided on, now or on an earlier update. A standing
    Keep/Never keeps its own state; an earlier hold, or an old decline the row
    cannot show, is held like any undecided row. A row in `decisions` (the screen's
    sparse {guid: state}, so away from its default, such as one seeded at Later) is
    not held either. `card_kinds` is {guid: kind} in row order."""
    decisions = decisions or {}
    return [guid for guid, kind in card_kinds.items()
            if kind in ("new", "changed") and guid not in reviewed
            and guid not in decisions
            and not carries_decision((registry or {}).get(guid), kind)]


def unopened_line(card_kinds, reviewed, folded=(), registry=None, decisions=None):
    """The update screen's count of card rows nobody has opened yet, how many of those
    sit inside a folded group and how many already carry an earlier update's decision,
    or "" once there are none. `card_kinds` is {guid: kind}; only new and changed rows
    count. `decisions` is the screen's live {guid: state}: an unopened row in it was
    seeded there by an earlier update (a waiting Later row, say), since a row decided
    this run counts as reviewed."""
    rows = [g for g, k in card_kinds.items() if k in ("new", "changed")]
    left = [g for g in rows if g not in reviewed]
    if not left:
        return ""
    folded = set(folded)
    inside = sum(1 for g in left if g in folded)
    decisions = decisions or {}
    decided = sum(1 for g in left
                  if g in decisions
                  or carries_decision((registry or {}).get(g), card_kinds[g]))
    notes = ([f"{inside} of them in folded groups"] if inside else []) + (
        [f"{decided} already decided on an earlier update"] if decided else [])
    where = (", " + " and ".join(notes)) if notes else ""
    return (f"<b>{len(left)} of {plural(len(rows), 'card')} not opened yet</b>{where}. "
            "Update applies each one as its row is set.")


# The card list gets a filter bar from this many card rows up.
FILTER_MIN_CARDS = 20

FILTER_MODES = (("all", "All"), ("new", "New"), ("changed", "Changed"),
                ("held", "Later"), ("unreviewed", "Not reviewed"))


def group_run(items, start):
    """(members, next_index): the run of card, retired and grouped-sep items from
    `start`, which is what sits under a ("group_note", ...) header."""
    end = start
    while end < len(items) and (items[end][0] in ("card", "retired")
                                or tuple(items[end][:2]) == ("sep", "grouped")):
        end += 1
    return list(items[start:end]), end


def count_cards(items):
    """Card rows in an update list, including the members of folded groups."""
    return sum(1 for _ in _card_items(items))


def _card_items(items):
    for item in items:
        if item[0] == "card":
            yield item
        elif item[0] == "group_note" and len(item) > 3:
            yield from (m for m in item[3] if m[0] == "card")


def _row_text(item):
    if item[0] == "card":
        return note_display_label([v for _, v in item[2].get("fields", [])],
                                  max_len=10 ** 6)
    return plain_text(item[1])


def _row_shown(item, mode, needle, touched):
    if item[0] == "card":
        detail = item[2]
        wanted = {"new": detail.get("kind") == "new",
                  "changed": detail.get("kind") == "changed",
                  "held": detail.get("declined_state") == "held",
                  "unreviewed": detail.get("guid") not in touched}
        if not wanted.get(mode, True):
            return False
    elif item[0] in ("retired", "moved"):
        if mode != "all":
            return False
    else:
        return False
    return not needle or needle in _row_text(item).casefold()


def _kept_members(members, mode, needle, touched):
    kept, sep = [], None
    for m in members:
        if m[0] == "sep":
            sep = m
        elif _row_shown(m, mode, needle, touched):
            kept += ([sep] if sep else []) + [m]
            sep = None
        else:
            sep = None
    return kept


def filter_update_items(items, mode="all", query="", touched=()):
    """`items` reduced to what a FILTER_MODES key and a search leave visible. Group
    headers keep and count only their matching members, and headings with nothing left
    under them go. Retired, moved and per-deck summary rows show only under All."""
    needle = query.strip().casefold()
    if mode == "all" and not needle:
        return list(items)
    out, heads, sep, fresh, prev, i = [], [], None, True, None, 0
    while i < len(items):
        item = items[i]
        i += 1
        if item[0] == "sep":
            sep = item
            continue
        was, prev = prev, item[0]
        if item[0] in ("header", "note"):
            # A note straight after a header reads with it; anything else starts over,
            # dropping a header whose rows were all filtered out.
            heads = heads + [item] if item[0] == "note" and was == "header" else [item]
            sep, fresh = None, True
            continue
        before, sep = sep, None
        rows = [item]
        if item[0] == "group_note":
            folded = len(item) > 3
            members, upto = (item[3], i) if folded else group_run(items, i)
            i = upto
            kept = _kept_members(members, mode, needle, touched)
            if not any(m[0] in ("card", "retired") for m in kept):
                continue
            count = sum(1 for m in kept if m[0] == "card")
            head = item[:2] + ((count,) if len(item) > 2 else ())
            rows = [head + (kept,)] if folded else [head] + kept
        elif not _row_shown(item, mode, needle, touched):
            continue
        out += heads
        heads = []
        if before is not None and not fresh:
            out.append(before)
        out += rows
        fresh = False
    return out


def released_held_guids(registry, card_kinds, decisions, hold, readable_decks):
    """Held entries this run settles by removing them: a held row left at its
    default (Import/Apply) and not held again, and a held card whose deck was read
    this run but which has nothing pending any more. A held row the learner
    declined is left to that decline's own write."""
    hold = set(hold)
    out = []
    for guid, entry in held_entries(registry).items():
        if guid in card_kinds:
            if guid not in decisions and guid not in hold:
                out.append(guid)
        elif entry.get("deck") in readable_decks:
            out.append(guid)
    return out


def declined_drop(src, remap, existing_fronts, declined, in_place, as_new):
    """The rids to drop for a decline, plus `touched` and `in_place`/`as_new`
    corrected to exclude them. `remap` is mutated in place (a dropped note's remap
    entry removed), so a drop always wins over a remap for the same note.

    Shared by collection._apply_deck and sync.import_single, so a decline filters
    identically whichever path a deck lands in the collection through."""
    drop, touched = set(), set()
    existing_guids = set(getattr(existing_fronts, "guids", existing_fronts.values()))
    for rid, _f, guid in apkg_notes(src):
        final = remap.get(rid, guid)
        if final in declined or guid in declined:
            drop.add(rid)
            if remap.pop(rid, None) is not None or guid in existing_guids:
                in_place -= 1
            else:
                as_new -= 1
        else:
            touched.add(final)
    return drop, touched, in_place, as_new


def write_personalized(src, remap, out, drop=frozenset(), prepare_notetypes=None):
    """Copy the .apkg at `src` to `out`, rewriting note GUIDs per `remap` and dropping declined notes.

    `remap` is {note_id: new_guid}. Notes not in `remap` are left untouched.
    `drop` is a set of note ids to remove entirely, notes and their cards rows both;
    a drop always wins over a remap for the same id.
    `prepare_notetypes`, when provided, receives the scratch SQLite connection
    after those edits, before commit. It never receives the original archive.

    Legacy format only, preferring collection.anki21 over its collection.anki2
    compatibility copy. It refuses a newer format rather than doing nothing quietly.
    Anki reads a modern package's collection.anki21b, so rewriting a legacy member beside
    it would apply no guid at all while every caller went on reporting the matched counts
    remap_cards computed: every front-matched card would import as a duplicate and the
    learner's history would stay on the copy they already had. Writing the anki21b back
    needs zstd compression, which nothing here has, so the honest answer is the re-export
    instruction.
    """
    check_apkg_limits(src)
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            if "collection.anki21b" in names:
                raise RuntimeError(NEWER_APKG_ERROR)
            z.extractall(d)
        member = ("collection.anki21" if "collection.anki21" in names
                  else "collection.anki2")
        with contextlib.closing(sqlite3.connect(os.path.join(d, member))) as con:
            drop = set(drop)
            if drop:
                marks = ",".join("?" * len(drop))
                con.execute(f"delete from notes where id in ({marks})", tuple(drop))
                has_cards = con.execute(
                    "select 1 from sqlite_master where type='table' and name='cards'"
                ).fetchone()
                if has_cards:
                    con.execute(f"delete from cards where nid in ({marks})", tuple(drop))
            for rid, g in remap.items():
                if rid in drop:
                    continue
                con.execute("update notes set guid=? where id=?", (g, rid))
            if prepare_notetypes is not None:
                prepare_notetypes(con)
            con.commit()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))


def remap_cards(src, existing_fronts, aliases):
    """Match each note in the .apkg at `src` to one of the learner's existing cards.

    Matching order, strongest signal first:
      1. GUID: the incoming note's GUID already belongs to one of the learner's cards.
         This is the durable path: deck specs give every card an explicit stable `id`,
         so a reworded front no longer changes the GUID and needs no alias at all.
      2. Front text: the learner's card currently shows this exact front
         (`existing_fronts` is {front_text: guid}). Covers collections whose GUIDs
         predate stable ids.
      3. `aliases` ({current_front: previous_front}): the learner's card still shows
         the one prior wording of a renamed front.
    Each tier runs over the whole package before the next, and a learner GUID is claimed
    by one incoming note only: a note whose text match is already taken imports as new,
    since a visible duplicate beats a card that silently never arrives.

    Returns (remap, in_place, as_new, new_notes, matched): `remap` is {note_id: guid} for
    notes whose GUID needs rewriting to match an existing card, `in_place`/`as_new` are
    counts for the confirmation dialogs. A GUID match needs no rewrite, so it never lands
    in `remap`.

    `new_notes` is [(note_id, fields, guid), ...] for the notes that will import as new,
    in the .apkg's own order, and `as_new` is exactly its length. `matched` is the
    complement, [(note_id, apkg_guid, existing_guid), ...], where existing_guid is the
    .apkg's own guid on a GUID match. Both are returned from here rather than re-derived
    by a second function so the matching ladder above stays the single source of truth
    about what "new" and "already existing" mean: a separate implementation would
    eventually disagree with this one, and the visible symptom would be a preview that
    lies to the learner about which cards are about to appear or change.
    """
    notes = apkg_notes(src)
    existing_guids = set(getattr(existing_fronts, "guids", existing_fronts.values()))
    resolved = {i: guid for i, (_rid, _fields, guid) in enumerate(notes)
                if guid in existing_guids}
    claimed = set(resolved.values())
    for use_alias in (False, True):
        for i, (_rid, fields, _guid) in enumerate(notes):
            front = fields[0] if fields else ""
            if i in resolved or (use_alias and front not in aliases):
                continue
            existing_guid = existing_fronts.get(aliases[front] if use_alias else front)
            # Locally generated cards are invisible to sync matching, and a learner
            # card is claimed by one incoming note only.
            if (existing_guid is not None and not is_generated_guid(existing_guid)
                    and existing_guid not in claimed):
                resolved[i] = existing_guid
                claimed.add(existing_guid)
    remap, new_notes, matched = {}, [], []
    for i, (rid, fields, apkg_guid) in enumerate(notes):
        if i not in resolved:
            new_notes.append((rid, fields, apkg_guid))
            continue
        matched.append((rid, apkg_guid, resolved[i]))
        if resolved[i] != apkg_guid:
            remap[rid] = resolved[i]
    return remap, len(matched), len(new_notes), new_notes, matched


def protected_for(guid, configured, per_note):
    """Field names to preserve on one note: the configured global list plus any the deck
    source has declared the learner's own on this card.

    Per-note entries are what let one figure on one card survive a sync without freezing
    that field across every card that has one.
    """
    return set(configured) | set((per_note or {}).get(guid) or ())


def merge_learner_tags(before, imported, scope_tag, markers=()):
    """A matched note's tags after an import: the source's own scope-tag subtree from
    `imported`, every tag in `before` the source does not manage (anything outside the
    scope, and the archive `markers` under it), and any other tag the source ships.
    Anki compares tags case-insensitively.

    With no scope tag nothing tells the source's tags from the learner's, so both are kept.
    """
    scope = (scope_tag or "").lower()
    marks = [m.lower() for m in markers]

    def under(tag, root):
        return tag == root or tag.startswith(root + "::")

    def managed(tag):
        low = tag.lower()
        return (bool(scope) and under(low, scope)
                and not any(under(low, m) for m in marks))

    out, seen = [], set()
    for tag in ([t for t in imported if managed(t)] + [t for t in before if not managed(t)]
                + [t for t in imported if not managed(t)]):
        if tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def per_note_for_package(per_note, matched):
    """Re-key {builder guid: [field]} onto the guids the learner's own notes carry.

    `matched` is remap_cards' own list of (rid, package guid, existing guid). A note the
    learner holds under a drifted guid is matched by front text, so a declaration keyed by
    the builder's guid would otherwise never be found. find_retired_in_collection solves
    the same problem the same way for the retired ledger.
    """
    if not per_note:
        return {}
    out = {}
    for _rid, package_guid, existing_guid in matched:
        fields = per_note.get(package_guid)
        if fields:
            out[existing_guid] = fields
    return out


def find_changed_notes(matched, details, existing_fields, protected=(), per_note=None,
                       baseline=None):
    """Which already-matched notes this .apkg would rewrite, and what they say now.

    `matched` is remap_cards' own pair list, so this never re-decides what counts as
    matched. `details` is apkg_note_details' labeled output for the same .apkg,
    `existing_fields` is {guid: {field name: value}} for the learner's notes.

    Returns {note_id: {field name: current value}}, carrying only the fields that
    actually differ, so a caller can show what a card says today next to what it is
    about to say.

    Compared by field name, never by position: index 1 is Back on a basic note and
    Prompt on an image note, so a positional comparison would report whole decks as
    changed. A field named in `protected` follows collection._restore's own rule, so
    the preview and the import agree: the import's value stands, and is shown here,
    when the learner's field still equals what `baseline` ({guid: {field: value}}) says
    the source last shipped, or, with no baseline for it, when the field is blank.
    Otherwise the restore puts the learner's value back, and nothing is shown. A field the learner's note type
    does not have is skipped too: the import's own note-type step adds a genuinely
    missing field, and until it does there is no existing value to show.

    `protected` is matched case-insensitively, the way collection.py's _note_field
    resolves the same free-text names when it snapshots and restores them. The box is
    free text and the real field names are capitalised, so an exact match here meant a
    typed "notes" listed the field as about to change while the restore quietly put it
    back: the preview and the safety net disagreeing about the same setting.

    `per_note` is threaded through the same way and for the same reason: a field the
    deck source declares protected only on this note (see protected_for) must skip here
    too, or the preview promises a change the sync then refuses to make.
    """
    by_rid = {d.get("rid"): d for d in details}
    out = {}
    # protected_for's per-note lookup is only worth redoing per note when there is a
    # per-note map to consult; otherwise every note shares the same global skip set,
    # and rebuilding it per note was 4000 set constructions on a 4000-note package
    # instead of one.
    fallback_skip = None if per_note else {str(p).lower() for p in protected}
    for rid, _apkg_guid, existing_guid in matched:
        detail = by_rid.get(rid)
        existing_value = (existing_fields or {}).get(existing_guid)
        if not detail or not existing_value:
            continue
        skip = fallback_skip if fallback_skip is not None else {
            str(p).lower() for p in protected_for(existing_guid, protected, per_note)}
        shipped = (baseline or {}).get(existing_guid, {})
        changed = {}
        for name, value in detail.get("fields", []):
            if name not in existing_value:
                continue
            if str(name).lower() in skip:
                mine = existing_value[name] or ""
                if (shipped[name] != mine) if name in shipped else mine.strip():
                    continue
            if (existing_value[name] or "").strip() != (value or "").strip():
                changed[name] = existing_value[name]
        if changed:
            out[rid] = changed
    return out


def find_duplicate_groups(existing_notes, canonical_deck_names):
    """Group the learner's notes that share a note type and front text, and for each
    group decide which copy to keep.

    `existing_notes` is a list of {guid, nid, model, front, reps, deck} for every note
    under the scope tag (collection.py's _existing_notes_summary builds this; it
    already excludes notes a previous run archived as a duplicate, so a repeat run is
    idempotent).
    `canonical_deck_names` is the manifest's current top-level deck names, used only as
    a tie breaker.

    Groups by (model, front); a group of size 1 is not a duplicate and is skipped.
    Within a group, the kept copy is the one with the most reps. Ties prefer a copy
    currently filed under one of canonical_deck_names (or a subdeck of one). Remaining
    ties prefer the lower note id, for a fully deterministic result.

    Returns one entry per duplicate group, sorted by (model, front):
        {"model": ..., "front": ..., "keep": {...}, "archive": [{...}, ...]}
    """
    groups = {}
    for note in existing_notes:
        groups.setdefault((note["model"], note["front"]), []).append(note)

    def is_canonical(note):
        d = note["deck"]
        return any(d == name or d.startswith(name + "::") for name in canonical_deck_names)

    out = []
    for (model, front), members in groups.items():
        if len(members) < 2:
            continue
        ranked = sorted(members, key=lambda n: (-n["reps"], not is_canonical(n), n["nid"]))
        out.append({
            "model": model,
            "front": front,
            "keep": ranked[0],
            "archive": ranked[1:],
        })
    out.sort(key=lambda g: (g["model"], g["front"]))
    return out


# A real tag, comment or declaration: "<" then a name, "/", "!" or "?". A "<" followed by
# anything else ("MAP <65", "1 < 2") is a literal and survives.
_TAG_RE = re.compile(r"<!--.*?-->|</?[A-Za-z][^>]*>|<[!?][^>]*>", re.S)
_IMG_SRC_RE = re.compile(r"""<img[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def plain_text(field):
    """The visible text of one card field: HTML tags stripped, entities decoded,
    whitespace collapsed. Tags become a space rather than nothing, so text either side
    of a block tag doesn't run together into one word.
    """
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", field or ""))).strip()


def truncate(text, max_len):
    """`text` capped at `max_len` characters, ending in an ellipsis when cut."""
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def note_display_label(fields, max_len=90):
    """A short, human-readable label for a note, for dialogs that list its card.

    Uses the first field whose visible text (HTML stripped, entities decoded) is
    non-empty, so a normal card shows its front and an image card whose first field is
    just an `<img>` falls through to its prompt field. If every field is non-text (a
    pure image card with no prompt), returns the first image's filename, so the line
    still says which card it is instead of rendering as a broken image. Plain text
    only, never raw HTML; long labels are truncated.
    """
    for field in fields or []:
        text = plain_text(field)
        if text:
            return truncate(text, max_len)
    for field in fields or []:
        m = _IMG_SRC_RE.search(field or "")
        if m:
            return os.path.basename(m.group(1))
    return "(card)"


_CLOZE_RE = re.compile(r"\{\{c(\d+)::([^{}]*?)(?:::([^{}]*?))?\}\}", re.S)


def cloze_filled_html(text, escape=True, mark_groups=None):
    """A cloze field as HTML with every deletion showing its answer.

    Review is for confirming the fact is right, and for a cloze the fact lives in the
    deletions, so they are filled rather than blanked. The hint half of
    {{c1::answer::hint}} follows its answer in brackets, the way Anki itself shows a
    hint while the answer is still blanked: it is the wording the learner reads on the
    question side, so a card whose hint is wrong is a card that reads wrong, and a
    review that dropped it showed nothing to check. It is styled apart from the answer
    (`.ch`, not `.cloze`) because it is the prompt rather than the fact. An empty hint
    ({{c1::answer::}}) is left off rather than rendered as empty brackets.

    The field is escaped first and the spans injected after: the other order escapes
    the spans themselves into visible markup. The
    deletion regex excludes braces from the answer, so a well-formed deletion whose
    answer contains a literal brace renders raw instead of filling; that degrades to
    visible markup rather than silently corrupting the card, which is the acceptable
    direction.

    `escape=False` is for a field that has already been through field_preview_html,
    which returns real markup: escaping again would turn that field's own tags into
    visible text, which is the whole defect that function exists to fix.

    Deletions that share a number are one card, so a note with two or more groups is
    really two or more cards sharing a field, and filling every deletion in one colour
    hides where a card ends. Each one therefore carries its group number as a small
    superscript. Default (`mark_groups=None`) adds them only when the field has more
    than one group, since labelling every blank "c1" on a single-card note is noise
    for a distinction that isn't there. Pass True or False to force it either way.
    """
    text = html.escape(text or "") if escape else (text or "")
    if mark_groups is None:
        mark_groups = len({m.group(1) for m in _CLOZE_RE.finditer(text)}) > 1

    def fill(m):
        badge = f'<sup class="cn">c{int(m.group(1))}</sup>' if mark_groups else ""
        hint = f' <span class="ch">[{m.group(3)}]</span>' if m.group(3) else ""
        return f'<span class="cloze">{m.group(2)}{badge}</span>{hint}'

    return _CLOZE_RE.sub(fill, text)


def merged_word_diff(old, new):
    """Both versions of a changed plain-text field as one word-level sequence:
    [(op, text)] segments in reading order, op one of "equal", "removed", "added".

    This is what lets the update screen show a change as a single line instead of two
    near-identical paragraphs the reader has to compare by eye. Word-level rather than
    character-level because a card edit is words: a dropped clause, a corrected value,
    a rewording. Whitespace is normalized to single spaces, which is how the rendered
    field reads anyway.

    Plain text only, by contract: the caller gates on the field carrying no markup,
    since splitting HTML on spaces would tear tags apart. Junk detection is off; a
    field is a few dozen words and difflib's popularity heuristic exists for inputs
    orders of magnitude longer, where it can silently degrade a diff this short.
    """
    a, b = (old or "").split(), (new or "").split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            out.append(("removed", " ".join(a[i1:i2])))
        if tag in ("replace", "insert"):
            out.append(("added", " ".join(b[j1:j2])))
        if tag == "equal":
            out.append(("equal", " ".join(a[i1:i2])))
    return out


def word_diff_ratio(segments):
    """How much of a merged_word_diff is unchanged text, as difflib's own 0..1 ratio
    (2 * matched words / total words on both sides).

    What the caller gates readability on: a small edit renders beautifully as one
    marked-up line, but the same treatment on a rewritten paragraph is a wall of
    struck and highlighted text that is harder to read than either version alone.
    The ratio says which of those a change is. Empty-on-both-sides reads as 1.0,
    since there is nothing to mark either way.
    """
    equal = sum(len(text.split()) for op, text in segments if op == "equal")
    changed = sum(len(text.split()) for op, text in segments if op != "equal")
    total = 2 * equal + changed
    return (2 * equal / total) if total else 1.0


def cloze_answer_changes(old, new):
    """Which deletions moved between two versions of a cloze Text field:
    (no_longer_blanked, newly_blanked) answer texts, or None when the surrounding
    words changed too.

    The filled renderings of a blanks-only change are word-for-word identical, so a
    text diff of them shows nothing and a verbatim old copy repeats the whole sentence
    for one moved blank. Naming the moved blanks is the change itself. None means the
    sentence was also reworded, where only showing the full old version is honest;
    the caller falls back to that. Both lists empty means the deletions were only
    regrouped (same answers, different numbering), which changes how the note splits
    into cards but blanks nothing new.

    Answers are compared as multisets, so a sentence blanking the same word twice
    reports a change only when a copy actually appears or disappears.
    """
    def fill(text):
        return plain_text(_CLOZE_RE.sub(lambda m: m.group(2), text or ""))

    if fill(old) != fill(new):
        return None
    old_answers = [m.group(2) for m in _CLOZE_RE.finditer(old or "")]
    new_answers = [m.group(2) for m in _CLOZE_RE.finditer(new or "")]
    removed, remaining = [], list(new_answers)
    for answer in old_answers:
        if answer in remaining:
            remaining.remove(answer)
        else:
            removed.append(answer)
    added, remaining = [], list(old_answers)
    for answer in new_answers:
        if answer in remaining:
            remaining.remove(answer)
        else:
            added.append(answer)
    return removed, added


def cloze_hint_changes(old, new):
    """Which deletions' hints moved between two versions of a cloze Text field:
    [(answer, before, after)] for every deletion whose hint was added, reworded or
    dropped, with "" on whichever side has none.

    The hint is what the learner reads while the answer is still blanked, so editing
    one changes the card even though both versions fill to word-for-word the same
    sentence. Without this, `cloze_answer_changes` sees an identical set of answers and
    the row reports a regrouping that never happened.

    Deletions pair by answer text in order of appearance, since by the time this is
    asked the filled words already match on both sides. An answer only one version has
    was blanked or unblanked rather than re-hinted, which is `cloze_answer_changes`'s
    to report, so it is skipped here rather than counted twice.
    """
    def hints(text):
        out = {}
        for m in _CLOZE_RE.finditer(text or ""):
            out.setdefault(m.group(2), []).append(m.group(3) or "")
        return out

    old_hints, new_hints = hints(old), hints(new)
    changes = []
    for answer, before in old_hints.items():
        after = new_hints.get(answer, [])
        changes.extend((answer, b, a) for b, a in zip(before, after) if b != a)
    return changes


def field_preview_text(value):
    """One card field as plain text for the review list, with any images named rather
    than rendered.

    The review dialog reads fields straight out of the .apkg and never extracts its
    media, so an <img> tag in there points at a file that isn't on disk yet: rendering
    it would paint a broken image. Naming the file instead tells the reader the card
    has a picture, which is what they actually need to know at review time. A field
    holding both text and an image reports both, since dropping either would misrepresent
    the card.
    """
    text = plain_text(render_math_spans(value, tags=False))
    names = [os.path.basename(src) for src in _IMG_SRC_RE.findall(value or "")]
    if not names:
        return text
    tag = f"[image: {', '.join(names)}]"
    return f"{text} {tag}" if text else tag


# The structure a card field actually uses, restricted to what a QLabel's rich text can
# render. A comparison is written as a <table> and a set of causes as a <ul> precisely
# because the shape carries the meaning, so flattening those to a run-on line loses the
# card. Anything outside this set has its tag dropped and its text kept.
_PREVIEW_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "s", "sub", "sup", "small", "span", "font",
    "br", "p", "div", "hr",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
})

# Attributes worth keeping: the ones carrying layout the tag can't express on its own.
# Everything else goes, so nothing a field happens to carry (ids, classes, handlers,
# stray Anki editor markup) reaches the dialog.
_PREVIEW_ATTRS = frozenset({"colspan", "rowspan", "align", "valign", "style"})

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SCRIPT_STYLE_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*?</\s*\1\s*>", re.I | re.S)
_ANY_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
_ATTR_RE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


def _named_image(match):
    src = _IMG_SRC_RE.search(match.group(0))
    name = os.path.basename(src.group(1)) if src else "image"
    return f"[image: {html.escape(name)}]"


def _clean_tag(match):
    closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
    if name not in _PREVIEW_TAGS:
        return " "
    if closing:
        return f"</{name}>"
    kept = ""
    for attr, value in _ATTR_RE.findall(attrs):
        if attr.lower() not in _PREVIEW_ATTRS:
            continue
        value = value.strip("\"'")
        kept += ' {}="{}"'.format(attr.lower(), html.escape(value, quote=True))
    return f"<{name}{kept}>"


def field_image_names(value):
    """Every picture a field references, as bare filenames in document order.

    The dialog needs the list before it renders, so it can extract exactly those files
    and nothing else.
    """
    return list(dict.fromkeys(
        os.path.basename(src) for src in _IMG_SRC_RE.findall(value or "")))


# MathJax spans in a card field (\( ... \) inline, \[ ... \] block) are typeset by
# Anki's reviewer, but a QLabel has no MathJax engine, so the raw markup reaches the
# review dialog as literal backslashes. These render the handful of constructs the
# decks actually write (\text, \frac, sub/superscripts, a few symbol macros) as
# ordinary text: not typesetting, just readable.
_MATH_SPAN_RE = re.compile(r"\\\[(.+?)\\\]|\\\((.+?)\\\)", re.DOTALL)
_MATH_SPACING_RE = re.compile(r"\\[,;:!]|\\quad\b|\\qquad\b")
_MATH_TEXT_RE = re.compile(r"\\text\s*\{([^{}]*)\}")
_MATH_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_MATH_SUB_RE = re.compile(r"_(?:\{([^{}]*)\}|([^\s{}\\]))")
_MATH_SUP_RE = re.compile(r"\^(?:\{([^{}]*)\}|([^\s{}\\]))")
_MATH_CMD_RE = re.compile(r"\\([A-Za-z]+)")
_MATH_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "approx": "≈",
    "neq": "≠", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "infty": "∞",
    "Delta": "Δ", "mu": "μ", "pi": "π",
}


def _math_frac_arg(arg):
    # A numerator or denominator holding an expression needs parens once the bar
    # becomes a slash: age/4 reads fine, but a+b/c would misplace the +.
    arg = arg.strip()
    return f"({arg})" if re.search(r"[+\-\s]", arg) else arg


def _render_one_math(body, tags):
    body = _MATH_SPACING_RE.sub(" ", body).replace(r"\%", "%")
    prev = None
    while prev != body:
        prev = body
        body = _MATH_TEXT_RE.sub(lambda m: m.group(1), body)
        body = _MATH_FRAC_RE.sub(
            lambda m: f"{_math_frac_arg(m.group(1))}/{_math_frac_arg(m.group(2))}", body)
    sub = "<sub>{}</sub>" if tags else "{}"
    sup = "<sup>{}</sup>" if tags else "{}"
    body = _MATH_SUB_RE.sub(lambda m: sub.format(m.group(1) or m.group(2)), body)
    body = _MATH_SUP_RE.sub(lambda m: sup.format(m.group(1) or m.group(2)), body)
    body = _MATH_CMD_RE.sub(lambda m: _MATH_SYMBOLS.get(m.group(1), m.group(1)), body)
    body = body.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", body).strip()


def render_math_spans(value, tags=True):
    """Replace every MathJax span in a field with a plain rendering of its formula.

    `tags` picks the output flavor: real <sub>/<sup> markup for the dialog's rich-text
    labels, or bare characters (PaCO2, Na+) for the plain-text paths, where plain_text
    would otherwise turn each stripped tag into a stray space.
    """
    if not value or "\\" not in value:
        return value
    return _MATH_SPAN_RE.sub(
        lambda m: _render_one_math(m.group(1) or m.group(2), tags), value)


def field_preview_html(value, image_html=None):
    """One card field as HTML the review dialog can render.

    field_preview_text's plain-text answer is right for the feedback digest and for a
    one-line label, and wrong for the dialog itself: a card back written as a <table>
    or a <ul> arrives as an unreadable run-on line, so the reader judges a card the
    learner has never actually seen. Real feedback came back that way ("just jumbled text to me")
    on cards whose only problem was that the preview flattened them.

    So structure is kept and everything else is dropped: script and style blocks go
    entirely, and any tag outside _PREVIEW_TAGS loses the tag but keeps its text.
    Attributes are filtered rather than passed through, so the output is a small, known
    subset rather than whatever a field happens to contain.

    `image_html`, when given, is called with one picture's bare filename and returns the
    markup to put in that <img>'s place, or None to decline. Without it, or on a decline,
    an <img> becomes "[image: name]" the same way field_preview_text names it: the caller
    that has not extracted the .apkg's media would otherwise paint a broken image, which
    is every caller except the dialog once a row is opened.
    """
    if not value:
        return ""

    value = render_math_spans(value)
    resolved_images = {}
    marker_counter = [0]

    def replace(match):
        if image_html is not None:
            names = field_image_names(match.group(0))
            rendered = image_html(names[0]) if names else None
            if rendered:
                marker = f"__RESOLVED_IMG_{marker_counter[0]}__"
                marker_counter[0] += 1
                resolved_images[marker] = rendered
                return marker
        return _named_image(match)

    text = _SCRIPT_STYLE_RE.sub(" ", value)
    text = _IMG_TAG_RE.sub(replace, text)
    text = _ANY_TAG_RE.sub(_clean_tag, text).strip()

    for marker, resolved in resolved_images.items():
        text = text.replace(marker, resolved)

    return text


_DECLINE_SNAPSHOT_GROUPS = (
    ("never", "Never imported"),
    ("frozen", "Kept yours, no more updates"),
    ("keep", "Kept yours"),
)


def _one_line(text):
    """Plain text with its whitespace folded onto one line. For a learner's note,
    which is stored as plain text already."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _decline_snapshot_lines(registry, excluded=()):
    """Render the current sparse decline registry for a feedback digest. Later cards
    are one count line, counted as later_count does (outside `excluded` decks)."""
    registry = registry if isinstance(registry, dict) else {}
    known = {state for state, _label in _DECLINE_SNAPSHOT_GROUPS}
    grouped = {state: [] for state in known}
    grouped[None] = []
    held = 0
    for guid, raw in registry.items():
        entry = raw if isinstance(raw, dict) else {}
        state = entry.get("state")
        if state == "held":
            held += 1
            continue
        state = state if isinstance(state, str) and state in known else None
        grouped[state].append((str(guid), entry))

    listed = len(registry) - held
    later = later_count(registry, excluded)
    lines = ["", f"Current standing declines ({listed})"] if listed else [""]
    for state, label in _DECLINE_SNAPSHOT_GROUPS + ((None, "Other"),):
        items = grouped[state]
        if not items:
            continue
        lines.append(f"  {label} ({len(items)})")
        by_deck = {}
        for guid, entry in items:
            deck = str(entry["deck"]) if entry.get("deck") else None
            date = str(entry.get("decided") or "")
            by_deck.setdefault(deck, {}).setdefault(date, []).append((guid, entry))
        leaf_counts = {}
        for deck in by_deck:
            if deck is not None:
                leaf = deck.split("::")[-1]
                leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
        display_names = {}
        for deck in by_deck:
            if deck is None:
                display_names[deck] = "(no deck)"
            else:
                leaf = deck.split("::")[-1]
                display_names[deck] = ascii_text(deck if leaf_counts[leaf] > 1 else leaf)
        for deck in sorted(by_deck, key=lambda name: (name is None, display_names[name],
                                                     name or "")):
            dates = by_deck[deck]
            count = sum(len(entries) for entries in dates.values())
            segments = []
            for date in sorted(dates, key=lambda value: (not value, value)):
                guids = []
                for guid, entry in sorted(dates[date], key=lambda item: item[0]):
                    if state is None:
                        guid = ascii_text(f"{entry.get('state') or 'unknown'}:") + guid
                    guids.append(guid)
                segments.append(ascii_text(f"{date or 'undated'}: ") + " ".join(guids))
            lines.append(f"    {display_names[deck]} ({count}), {'; '.join(segments)}")
    if later:
        lines.append(f"  {plural(later, 'card')} left for later")
    return lines


_ASCII_TABLE = {ord(ch): name for ch, name in _GREEK.items()}
_ASCII_TABLE.update({ord(ch.upper()): name.capitalize() for ch, name in _GREEK.items()})
_ASCII_TABLE.update({
    ord(ch): spelling
    for chars, spelling in (
        ("\u2026", "..."),
        ("\u2014\u2013\u2012\u2010\u2011\u2212\u207b\u208b", "-"),
        ("\u2018\u2019\u201b\u2032", "'"),
        ("\u201c\u201d\u201f\u2033", '"'),
        ("\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
         "\u2007\u2008\u2009\u200a\u202f\u205f", " "),
        ("\u200b", ""),
        ("\u2192", "->"), ("\u2190", "<-"), ("\u2194", "<->"),
        ("\u2191", "up"), ("\u2193", "down"),
        ("\u2265", ">="), ("\u2264", "<="), ("\u2248", "~"), ("\u2260", "!="),
        ("\u00b1", "+/-"), ("\u00d7", "x"), ("\u00f7", "/"), ("\u00b7", "."),
        ("\u00b0", " deg"), ("\u00b5", "u"),
    )
    for ch in chars
})


def ascii_text(text):
    """Spell Unicode punctuation and symbols in ASCII where a spelling exists."""
    text = unicodedata.normalize("NFC", text)
    result = []
    for ch in text.translate(_ASCII_TABLE):
        folded = "".join(c for c in unicodedata.normalize("NFKD", ch)
                         if not unicodedata.combining(c))
        result.append(folded if folded and folded.isascii() else ch)
    return "".join(result)


def build_feedback_digest(entries, version="", date="", standing_declines=None,
                          excluded=()):
    """Render flagged-card feedback as plain text, ready to paste into a message.

    `entries` is a list of {"deck", "front", "guid", "note"}, grouped here by deck in
    first-seen order. Plain text rather than HTML or JSON on purpose: this gets pasted
    into an ordinary text thread, so it has to survive being read by a person with no
    tooling. Deck headings use the leaf name, since the full "Intern Pearls::Intern
    Custom::" path is noise in a message.

    The guid line is what makes this worth more than the learner describing a card from
    memory: it names the exact spec note, so the fix doesn't start with hunting for
    which card the learner meant. A flagged card's front is HTML, so it goes through
    plain_text on the way out; notes are plain text already and only have their
    whitespace folded.

    `standing_declines`, when supplied, is the current declined-card registry. It is
    rendered after the run's entries as a complete state snapshot, with GUIDs grouped
    by deck and decision date on one line per deck per state. Content hashes stay out
    because they are sync bookkeeping rather than a learner decision. Later cards in
    the `excluded` decks are left out of its count line.

    Returns "" for no entries, so a caller can treat empty as "nothing to send" without
    a separate check.
    """
    if not entries:
        return ""
    header = "Intern Pearls card feedback"
    if date:
        header += f" ({date})"
    lines = [ascii_text(header)]
    if version:
        lines.append(ascii_text(f"Add-on v{version}"))
    by_deck = {}
    for e in entries:
        by_deck.setdefault(e.get("deck") or "", []).append(e)
    for deck, items in by_deck.items():
        lines.append("")
        lines.append(ascii_text(deck.split("::")[-1] if deck else "(unknown deck)"))
        for e in items:
            lines.append(ascii_text(f'  "{plain_text(e.get("front"))}"'))
            if e.get("decision"):
                lines.append(ascii_text(f"  decision: {e['decision']}"))
            if e.get("guid"):
                lines.append(f'  guid {e["guid"]}')
            if e.get("note"):
                lines.append(ascii_text(f'  > {_one_line(e.get("note"))}'))
            lines.append("")
    if standing_declines is not None:
        lines.extend(_decline_snapshot_lines(standing_declines, excluded))
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def duplicate_dialog_rows(groups):
    """Heading and rows for the Clean up duplicates confirmation, from
    find_duplicate_groups output.

    Each row leads with the card's readable label (the note's precomputed 'label',
    see collection._existing_notes_summary; escaped here since it's data), then says which
    copy is kept and which is archived. When every copy sits in the same deck it reads
    as a copy count rather than repeating that deck name twice.

    Returns (heading, [{"label", "detail"}]) rather than one block of HTML, because the
    confirmation draws each row as a widget. This module is pure Python by contract (no
    aqt/anki import, unit-testable with no Anki install), so it can build neither the
    widget nor the theme colour the detail is drawn in; the caller, which already knows
    both, does that.
    """
    lines = []
    for g in groups:
        label = html.escape(g["keep"].get("label") or g["front"])
        keep_leaf = g["keep"]["deck"].split("::")[-1]
        arch = g["archive"]
        arch_leaves = [a["deck"].split("::")[-1] for a in arch]
        # One archived copy reads as its own count; several read as a joined list under
        # a single plural, since "3 reviews, 5 reviews" says the same thing twice.
        arch_reps = (plural(arch[0]["reps"], "review") if len(arch) == 1
                     else ", ".join(str(a["reps"]) for a in arch) + " reviews")
        if all(leaf == keep_leaf for leaf in arch_leaves):
            detail = (f"{1 + len(arch)} copies in {html.escape(keep_leaf)}: keeping the "
                      f"one with {plural(g['keep']['reps'], 'review')}, archiving "
                      f"{len(arch)} ({arch_reps})")
        else:
            detail = (f"keeping {html.escape(keep_leaf)} "
                      f"({plural(g['keep']['reps'], 'review')}), "
                      f"archiving {html.escape(', '.join(arch_leaves))} "
                      f"({arch_reps})")
        lines.append({"label": label, "detail": detail})
    n_archive = sum(len(g["archive"]) for g in groups)
    n_cards = len(groups)
    copies = "copy" if n_archive == 1 else "copies"
    cards = "card" if n_cards == 1 else "cards"
    heading = (f"Found <b>{n_archive}</b> duplicate {copies} of <b>{n_cards}</b> {cards}. "
               "Each card was imported more than once. Archiving keeps one copy of each:")
    return heading, lines


def select_empty_cards(report_notes, scoped_nids):
    """Split an empty-cards report down to the cards this add-on is willing to remove.

    An empty card is one whose template renders nothing, which for a cloze note means a
    card for a deletion number the note's text no longer contains (Anki shows it in
    review as "No cloze 3 found on card"). They appear when a deck source regroups a
    live cloze into fewer deletions: an import rewrites a matched note's fields but
    never removes its cards, so the surplus cards stay behind with nothing to render.

    Two filters, both load-bearing:

    - Only notes in `scoped_nids` (the learner's notes under the configured scope tag).
      Anki's own report covers the whole collection, and other people's decks are not
      ours to clean up.
    - A note whose cards are ALL empty is never touched, only reported. That is the one
      case where removing the cards would take the note and its content with it, and it
      means something is wrong upstream (a note with no deletions at all), not that
      there is a card to tidy away.

    Returns (removable, skipped), each a list of {"nid", "card_ids"}.
    """
    removable, skipped = [], []
    for n in report_notes:
        if n["nid"] not in scoped_nids:
            continue
        entry = {"nid": n["nid"], "card_ids": list(n["card_ids"])}
        (skipped if n.get("will_delete_note") else removable).append(entry)
    return removable, skipped


def empty_cards_dialog_rows(rows, skipped=0):
    """Heading, rows and closing note for the Remove empty cards confirmation, from
    collection.find_empty_cards.

    Each row names the card the way the rest of the add-on labels one, and carries the
    deletion numbers that went missing, since "c3, c4" is what the learner actually sees
    on the dead card in review.

    Returns (heading, [{"label", "gone"}], tail), structured for the same reason
    duplicate_dialog_rows is: the confirmation draws a widget per row, and this module
    stays free of both Qt and the live theme.
    """
    lines = []
    for r in rows:
        lines.append({"label": html.escape(r.get("label") or ""),
                      "gone": ", ".join(f"c{o}" for o in r.get("ords", []))})
    n_cards = sum(len(r["card_ids"]) for r in rows)
    cards = "card" if n_cards == 1 else "cards"
    notes = "note" if len(rows) == 1 else "notes"
    heading = (f"Found <b>{n_cards}</b> empty {cards} on <b>{len(rows)}</b> {notes}. "
               "These are leftovers from a card that used to have more blanks than it "
               "does now, so there is nothing left for them to show:")
    tail = ""
    if skipped:
        tail = (f"<b>{plural(skipped, 'note')}</b> "
                f"{'has' if skipped == 1 else 'have'} no content on any card at all and "
                f"{'was' if skipped == 1 else 'were'} left alone, since removing those "
                "cards would delete the note itself.")
    return heading, lines, tail


def feedback_entries(flags, index, decisions=None):
    """The flagged cards as digest entries: [{deck, front, guid, note}].

    `flags` is {guid: the learner's note}; `index` is {guid: (deck, front)}, which is
    what turns a GUID into something a person can read. A flag whose GUID isn't in the
    index is dropped rather than shown as a bare GUID: it means we no longer know which
    card the learner meant, and a line naming no card is not something anyone can act on.

    `decisions`, when given, is {guid: reader-facing state} ("kept yours"/"kept yours,
    no more updates"/"never"/"imported after all") for a decision made this run; a guid
    present there but with no note still gets an entry (empty "note"), carrying the
    "decision" key the digest renders.
    """
    decisions = decisions or {}
    out = []
    for g in dict.fromkeys(list(flags) + list(decisions)):
        if g not in index:
            continue
        out.append({"deck": index[g][0], "front": index[g][1], "guid": g,
                    "note": flags.get(g, ""),
                    **({"decision": decisions[g]} if g in decisions else {})})
    return out


def merge_saved_feedback(saved, flags, index):
    """Fold notes recovered from disk into this run's flags and card index, in place.

    A saved note is only restored for a card this run hasn't already collected a note
    on: what the learner just typed is newer than what a previous run left behind, so
    the live value wins on a conflict. The index gets the saved deck/front for every restored
    card, since the card may well not be in this run at all (its deck already imported
    last time), and without a name it would be dropped from the digest.

    Returns the number of notes restored, so the caller can say so.
    """
    restored = 0
    for guid, entry in (saved or {}).items():
        note = (entry or {}).get("note", "").strip()
        if not note or guid in flags:
            continue
        flags[guid] = note
        index.setdefault(guid, (entry.get("deck", ""), entry.get("front", guid)))
        restored += 1
    return restored
