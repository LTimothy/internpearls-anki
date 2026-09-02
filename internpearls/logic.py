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
import sqlite3
import tempfile
import zipfile

from .ai_logic import is_generated_guid

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
    """The one CSS source for Night Mode Dimming. "images" dims bright images
    only (the original behaviour); "content" dims the whole web view body,
    which is every card, the deck list, the overview, and the editor. Anki adds
    the nightMode class to body only in Night Mode, so neither rule ever
    applies in Day mode. Unknown scope, or disabled, is no CSS at all."""
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
    now; the caller asks before applying anything.
    """
    def pick(key, current):
        v = (manifest or {}).get(key)
        return v if isinstance(v, str) and v and v != current else None

    return pick("scope_tag", scope_tag), pick("export_deck", export_deck)


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


def decks_to_update(manifest, installed, excluded=None):
    """Decks from the manifest whose version differs from what's already installed.

    `installed` is {deck_name: version_last_applied}. A deck missing from it is new; a
    deck whose version changed needs re-sync; matching versions are skipped. `excluded`
    is an optional collection of deck names the user has opted out of syncing (from the
    deck manager) — those are skipped regardless of version. Shared by Sync (to know what
    to apply) and Preview sync (to report the same set without touching the collection),
    so the two can never disagree about what's pending.

    An entry missing `name` or `version`, or a non-dict entry, is skipped rather than
    raising: without a name there is nothing to fetch or file cards under, and without a
    version there is nothing to compare against installed.json. One malformed row must
    not stop every other deck in the manifest from syncing.
    """
    excluded = set(excluded or ())
    out = []
    for d in (manifest or {}).get("decks", []):
        if not isinstance(d, dict):
            continue
        name, version = d.get("name"), d.get("version")
        if not name or version is None or name in excluded:
            continue
        if installed.get(name) != version:
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
    except (TypeError, ValueError):
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


def find_deck_moves_needed(moves_ledger, her_guid_to_deck, her_front_to_guid=None):
    """Which of the learner's cards need to move deck to match a pure reorg.

    `moves_ledger` is {guid: {from, to, front?}}: every note the deck repo has ever
    relocated without changing its GUID (see build_all.py's deck_moves.json). `front`,
    when present, is the note's current first field, used to find a learner's card
    even when its GUID no longer matches the ledger's (see below).
    `her_guid_to_deck` is {guid: current deck name} for her collection.
    `her_front_to_guid` is {first field: guid} for her collection (optional).

    Normally a card is matched to a ledger entry by GUID. But a card whose deck source
    changed its `id_seed` (say a deck's seed moving from v1 to v2) has a *different*
    GUID in a learner's older collection than the one the ledger is keyed by, so a
    pure GUID match misses it: the card sits stuck at `from` forever, its new deck
    perpetually re-offered because installed_matching_collection never finds a card
    under it. So when the ledger GUID isn't in her collection, fall back to matching by
    `front` (the same signal content-sync's remap_cards trusts; fronts are unique
    across decks by build lint), and act on *her* GUID for that front. An older
    manifest without `front`, or a caller that passes no `her_front_to_guid`, simply
    keeps the GUID-only behavior.

    A move only applies if her card is still sitting exactly where the deck source
    last put it (`from`). If it's anywhere else (already at `to` because she reconciled
    a previous move, or somewhere of her own choosing because she filed it into a
    custom deck), leave it alone. This is what makes reconciling deck moves both
    idempotent (nothing to do once she's there) and non-destructive of her own
    organization (a deliberate move away from `from` is never overwritten).

    Returns [{guid, from, to}] where `guid` is *her* note's GUID (so apply_deck_moves
    can find it), sorted by `to` then `from` for stable display.
    """
    out = []
    for guid, move in (moves_ledger or {}).items():
        her_guid = guid if guid in her_guid_to_deck else None
        if her_guid is None and her_front_to_guid and move.get("front"):
            her_guid = her_front_to_guid.get(move["front"])
        if her_guid is not None and her_guid_to_deck.get(her_guid) == move.get("from"):
            out.append({"guid": her_guid, "from": move["from"], "to": move["to"]})
    out.sort(key=lambda m: (m["to"], m["from"]))
    return out


def fields_to_carry_over(saved, target_current):
    """Which of a retired note's protected-field values to copy onto one of its
    replacement notes.

    `saved` is {field: value} read off the note being retired; `target_current` is
    the same shape for the replacement. Never overwrites a field the replacement
    already has text in — she may have already started annotating it herself, or a
    previous partial run already carried a value over — so this only ever fills in
    a field that's currently blank.
    """
    return {f: v for f, v in saved.items()
            if v.strip() and not (target_current.get(f) or "").strip()}


def find_retired_in_collection(retired_ledger, her_guids, her_front_to_guid=None):
    """The retired cards a learner still has in her collection.

    When a deck splits, merges, or drops a card, the old card's GUID leaves the
    canonical set but her copy of it is never touched by a sync (sync only ever adds
    the replacements), so it lingers in her reviews as a duplicate. The deck repo
    records every such retirement in `retired.json`, shipped to us inside the manifest.

    `retired_ledger` is that ledger: {deck_name: {guid: {identity, reason,
    superseded_by, ...}}}. `her_guids` is the set of note GUIDs she has under the scope
    tag. `her_front_to_guid` is {first field: guid} for those same notes (optional).

    Normally a retired card is matched by GUID. But a learner whose copy predates the
    identity the ledger is keyed by (the card was reworded between her import and the
    GUID freeze, or its deck source changed `id_seed`) holds a *different* GUID, so a
    pure GUID match misses her copy and the retired card lingers in her reviews
    forever, with nothing to signal it. So when the ledger GUID isn't in her
    collection, fall back to matching by front text (the same signal content-sync's
    remap_cards trusts; fronts are unique across decks by build lint) and report *her*
    GUID for it. The front compared is the entry's own `front` if the ledger records
    one, else its `identity`, which for a basic or cloze note is exactly the front.
    Two kinds of entry therefore keep GUID-only behaviour, both by simply not matching
    rather than by matching something wrong: an image note (identity is
    "image||answer", never a first field), and a card whose front was reworded under a
    frozen `id` before it was retired (identity is the pre-reword wording). Recording
    `front` at retirement time closes that second gap without another release here.

    Returns one dict per retired card she still has, so the reconcile flow can show
    and archive them:
        {guid, deck, identity, reason, superseded_by, replacements_present}
    where `guid` is HER note's GUID. `replacements_present` is how many of
    `superseded_by` are already in her collection, so the UI can distinguish "replaced
    by cards you already have" from "sync first to get the replacements". It stays a
    GUID-only count: a collection whose GUIDs have drifted reads 0 and gets the
    advisory "sync first" note, which is a cosmetic miss, not a wrong archive. Sorted
    by deck then identity for stable display. Pure: the caller supplies the collection
    maps and does anything collection-touching (tag checks, the archive itself).
    """
    her_guids = set(her_guids)
    out = []
    for deck, entries in (retired_ledger or {}).items():
        for guid, info in (entries or {}).items():
            her_guid = guid if guid in her_guids else None
            if her_guid is None and her_front_to_guid:
                front = info.get("front") or info.get("identity") or ""
                her_guid = her_front_to_guid.get(front) if front else None
            if her_guid is None:
                continue
            sup = list(info.get("superseded_by") or [])
            out.append({
                "guid": her_guid,
                "deck": deck,
                "identity": info.get("identity", ""),
                "reason": info.get("reason", ""),
                "superseded_by": sup,
                "replacements_present": sum(1 for g in sup if g in her_guids),
            })
    out.sort(key=lambda r: (r["deck"], r["identity"]))
    return out


def find_stranded_pairs(superseded, her_front_to_guid):
    """Pairs where the learner holds BOTH wordings of a card that was reworded once.

    Rewording a front freezes the old wording as the note's `id`, which keeps the GUID
    stable so the reword updates a learner's card in place. That works for anyone whose
    GUID matches. For anyone whose GUID drifted first, the reword matched nothing and
    imported as a second note, leaving her with the dead wording (carrying whatever
    review history she had built) sitting beside the live one (starting from zero).
    Neither copy alone is right: the old one has her progress but stale content, the new
    one has current content but no progress.

    `superseded` is the manifest's {superseded front: its replacement}, derived by the
    deck source from those same `id` freezes, so the pairing is the deck author's own
    declaration that the two wordings are one card rather than a guess made here.
    `her_front_to_guid` is {first field: guid} for her collection.

    Only pairs where she holds both are returned. Holding just the old wording is not a
    stranding: the import's own front matching merges it, which is the whole point of
    that ladder, and acting here too would fight it. Returns
    [{guid, front, successor_guid, successor_front}] sorted by front for stable display;
    `guid` is the predecessor, the copy that gets emptied out and archived.
    """
    out = []
    for old_front, new_front in (superseded or {}).items():
        old_guid = (her_front_to_guid or {}).get(old_front)
        new_guid = (her_front_to_guid or {}).get(new_front)
        if old_guid and new_guid and old_guid != new_guid:
            out.append({"guid": old_guid, "front": old_front,
                        "successor_guid": new_guid, "successor_front": new_front})
    out.sort(key=lambda p: p["front"])
    return out


@contextlib.contextmanager
def _apkg_db(path):
    """Yield (open sqlite connection, is_newer_format) for an .apkg's real collection.

    A package Anki exports today holds its data in a zstd-compressed collection.anki21b
    and ships a near-empty collection.anki2 stub beside it, carrying one placeholder note
    that reads "Please update to the latest Anki version". So the newer member has to
    win, or every reader here sees that stub instead of the deck: the visible symptom is
    an import that matches nothing, imports every card as new, and leaves the
    protected-field restore with no matched note to restore onto.

    zstandard is not stdlib and Anki does not ship it, so on a modern package the
    decode is impossible here and the reader stops with NEWER_APKG_ERROR: a loud
    "re-export this file" beats a silent empty result.
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        newer = "collection.anki21b" in names
        member = "collection.anki21b" if newer else "collection.anki2"
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
                    zstandard.ZstdDecompressor().copy_stream(fh, out)
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
        with zipfile.ZipFile(path) as z:
            if "media" not in z.namelist():
                return {}
            entries = json.loads(z.read("media").decode("utf8"))
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeDecodeError):
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
    except (OSError, zipfile.BadZipFile):
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


def plan_notetype_changes(incoming_types, her_types, managed):
    """Which of the learner's notes have to change note type for this update to land on
    them instead of beside them.

    Converting a Q&A card to a cloze changes its note type, and Anki's importer will not
    move an existing note to a different one. Without this the incoming note imports
    fresh and she keeps a stale duplicate holding all her history, which is why such a
    conversion has always had to retire the old card and restart the new one from zero.

    Both arguments are keyed by HER note guid: `incoming_types` is what this update
    would make each matched note (the caller resolves the .apkg's own guids through
    remap_cards first, so a note matched by front counts too), `her_types` is what they
    are now. A change is planned only when both names are in `managed`, so a learner's
    own note types are never touched and an unrecognised incoming type is left alone.

    Returns [{guid, old, new}] sorted by guid, for a caller that asks permission first:
    Anki treats this as a schema change, meaning a one-time full AnkiWeb sync.
    """
    out = []
    for guid, new in (incoming_types or {}).items():
        old = (her_types or {}).get(guid)
        if old is None:
            continue
        old_base, new_base = base_notetype_name(old), base_notetype_name(new)
        if (old_base == new_base or old_base not in managed
                or new_base not in managed):
            continue
        out.append({"guid": guid, "old": old, "new": new})
    out.sort(key=lambda c: c["guid"])
    return out


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
    """The manifest's notes describing exactly this incoming content: entries under
    `guid` whose hash matches these field values, newest first. A hash mismatch means
    the note was written about some other version of the card (a stale cached
    manifest, usually), and captioning content a note does not describe is worse than
    showing nothing."""
    entries = manifest_notes.get(guid) if isinstance(manifest_notes, dict) else None
    if not isinstance(entries, list):
        return []
    h = note_fields_hash(list(fields))
    return [e for e in reversed(entries)
            if isinstance(e, dict) and isinstance(e.get("note"), str)
            and e.get("note") and e.get("hash") == h]


SOURCE_LABEL_MAX = 120


def source_label_for(manifest_sources, guid):
    """A deck source's own short label for where a card came from, e.g. `[T10Q2]`, or
    "" when it ships none.

    Opaque on purpose. The string is built by whoever publishes the deck, out of
    whatever a card's provenance means for that deck, and nothing here parses it or
    assumes a shape. Unlike a change note it carries no claim about why the card
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


def prune_declined(reg, retired_guids, seen):
    """Drop registry entries that are moot: the note was retired upstream, or it is
    gone from its deck's current package. `seen` covers only decks actually
    downloaded this run, so an entry for a deck not in `seen` is never judged for
    absence. A hand-edited entry that isn't even a dict is left alone unless its
    guid is retired: there's no "deck" to check it against, and a bad entry must
    degrade gracefully rather than crash a sync. Mutates `reg`; returns whether
    anything was removed."""
    dead = [g for g, e in reg.items()
            if g in retired_guids
            or (isinstance(e, dict) and e.get("deck") in seen and g not in seen[e["deck"]])]
    for g in dead:
        del reg[g]
    return bool(dead)


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


def declined_drop(src, remap, her, declined, in_place, as_new):
    """The rids to drop for a decline, plus `touched` and `in_place`/`as_new`
    corrected to exclude them. `remap` is mutated in place (a dropped note's remap
    entry removed), so a drop always wins over a remap for the same note.

    Shared by collection._apply_deck and sync.import_single, so a decline filters
    identically whichever path a deck lands in the collection through."""
    drop, touched = set(), set()
    for rid, _f, guid in apkg_notes(src):
        final = remap.get(rid, guid)
        if final in declined or guid in declined:
            drop.add(rid)
            if remap.pop(rid, None) is not None or guid in her.values():
                in_place -= 1
            else:
                as_new -= 1
        else:
            touched.add(final)
    return drop, touched, in_place, as_new


def write_personalized(src, remap, out, drop=frozenset()):
    """Copy the .apkg at `src` to `out`, rewriting note GUIDs per `remap` and dropping declined notes.

    `remap` is {note_id: new_guid}. Notes not in `remap` are left untouched.
    `drop` is a set of note ids to remove entirely, notes and their cards rows both;
    a drop always wins over a remap for the same id.

    Legacy format only, and it refuses a newer one rather than doing nothing quietly.
    Anki reads a modern package's collection.anki21b, so rewriting the collection.anki2
    stub beside it would apply no guid at all while every caller went on reporting the
    matched counts remap_cards computed: every front-matched card would import as a
    duplicate and the learner's history would stay on the copy she already had. Writing
    the anki21b back needs zstd compression, which nothing here has, so the honest answer
    is the re-export instruction.
    """
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(src) as z:
            if "collection.anki21b" in z.namelist():
                raise RuntimeError(NEWER_APKG_ERROR)
            z.extractall(d)
        con = sqlite3.connect(os.path.join(d, "collection.anki2"))
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
        con.commit()
        con.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))


def remap_cards(src, her, aliases):
    """Match each note in the .apkg at `src` to one of the learner's existing cards.

    Matching order, strongest signal first:
      1. GUID: the incoming note's GUID already belongs to one of her cards. This is
         the durable path — deck specs give every card an explicit stable `id`, so a
         reworded front no longer changes the GUID and needs no alias at all.
      2. Front text: her card currently shows this exact front (`her` is
         {front_text: guid}). Covers collections whose GUIDs predate stable ids.
      3. `aliases` ({current_front: previous_front}): her card still shows the one
         prior wording of a renamed front.

    Returns (remap, in_place, as_new, new_notes, matched): `remap` is {note_id: guid} for
    notes whose GUID needs rewriting to match an existing card, `in_place`/`as_new` are
    counts for the confirmation dialogs. A GUID match needs no rewrite, so it never lands
    in `remap`.

    `new_notes` is [(note_id, fields, guid), ...] for the notes that will import as new,
    in the .apkg's own order, and `as_new` is exactly its length. `matched` is the
    complement, [(note_id, apkg_guid, her_guid), ...], where her_guid is the .apkg's own
    guid on a GUID match. Both are returned from here rather than re-derived by a second
    function so the matching ladder above stays the single source of truth about what
    "new" and "already hers" mean: a separate implementation would eventually disagree
    with this one, and the visible symptom would be a preview that lies to the learner
    about which cards are about to appear or change.
    """
    remap, in_place, new_notes, matched = {}, 0, [], []
    her_guids = set(her.values())
    for rid, fields, apkg_guid in apkg_notes(src):
        if apkg_guid in her_guids:
            in_place += 1
            matched.append((rid, apkg_guid, apkg_guid))
            continue
        front = fields[0] if fields else ""
        her_guid = her.get(front)
        if her_guid is None and front in aliases:
            her_guid = her.get(aliases[front])
        if is_generated_guid(her_guid):
            her_guid = None   # locally generated cards are invisible to sync matching
        if her_guid is None:
            new_notes.append((rid, fields, apkg_guid))
        else:
            in_place += 1
            matched.append((rid, apkg_guid, her_guid))
            if her_guid != apkg_guid:
                remap[rid] = her_guid
    return remap, in_place, len(new_notes), new_notes, matched


def find_changed_notes(matched, details, her_fields, protected=()):
    """Which already-matched notes this .apkg would rewrite, and what they say now.

    `matched` is remap_cards' own pair list, so this never re-decides what counts as
    matched. `details` is apkg_note_details' labeled output for the same .apkg,
    `her_fields` is {guid: {field name: value}} for the learner's notes.

    Returns {note_id: {field name: her current value}}, carrying only the fields that
    actually differ, so a caller can show what a card says today next to what it is
    about to say.

    Compared by field name, never by position: index 1 is Back on a basic note and
    Prompt on an image note, so a positional comparison would report whole decks as
    changed. Fields named in `protected` are skipped, since those hold the learner's own
    annotations and every spec ships them empty. A field her note type does not have is
    skipped too: the import's own note-type step adds a genuinely missing field, and
    until it does there is nothing of hers to show.

    `protected` is matched case-insensitively, the way collection.py's _note_field
    resolves the same free-text names when it snapshots and restores them. The box is
    free text and the real field names are capitalised, so an exact match here meant a
    typed "notes" listed the field as about to change while the restore quietly put it
    back: the preview and the safety net disagreeing about the same setting.
    """
    skip = {str(p).lower() for p in (protected or ())}
    by_rid = {d.get("rid"): d for d in details}
    out = {}
    for rid, _apkg_guid, her_guid in matched:
        detail = by_rid.get(rid)
        hers = (her_fields or {}).get(her_guid)
        if not detail or not hers:
            continue
        changed = {}
        for name, value in detail.get("fields", []):
            if str(name).lower() in skip or name not in hers:
                continue
            if (hers[name] or "").strip() != (value or "").strip():
                changed[name] = hers[name]
        if changed:
            out[rid] = changed
    return out


def find_duplicate_groups(her_notes, canonical_deck_names):
    """Group the learner's notes that share a note type and front text, and for each
    group decide which copy to keep.

    `her_notes` is a list of {guid, nid, model, front, reps, deck} for every note under
    the scope tag (collection.py's _her_notes_summary builds this; it already excludes
    notes a previous run archived as a duplicate, so a repeat run is idempotent).
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
    for note in her_notes:
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


_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r"""<img[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def plain_text(field):
    """The visible text of one card field: HTML tags stripped, entities decoded,
    whitespace collapsed. Tags become a space rather than nothing, so text either side
    of a block tag doesn't run together into one word.
    """
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", field or ""))).strip()


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
            return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"
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
    or a <ul> arrives as an unreadable run-on line, so the reader judges a card she has
    never actually seen. Real feedback came back that way ("just jumbled text to me")
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


def build_feedback_digest(entries, version="", date=""):
    """Render flagged-card feedback as plain text, ready to paste into a message.

    `entries` is a list of {"deck", "front", "guid", "note"}, grouped here by deck in
    first-seen order. Plain text rather than HTML or JSON on purpose: this gets pasted
    into an ordinary text thread, so it has to survive being read by a person with no
    tooling. Deck headings use the leaf name, since the full "Intern Pearls::Intern
    Custom::" path is noise in a message.

    The guid line is what makes this worth more than the learner describing a card from
    memory: it names the exact spec note, so the fix doesn't start with hunting for
    which card she meant. Fronts are stored as HTML, so they go through plain_text on
    the way out.

    Returns "" for no entries, so a caller can treat empty as "nothing to send" without
    a separate check.
    """
    if not entries:
        return ""
    header = "Intern Pearls card feedback"
    if date:
        header += f" ({date})"
    lines = [header]
    if version:
        lines.append(f"Add-on v{version}")
    by_deck = {}
    for e in entries:
        by_deck.setdefault(e.get("deck") or "", []).append(e)
    for deck, items in by_deck.items():
        lines.append("")
        lines.append(deck.split("::")[-1] if deck else "(unknown deck)")
        for e in items:
            lines.append(f'  "{plain_text(e.get("front"))}"')
            if e.get("decision"):
                lines.append(f"  decision: {e['decision']}")
            if e.get("guid"):
                lines.append(f'  guid {e["guid"]}')
            if e.get("note"):
                lines.append(f'  > {plain_text(e.get("note"))}')
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def duplicate_dialog_rows(groups):
    """Heading and rows for the Clean up duplicates confirmation, from
    find_duplicate_groups output.

    Each row leads with the card's readable label (the note's precomputed 'label',
    see collection._her_notes_summary; escaped here since it's data), then says which
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
    deletion numbers that went missing, since "c3, c4" is what she actually sees on the
    dead card in review.

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

    `flags` is {guid: her note}; `index` is {guid: (deck, front)}, which is what turns a
    GUID into something a person can read. A flag whose GUID isn't in the index is
    dropped rather than shown as a bare GUID: it means we no longer know which card she
    meant, and a line naming no card is not something anyone can act on.

    `decisions`, when given, is {guid: reader-facing state} ("skipped"/"kept yours"/
    "never"/"imported after all") for a decision made this run; a guid present there
    but with no note still gets an entry (empty "note"), carrying the "decision" key
    the digest renders.
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
    on: what she just typed is newer than what a previous run left behind, so the live
    value wins on a conflict. The index gets the saved deck/front for every restored
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
