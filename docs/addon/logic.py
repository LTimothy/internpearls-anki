"""Pure logic for Intern Pearls Deck Tools.

Nothing here imports aqt or anki, so it runs under plain pytest. A function that needs mw or
col belongs in another module."""
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
from .dupes import _GREEK, _TAG_RE

FS = "\x1f"   # Anki's field separator inside a note's flds column

NEWER_APKG_ERROR = ('This .apkg uses Anki\'s newer export format. Re-export it with '
                    '"Support older Anki versions" ticked and try again.')


def plural(count, noun):
    """A count and its noun, agreeing: "1 card", "3 cards", "0 cards". Only handles nouns
    that take a plain "s"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def clamp_night_mode_dim_percent(percent, floor_percent=0, default_percent=30,
                                 ceiling_percent=90):
    """Sanitize a night-mode image-dim percentage: a missing or non-numeric value becomes
    `default_percent`, and the result is clamped to `floor_percent`..`ceiling_percent`.

    `int()` raises OverflowError, not ValueError, on a non-finite float, and `json` parses
    Infinity from a hand-edited config, so both are caught."""
    try:
        p = int(percent)
    except (TypeError, ValueError, OverflowError):
        p = default_percent
    return min(ceiling_percent, max(floor_percent, p))


def night_mode_dim_factor(percent):
    """The 0..1 brightness multiplier night_mode_css applies for a dim percent: 0 -> 1.0,
    the default 30 -> 0.7. Clamps the percent first, so a raw config value is safe to pass.
    The dimming preview uses it too."""
    percent = clamp_night_mode_dim_percent(percent)
    return round(1 - percent / 100, 2)


NIGHT_MODE_SCOPES = ("images", "content")


def night_mode_css(percent, scope="images"):
    """The CSS for Night mode dimming. "images" dims images only; "content" dims the whole
    web view. Unknown scope is no CSS. Both rules need Anki's night mode classes and the
    ip-dim class night_mode_dim_script sets. "content" filters html, not body: a filter on
    body would pin other add-ons' position: fixed backgrounds to the card box.

    "images" returns a wrapped `<style>` block, since the card hook appends it to card HTML;
    "content" returns bare CSS, since the web view hook wraps it itself."""
    if scope not in NIGHT_MODE_SCOPES:
        return ""
    factor = night_mode_dim_factor(percent)
    if scope == "content":
        return f"html.night-mode.ip-dim {{ filter: brightness({factor:.2f}); }}"
    # Dims rather than inverts: an inverted photo looks wrong among diagrams.
    return ("<style>html.ip-dim .nightMode img {{ filter: brightness({b:g}) "
            "contrast(0.92); }}</style>").format(b=factor)


def clamp_hour(hour, default):
    """A configured hour of the day, 0-23; anything else is `default`."""
    try:
        h = int(hour)
    except (TypeError, ValueError, OverflowError):
        return default
    return h if 0 <= h <= 23 else default


def night_mode_dim_args(on, scheduled, start_hour, end_hour):
    """The arguments ipDim.set takes, as JS source."""
    return (f"{'true' if on else 'false'},{'true' if scheduled else 'false'},"
            f"{int(start_hour)},{int(end_hour)}")


def night_mode_dim_script(args):
    """Head script that sets the ip-dim class the dimming rules need. With the
    schedule on, only from start_hour up to end_hour (wrapping past midnight;
    equal hours mean all day), rechecked every minute. ipDim.set lets the
    add-on flip a screen that is already open."""
    return ("<script>window.ipDim={set:function(on,sched,from,to){"
            "ipDim.v={on:on,sched:sched,from:from,to:to};ipDim.apply()},"
            "apply:function(){var v=ipDim.v,h=new Date().getHours(),"
            "inHours=!v.sched||v.from===v.to||(v.from<v.to?h>=v.from&&h<v.to:"
            "h>=v.from||h<v.to);"
            "document.documentElement.classList.toggle('ip-dim',v.on&&inHours)}};"
            f"ipDim.set({args});setInterval(ipDim.apply,60000);</script>")


def version_tuple(v):
    """Parse a version string into a tuple of ints, e.g. "0.10.2" -> (0, 10, 2)."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def version_at_least(current, latest):
    """True if `latest` is not newer than `current`. The shorter tuple is zero-padded, so
    "0.5" equals "0.5.0"."""
    cur_n, latest_n = version_tuple(current), version_tuple(latest)
    width = max(len(cur_n), len(latest_n))
    cur_n = cur_n + (0,) * (width - len(cur_n))
    latest_n = latest_n + (0,) * (width - len(latest_n))
    return latest_n <= cur_n


def manifest_needs_newer_addon(manifest, supported_schema):
    """True if this manifest's `schema` int is newer than this add-on understands. A missing
    `schema` reads as 1. A `schema` that is not a plain int counts as newer."""
    if not manifest:
        return False
    schema = manifest.get("schema", 1)
    if not isinstance(schema, int) or isinstance(schema, bool):
        return True
    return schema > supported_schema


def manifest_scope_suggestion(manifest, scope_tag, export_deck):
    """(suggested scope_tag, suggested export_deck), each None when there is nothing to
    offer.

    A manifest may carry the author's own `scope_tag` and `export_deck`. A value is
    suggested only when it is a non-empty string that differs from the configured one; the
    caller asks before applying it. A scope tag is quoted into every collection search, so
    one holding a quote, whitespace or `*` is never offered."""
    def pick(key, current):
        v = (manifest or {}).get(key)
        return v if isinstance(v, str) and v and v != current else None

    tag = pick("scope_tag", scope_tag)
    if tag and re.search(r"[\s\"'*]", tag):
        tag = None
    return tag, pick("export_deck", export_deck)


def scope_search(tag, exclude_tag=None):
    """The Anki search for notes under `tag` or its `::` children, less any under
    `exclude_tag`, or "" (every note) when there is no tag. Anki reads `_` in a search
    as any one character and `*` as any run, so both are escaped, as are a backslash
    and a quote, or `My_Deck` would also select `MyXDeck`."""
    def esc(t):
        for ch in ("\\", '"', "*", "_"):
            t = t.replace(ch, "\\" + ch)
        return t

    if not tag:
        return ""
    search = f'"tag:{esc(tag)}" OR "tag:{esc(tag)}::*"'
    return f'({search}) -"tag:{esc(exclude_tag)}"' if exclude_tag else search


def parse_fields(text, default=("Notes",)):
    """Parse the comma-separated "preserved fields" box into a list: trimmed, empties
    dropped, de-duplicated in order. Returns `default` if nothing usable is left."""
    out = []
    for f in (text or "").split(","):
        f = f.strip()
        if f and f not in out:
            out.append(f)
    return out or list(default)


def decks_to_update(manifest, installed, excluded=None, held=None):
    """Decks from the manifest whose version differs from what is installed.

    `installed` is {deck_name: version_last_applied}; a deck missing from it is new.
    `excluded` is deck names the user opted out of syncing. `held` is deck names holding
    cards set aside with "hold for later"; those count as pending even at a matching
    version. Auto-sync never passes it.

    A non-dict entry, or one missing `name` or `version`, is skipped."""
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
    """One row per available deck for the deck manager: dicts with the deck's full `name`, a
    short display label, its `cards` count, whether it is `enabled`, and a `state` of "new"
    (never synced), "update" (a newer version is available) or "current".

    A non-dict entry, or one missing `name`, is skipped."""
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
    """Whether the startup check should show an "update available" notice: True only if
    `latest` is strictly newer than `current` and no notice for `latest` or anything newer
    has been shown. A missing or blank `latest` returns False."""
    if not latest:
        return False
    if version_at_least(current, latest):          # current already >= latest
        return False
    if last_notified and version_at_least(last_notified, latest):
        return False                               # already told them about this one
    return True


def clamp_interval_minutes(minutes, floor_minutes=1, default_minutes=15,
                           ceiling_minutes=7 * 24 * 60):
    """Sanitize a poll interval: a missing or non-numeric value becomes `default_minutes`,
    and the result is clamped to `floor_minutes`..`ceiling_minutes`. The floor prevents a
    busy poll; the ceiling keeps the QTimer interval, a C int of milliseconds, from
    overflowing."""
    try:
        m = int(minutes)
    except (TypeError, ValueError, OverflowError):
        m = default_minutes
    return min(ceiling_minutes, max(floor_minutes, m))


def decide_addon_update_action(current, latest, auto_update, notify, last_notified=None):
    """What the background add-on-update check should do: "none" (up to date, or nothing to
    do), "auto_update" (download and install without asking) or "notify" (show a tooltip,
    once per release). Auto-update wins when both are on."""
    if not latest or version_at_least(current, latest):
        return "none"
    if auto_update:
        return "auto_update"
    if notify and should_notify_update(current, latest, last_notified):
        return "notify"
    return "none"


def find_deck_moves_needed(moves_ledger, existing_guid_to_deck, existing_front_to_guid=None):
    """Which of the learner's cards need to move deck to match a reorganised source.

    `moves_ledger` is {guid: {from, to, front?}}: every note the deck source relocated
    without changing its GUID. `existing_guid_to_deck` is {guid: current deck name} and
    `existing_front_to_guid` is {first field: guid} (optional), both for the learner's
    collection.

    A card is matched by GUID. When the ledger GUID is not in the collection (the learner's
    GUID predates an `id_seed` change), it is matched by the entry's `front`, if both
    `front` and `existing_front_to_guid` are present.

    A move applies only if the card still sits at `from`. A card anywhere else is left
    alone, which keeps this idempotent and preserves the learner's own filing.

    Returns [{guid, from, to}] where `guid` is the learner's note GUID, sorted by `to` then
    `from`."""
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
    """Which of a retired note's protected-field values to copy onto a replacement note.

    `saved` is {field: value} read off the retired note; `target_current` is the same shape
    for the replacement. Only fills a field that is blank on the replacement. Field names
    match case-insensitively, since the two note types can spell a field differently."""
    current_lower = {f.lower(): v for f, v in target_current.items()}
    return {f: v for f, v in saved.items()
            if v.strip() and not (current_lower.get(f.lower()) or "").strip()}


def find_retired_in_collection(retired_ledger, existing_guids, existing_front_to_guid=None,
                               live_guids=(), live_fronts=()):
    """The retired cards the learner still has in their collection.

    `retired_ledger` is the manifest's {deck_name: {guid: {identity, reason, superseded_by,
    ...}}}. `existing_guids` is the note GUIDs the learner has under the scope tag, and
    `existing_front_to_guid` is {first field: guid} for the same notes (optional).

    A retired card is matched by GUID. When the ledger GUID is not in the collection, it is
    matched by front text: the entry's `front` if recorded, else its `identity`. An image
    note's identity is never a first field, so it stays GUID-only.

    Returns one dict per retired card, sorted by deck then identity, with keys guid,
    source_guid, deck, identity, reason, superseded_by and replacements_present. `guid` is
    the learner's note GUID and `source_guid` is the ledger key, for lookups in maps keyed
    the builder's way. `replacements_present` counts how many of `superseded_by` are in the
    collection, by GUID only.

    `live_guids` and `live_fronts` are the guids and fronts of the cards in the current
    packages. A learner note that matches either is a live card and is never reported."""
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
    """Pairs where the learner holds both wordings of a reworded card: the old wording with
    its review history and the current one imported beside it.

    `superseded` is the manifest's {superseded front: its replacement}.
    `existing_front_to_guid` is {first field: guid} for the learner's collection.

    Only pairs where the learner holds both are returned; the import's front matching
    handles a lone old wording. Returns [{guid, front, successor_guid, successor_front}]
    sorted by front; `guid` is the predecessor, the copy that gets emptied and archived.

    A chain of rewordings (A to B to C) pairs every older wording with the final one. A
    wording that leads into a cycle reports nothing. A predecessor whose guid or front is in
    `live_guids`/`live_fronts` is a live card and is never reported."""
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


# Limits checked before a package is unpacked. Real packages run far below these; they only
# stop one that would fill the disk or memory.
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
    """(entry count, directory bytes) from the archive's end records, or None when there is
    no end record.

    Read the way zipfile reads them: the 32-bit end record, and a zip64 record whenever a
    zip64 locator sits in front of it, whatever the 32-bit fields say. The directory size is
    what bounds zipfile's parsing work, so a record that undercounts its entries cannot hide
    a large one. Where a zip64 record can be found two ways, the larger reading wins."""
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

    Raises PackageLimitError for excessive sizes or unsafe member names, and
    zipfile.BadZipFile for a file that is not a zip. The unpacked sizes are the ones the
    directory declares. Anki's importer ignores those, so a package handed to it directly
    goes through copy_apkg_checked first."""
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
    for info in infos:
        name = info.orig_filename
        if (name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name)
                or ".." in name.replace("\\", "/").split("/") or "\0" in name):
            raise PackageLimitError("This .apkg looks damaged or unsafe, "
                                    "so nothing was imported.")
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

    A current export keeps its data in a zstd-compressed collection.anki21b beside a
    placeholder collection.anki2, so the newest member present wins: .anki21b, then .anki21,
    then .anki2. Decoding .anki21b needs zstandard; without it this raises
    RuntimeError(NEWER_APKG_ERROR).

    The package's limits are checked first (check_apkg_limits), and a zstd member is decoded
    no further than APKG_MAX_MEMBER."""
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
    """{notetype id as str: (notetype name, [field name, ...])} from either format:
    col.models JSON in the legacy one, the `notetypes` and `fields` tables in the newer one.
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
    """Return (note_id, fields, guid) for every note in an .apkg file, in either format.
    `fields` is the note's complete field list; matching keys on `fields[0]` and
    note_display_label picks the field to show."""
    with _apkg_db(path) as (con, _newer):
        return [(rid, flds.split(FS), guid) for rid, guid, flds in
                con.execute("select id, guid, flds from notes")]


def apkg_media_index(path):
    """{media filename: zip member} for the pictures an .apkg carries, read from its JSON
    "media" member. Returns {} for a deck with no pictures or an index that will not parse.
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


_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_media_entries(media):
    """A deck's manifest `media` map, checked: {filename: {"path", "sha256"}}.

    The filename is what a card's <img> names, so it must be a bare name; the path must
    stay inside the source. Anything else raises RuntimeError before a byte is fetched.
    """
    if media is None:
        return {}
    if not isinstance(media, dict):
        raise RuntimeError("the manifest lists a deck's images in a shape this add-on "
                           "doesn't recognize")
    out = {}
    for name, entry in media.items():
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(c in name for c in "/\\:\0")):
            raise RuntimeError(f"the manifest names an image {name!r} that isn't a "
                               "plain file name")
        if (not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str)
                or not _SHA256.fullmatch(entry["sha256"])):
            raise RuntimeError(f"the manifest entry for the image {name!r} is incomplete")
        safe_source_path(entry.get("path"))
        out[name] = {"path": entry["path"], "sha256": entry["sha256"]}
    return out


def inject_apkg_media(src, dest, files):
    """Write `src` to `dest` with `files` ({filename: local path}) added as media.

    Members already in `src` are copied as they are; each new picture gets the next free
    number in the legacy JSON "media" index, which is the layout genanki writes.
    """
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        index = json.loads(zin.read("media").decode("utf8")) if "media" in names else {}
        if not isinstance(index, dict):
            raise RuntimeError("this deck's picture index is damaged, so its images "
                               "couldn't be added")
        taken = {int(k) for k in index if str(k).isdigit()}
        n = max(taken, default=-1) + 1
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename != "media":
                    with zin.open(info) as fin, zout.open(info, "w",
                                                          force_zip64=True) as fout:
                        shutil.copyfileobj(fin, fout, 1024 * 1024)
            for name, local in files.items():
                zout.write(local, str(n), compress_type=zipfile.ZIP_STORED)
                index[str(n)] = name
                n += 1
            zout.writestr("media", json.dumps(index))


def extract_apkg_media(path, index, names, dest):
    """Extract the named pictures from an .apkg into `dest`. Returns {filename: local path}
    for the ones that came out. A name absent from `index`, or a member absent from the zip,
    is skipped."""
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
    """Every Anki deck name inside an .apkg, in either on-disk format. The newer format's
    decks table separates path segments with the unit separator."""
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
    """Return labeled field detail for notes in the .apkg at `path`, as a list of {"rid",
    "guid", "notetype", "fields": [(field_name, value), ...]} in the .apkg's note order, in
    either format. `rids`, if given, limits the result to those note ids.

    Field names come from the .apkg's note types, since layouts differ by note type. A note
    whose note type the .apkg does not describe gets generic "Field N" labels. Costs more
    than apkg_notes, so only the review dialog calls it."""
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
    """{guid: notetype name} for every note in the .apkg, in either format. Cheaper than
    apkg_note_details."""
    with _apkg_db(path) as (con, newer):
        names = {int(mid): n for mid, (n, _f) in _apkg_notetypes(con, newer).items()}
        return {guid: names.get(mid, "")
                for guid, mid in con.execute("select guid, mid from notes")}


def base_notetype_name(name):
    """A note type's name with Anki's collision suffixes stripped: the "+" Anki appends on
    import to a same-named type with different fields (repeatable: "Basic++"), and the "-"
    plus five hex digits its legacy save appends."""
    return re.sub(r"(?:\++|-[0-9a-f]{5})+$", "", name or "") or (name or "")


def plan_notetype_changes(incoming_types, existing_types, managed):
    """Which of the learner's notes must change note type for this update to land on them:
    Anki's importer will not move an existing note to a different note type.

    Both arguments are keyed by the learner's note guid: `incoming_types` is what this
    update would make each matched note and `existing_types` is what they are now. A change
    is planned only when both names are in `managed`, so the learner's own note types are
    never touched.

    Returns [{guid, old, new}] sorted by guid. The caller asks permission first: Anki treats
    this as a schema change, which forces a full AnkiWeb sync."""
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
    """Return {notetype_name: {"css": str, "tmpls": [(name, qfmt, afmt), ...]}} for every
    note type in the .apkg at `path`, from the legacy `col.models` JSON.

    A newer-format package raises with the re-export instruction: its templates sit in
    protobuf blobs, and returning {} would read as "no templates differ"."""
    with _apkg_db(path) as (con, newer):
        if newer:
            raise RuntimeError(NEWER_APKG_ERROR)
        models_json = con.execute("select models from col").fetchone()[0]
    return {m["name"]: model_shape(m) for m in json.loads(models_json).values()}


def apkg_model_fields(path):
    """{notetype_name: [field name, ...]} for the .apkg at `path` (legacy format, as
    apkg_models)."""
    with _apkg_db(path) as (con, newer):
        if newer:
            raise RuntimeError(NEWER_APKG_ERROR)
        models_json = con.execute("select models from col").fetchone()[0]
    return {m["name"]: [f["name"] for f in m.get("flds", [])]
            for m in json.loads(models_json).values()}


def model_shape(m):
    """Reduce a note-type dict (apkg JSON or mw.col.models form) to what determines how
    cards look: CSS plus each template's question/answer HTML."""
    return {
        "css": m.get("css", ""),
        "tmpls": [(t.get("name", ""), t.get("qfmt", ""), t.get("afmt", ""))
                  for t in m.get("tmpls", [])],
    }


def changed_templates(incoming, existing):
    """Note-type names present in both mappings whose template HTML or CSS differ.
    `incoming`/`existing` are {name: model_shape(...)}."""
    return [name for name, shape in incoming.items()
            if name in existing and existing[name] != shape]


def note_fields_hash(fields):
    """A short stable digest of a note's field values, for the declined-card registry. 16
    hex chars: a collision only costs one missing changed-since cue."""
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
    """Group pending cards and retired rows that belong to the same change, so an update
    screen can show one explanation over the rows it caused.

    `details` is a list of dicts, each with a `guid` and optionally a `change_notes` list in
    `change_notes_for`'s shape ({kind, note, on, hash}). `retired` is a list of dicts, each
    with `superseded_by` (a list of guids).

    Cards join a group by carrying an identical (kind, note, on) entry; only a card's first
    matching entry is used. A retired row joins the group of any card it names in
    `superseded_by`, merging groups if it names cards in several. A retired row naming no
    card in this batch starts a group of its own with no note.

    Returns [{"note": entry-or-None, "members": [row, ...]}] in the order each group was
    first started."""
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
    """A deck source's own short label for where a card came from, e.g. `[T10Q2]`, or ""
    when it ships none. The label is opaque text. Anything that is not a plain string is
    dropped, and a long label is clipped."""
    if not isinstance(manifest_sources, dict):
        return ""
    label = manifest_sources.get(guid)
    if not isinstance(label, str):
        return ""
    label = label.strip()
    return label[:SOURCE_LABEL_MAX] if label else ""


def prune_declined(reg, retired_guids, seen, manifest_decks):
    """Drop registry entries that are moot, and re-file the ones whose card moved.

    An entry is moot when its note was retired upstream, or when its guid is in no package
    of the manifest. `seen` is {deck: guids} for the decks read this run (a package's own
    guids plus the learner guids they matched). Absence is judged only once every deck in
    `manifest_decks` was read, because a card the source moved to an unread deck is still
    there. An entry whose card turns up in a different deck follows it. An entry that is not
    a dict is left alone unless its guid is retired. Mutates `reg`; returns whether anything
    changed."""
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
    """Every guid a standing decline suppresses: every key of the registry, whatever its
    entry holds. Membership alone decides, so an entry whose value is not a dict still
    suppresses its card; do not filter on `state`."""
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
    """Turn every old "skip" entry into a held one flagged `migrated`, in place, so those
    cards return at Later. Returns whether anything changed; a second call changes nothing.
    """
    changed = False
    for entry in (registry.values() if isinstance(registry, dict) else ()):
        if isinstance(entry, dict) and entry.get("state") == "skip":
            entry["state"] = "held"
            entry["migrated"] = True
            changed = True
    return changed


def card_progress(decks):
    """([cards ahead of each deck], total cards), for a progress bar that moves by
    cards instead of by decks. A deck with no usable `cards` count weighs one."""
    starts, total = [], 0
    for d in decks:
        starts.append(total)
        n = d.get("cards")
        total += n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
    return starts, total


def later_status(entry, incoming_hash):
    """What a returning held card's row needs to know about its history, as the detail
    keys the review screen reads: `later_wait` (the row starts at Later: a noted card
    whose content has not changed, or an old Skip, `migrated` or `wait`), `later_note` and
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
    if (out.get("later_note_status") == "not_updated" or entry.get("migrated")
            or entry.get("wait")):
        out["later_wait"] = True
    return out


def later_count(registry, excluded, retired=()):
    """How many Later cards Update my decks can still offer: held entries outside the
    decks named in `excluded`, leaving out the `retired` guids the next run releases."""
    excluded, retired = set(excluded or ()), set(retired or ())
    return sum(1 for g, e in held_entries(registry).items()
               if e.get("deck") not in excluded and g not in retired)


def later_migration_line(n):
    """The update screen's one line over a list holding `n` rows migrated from Skip, or ""
    when there are none."""
    if not n:
        return ""
    return (f"Skip is now Later. {plural(n, 'card')} you skipped earlier "
            f"{'is' if n == 1 else 'are'} below, still set to Later.")


def later_nudge_due(count, last_seen):
    """Whether the startup reminder about Later cards should show: only when the count
    has grown past what the learner was last told."""
    return count > (last_seen or 0)


# The standing declines a row's control can show, by row kind. A registry state outside
# these is not a decision the row carries.
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
    group_hint = "Showing a group's cards does not count them as opened. " if inside else ""
    return (f"<b>{len(left)} of {plural(len(rows), 'card')} not opened yet</b>{where}. "
            f"{group_hint}Update applies each one as its row is set.")


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
    """Copy the .apkg at `src` to `out`, rewriting note GUIDs per `remap` and dropping
    declined notes.

    `remap` is {note_id: new_guid}; other notes are untouched. `drop` is a set of note ids
    to remove, notes and their cards rows both; a drop wins over a remap for the same id.
    `prepare_notetypes`, when provided, receives the scratch SQLite connection after those
    edits, before commit. It never receives the original archive.

    Legacy format only, preferring collection.anki21 over collection.anki2. Raises on a
    newer-format package: Anki would read its collection.anki21b and ignore a rewritten
    legacy member."""
    check_apkg_limits(src)
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            if "collection.anki21b" in names:
                raise RuntimeError(NEWER_APKG_ERROR)
            member = ("collection.anki21" if "collection.anki21" in names
                      else "collection.anki2")
            z.extract(member, d)
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
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zout:
            for info in zin.infolist():
                if info.filename == member:
                    zout.write(os.path.join(d, member), member,
                               compress_type=zipfile.ZIP_DEFLATED)
                else:
                    copy = zipfile.ZipInfo(info.filename, info.date_time)
                    copy.file_size = info.file_size
                    with zin.open(info) as fin, zout.open(copy, "w") as fout:
                        shutil.copyfileobj(fin, fout, 1024 * 1024)


def remap_cards(src, existing_fronts, aliases):
    """Match each note in the .apkg at `src` to one of the learner's existing cards.

    Matching order, strongest signal first:

      1. GUID: the incoming note's GUID already belongs to one of the learner's cards.
      2. Recorded source: the learner's card last synced as this incoming GUID
         (`existing_fronts.sources`). Survives a front the learner reworded.
      3. Front text: the learner's card shows this exact front (`existing_fronts` is
         {front_text: guid}).
      4. `aliases` ({current_front: previous_front}): the learner's card still shows the
         prior wording of a renamed front.

    Each tier runs over the whole package before the next, and a learner GUID is claimed by
    one incoming note only: a note whose text match is already taken imports as new.

    Returns (remap, in_place, as_new, new_notes, matched). `remap` is {note_id: guid} for
    notes whose GUID needs rewriting; a GUID match needs none. `in_place` and `as_new` are
    counts. `new_notes` is [(note_id, fields, guid), ...] for the notes that will import as
    new, in the .apkg's order. `matched` is [(note_id, apkg_guid, existing_guid), ...] for
    the rest."""
    notes = apkg_notes(src)
    existing_guids = set(getattr(existing_fronts, "guids", existing_fronts.values()))
    resolved = {i: guid for i, (_rid, _fields, guid) in enumerate(notes)
                if guid in existing_guids}
    claimed = set(resolved.values())
    sources = getattr(existing_fronts, "sources", None) or {}
    for i, (_rid, _fields, guid) in enumerate(notes):
        mine = sources.get(guid)
        if i not in resolved and mine is not None and mine not in claimed:
            resolved[i] = mine
            claimed.add(mine)
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
    source declares protected on this card."""
    return set(configured) | set((per_note or {}).get(guid) or ())


# shipped_fields.json also keeps one hash per shipped field under "#<field>". Anki
# rejects field names starting with "#", so these never collide with a text baseline.
HASH_KEY = "#"
MOD_KEY = "##mod"


def shipped_field_hashes(details, matched, skip=frozenset()):
    """{learner guid: {"#<field>": hash}} for every note in a package that reached the
    collection, whether or not Anki had to write it. `skip` holds the package or learner
    guids the import left out, which keep their earlier hashes."""
    learner = {rid: existing for rid, _pkg, existing in matched}
    out = {}
    for d in details:
        guid = learner.get(d["rid"], d["guid"])
        if guid in skip or d["guid"] in skip:
            continue
        hashes = {HASH_KEY + name: note_fields_hash([value or ""])
                  for name, value in d["fields"]}
        if guid != d["guid"]:
            hashes[HASH_KEY] = d["guid"]   # the source's own guid, for the digest
        out[guid] = hashes
    return out


def stored_field_hashes(hashes, stored, wrote):
    """`hashes` with each note in `wrote` re-hashed from `stored` ({guid: {field:
    value}}, the text the collection holds after the import), so a field Anki rewrote on
    the way in is hashed as it was stored. A field `stored` lacks, the source-guid key
    and a note the import did not write keep what `hashes` has."""
    out = {}
    for guid, fields in hashes.items():
        text = (stored.get(guid) or {}) if guid in wrote else {}
        out[guid] = {k: (note_fields_hash([text[k[len(HASH_KEY):]]])
                         if k != HASH_KEY and k[len(HASH_KEY):] in text else v)
                     for k, v in fields.items()}
    return out


def changed_since(hashes, before):
    """The learner guids in `hashes` ({guid: {"#<field>": hash}}) with a field whose text
    differs from `before` ({guid: {field: value}}, read ahead of the import; a missing
    field reads as empty), or that `before` lacks. Tags and the source-guid key don't count."""
    return {guid for guid, fields in hashes.items()
            if guid not in before or any(
                note_fields_hash([before[guid].get(k[len(HASH_KEY):]) or ""]) != v
                for k, v in fields.items() if k != HASH_KEY)}


_RENAMED_MEDIA = re.compile(r"-[0-9a-f]{40}(\.[A-Za-z0-9]+)")


def hashes_unless_renamed(hashes, stored):
    """`hashes` where a field that differs from the package only by Anki's renamed media
    references (name-<sha1>.ext) is hashed from the `stored` text ({guid: {field:
    value}}) instead, so a rename from an earlier import is not read as a hand edit."""
    out = {}
    for guid, fields in hashes.items():
        text = stored.get(guid) or {}
        out[guid] = {k: (note_fields_hash([text[k[len(HASH_KEY):]]])
                         if k != HASH_KEY and k[len(HASH_KEY):] in text
                         and note_fields_hash([_RENAMED_MEDIA.sub(
                             r"\1", text[k[len(HASH_KEY):]])]) == v else v)
                     for k, v in fields.items()}
    return out


def with_field_hashes(captured, prior, hashes, wrote):
    """New shipped_fields.json entries for one deck plus this package's hashes. A note
    the import wrote takes this import's captured text baselines; one it did not write
    keeps its prior ones. A note without new hashes (the import skipped it) keeps its
    prior hash keys too."""
    out = {}
    for guid in set(captured) | set(hashes):
        old = prior.get(guid) or {}
        if guid in wrote:
            base = captured.get(guid, {})
        else:
            base = {k: v for k, v in old.items() if not k.startswith(HASH_KEY)}
        kept = {} if guid in hashes else {k: v for k, v in old.items()
                                          if k.startswith(HASH_KEY)}
        out[guid] = {**base, **kept, **hashes.get(guid, {})}
    return out


def find_learner_edits(shipped, existing_fields, protected=(), per_note=None, mods=None):
    """[(guid, field, shipped hash, current value)] for each field whose text no longer
    matches what the source last shipped. Protected fields are the learner's by design,
    and a field with no recorded hash is never reported. A note whose mod in `mods` is
    older than the one recorded with its hashes was rolled back, not edited."""
    out = []
    for guid, fields in existing_fields.items():
        entry = shipped.get(guid)
        if not isinstance(entry, dict):
            continue
        recorded = str(entry.get(MOD_KEY, ""))
        if recorded.isdigit() and (mods or {}).get(guid, int(recorded)) < int(recorded):
            continue
        # Per-card protection is keyed by the deck's own guid (the "#" entry).
        skip = {str(p).lower() for p in protected_for(guid, protected, per_note)
                | protected_for(entry.get(HASH_KEY, guid), protected, per_note)}
        for name, value in fields.items():
            h = entry.get(HASH_KEY + name)
            if (isinstance(h, str) and str(name).lower() not in skip
                    and note_fields_hash([value or ""]) != h):
                out.append((guid, name, h, value or ""))
    return out


def learner_owns(entry, field, value):
    """Whether a protected field's value is the learner's own, judged against what the
    source last shipped: its text baseline, else its hash, else whether it is non-blank."""
    entry = entry or {}
    if field in entry:
        return entry[field] != value
    h = entry.get(HASH_KEY + field)
    if isinstance(h, str):
        return note_fields_hash([value or ""]) != h
    return bool((value or "").strip())


def source_changed(entry, field, incoming):
    """Whether an incoming value differs from what the source last shipped."""
    entry = entry or {}
    if field in entry:
        return entry[field] != incoming
    h = entry.get(HASH_KEY + field)
    return isinstance(h, str) and note_fields_hash([incoming or ""]) != h


def with_edits_protected(per_note, pairs):
    """`per_note` plus each (guid, field) the learner edited by hand, for one import."""
    out = {g: list(fs) for g, fs in (per_note or {}).items()}
    for guid, field in pairs:
        if field not in out.setdefault(guid, []):
            out[guid].append(field)
    return out


class KeptEdit(str):
    """The learner's own value on a field the import keeps. Still the current value to
    every reader of find_changed_notes' output; `guid` is the learner's note."""
    guid = ""


def find_kept_edits(matched, details, existing_fields, edited, shipped):
    """{rid: {field: KeptEdit(current)}} for each hand-edited field the incoming package
    also changes, so the confirmation can offer the deck's version."""
    by_rid = {d["rid"]: d for d in details}
    out = {}
    for rid, _pkg, guid in matched:
        mine = existing_fields.get(guid) or {}
        for name, incoming in (by_rid.get(rid) or {}).get("fields", []):
            if (name in edited.get(guid, ()) and name in mine and mine[name] != incoming
                    and source_changed(shipped.get(guid), name, incoming)):
                kept = KeptEdit(mine[name])
                kept.guid = guid
                out.setdefault(rid, {})[name] = kept
    return out


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
    `matched` is remap_cards' list of (rid, package guid, existing guid)."""
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

    `matched` is remap_cards' pair list, `details` is apkg_note_details' output for the same
    .apkg, and `existing_fields` is {guid: {field name: value}} for the learner's notes.

    Returns {note_id: {field name: current value}} with only the fields that differ. Fields
    are compared by name, never by position.

    A field in `protected` or `per_note` (see protected_for) follows collection._restore's
    rule, so the preview and the import agree: it is shown only when the import's value will
    stand, which is when the learner's field still equals what `baseline` ({guid: {field:
    value}}) says the source last shipped. With no text baseline a hash baseline decides,
    and with neither the field stands when it is blank. `protected` matches
    case-insensitively. A field the learner's note type does not have is skipped."""
    by_rid = {d.get("rid"): d for d in details}
    out = {}
    # With no per-note map every note shares one skip set, so it is built once.
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
                if learner_owns(shipped, name, existing_value[name] or ""):
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


_IMG_SRC_RE = re.compile(r"""<img[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def plain_text(field):
    """The visible text of one card field: HTML tags stripped, entities decoded, whitespace
    collapsed. A tag becomes a space, so text either side of a block tag stays separate."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", field or ""))).strip()


def truncate(text, max_len):
    """`text` capped at `max_len` characters, ending in an ellipsis when cut."""
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def note_display_label(fields, max_len=90):
    """A short plain-text label for a note, for dialogs that list its card: the first field
    with visible text, else the first image's filename. Long labels are truncated."""
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

    The hint of {{c1::answer::hint}} follows its answer in brackets, styled `.ch`; an empty
    hint is left off. The field is escaped first and the spans injected after. A deletion
    whose answer contains a brace is left as raw markup.

    `escape=False` is for a field that has already been through field_preview_html, which
    returns real markup.

    `mark_groups` adds each deletion's group number as a superscript. None adds them only
    when the field has more than one group; True or False forces it."""
    text = html.escape(text or "") if escape else (text or "")
    if mark_groups is None:
        mark_groups = len({m.group(1) for m in _CLOZE_RE.finditer(text)}) > 1

    def fill(m):
        badge = f'<sup class="cn">c{int(m.group(1))}</sup>' if mark_groups else ""
        hint = f' <span class="ch">[{m.group(3)}]</span>' if m.group(3) else ""
        return f'<span class="cloze">{m.group(2)}{badge}</span>{hint}'

    return _CLOZE_RE.sub(fill, text)


def merged_word_diff(old, new):
    """Both versions of a changed plain-text field as one word-level sequence: [(op, text)]
    segments in reading order, op one of "equal", "removed", "added". Whitespace is
    normalized to single spaces.

    Plain text only: the caller must check the field carries no markup. difflib's junk
    heuristic is off, since it can degrade a diff this short."""
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
    """How much of a merged_word_diff is unchanged text, as difflib's 0..1 ratio (2 *
    matched words / total words on both sides). Callers use it to decide whether an inline
    diff is readable. Empty on both sides is 1.0."""
    equal = sum(len(text.split()) for op, text in segments if op == "equal")
    changed = sum(len(text.split()) for op, text in segments if op != "equal")
    total = 2 * equal + changed
    return (2 * equal / total) if total else 1.0


def cloze_answer_changes(old, new):
    """Which deletions moved between two versions of a cloze Text field: (no_longer_blanked,
    newly_blanked) answer texts, or None when the surrounding words changed too. Both lists
    empty means the deletions were only regrouped.

    Answers are compared as multisets, so a sentence blanking the same word twice reports a
    change only when a copy appears or disappears."""
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
    """Which deletions' hints moved between two versions of a cloze Text field: [(answer,
    before, after)] for every deletion whose hint was added, reworded or dropped, with "" on
    whichever side has none.

    Deletions pair by answer text in order of appearance. An answer only one version has is
    cloze_answer_changes's to report and is skipped here."""
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
    """One card field as plain text for the review list, with images named instead of
    rendered, since the .apkg's media is not on disk. A field holding both text and an image
    reports both."""
    text = plain_text(render_math_spans(value, tags=False))
    names = [os.path.basename(src) for src in _IMG_SRC_RE.findall(value or "")]
    if not names:
        return text
    tag = f"[image: {', '.join(names)}]"
    return f"{text} {tag}" if text else tag


# The tags a card field uses that a QLabel's rich text can render. Any other tag is dropped
# and its text kept.
_PREVIEW_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "s", "sub", "sup", "small", "span", "font",
    "br", "p", "div", "hr",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
})

# Layout attributes worth keeping. Everything else is dropped.
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
    """Every picture a field references, as bare filenames in document order."""
    return list(dict.fromkeys(
        os.path.basename(src) for src in _IMG_SRC_RE.findall(value or "")))


# A QLabel has no MathJax engine, so these render the constructs the decks write (\text,
# \frac, sub/superscripts, a few symbol macros) as ordinary text.
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
    """One card field as HTML the review dialog can render. Structure is kept: script and
    style blocks are removed, a tag outside _PREVIEW_TAGS loses the tag but keeps its text,
    and attributes are filtered to _PREVIEW_ATTRS.

    `image_html`, when given, is called with one picture's bare filename and returns the
    markup to put in that <img>'s place, or None to decline. Without it, or on a decline, an
    <img> becomes "[image: name]"."""
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
        # The ones a plain Update never imports: an old Skip or a noted card.
        waiting = [e for e in held_entries(registry).values()
                   if e.get("deck") not in set(excluded or ())
                   and (e.get("wait") or e.get("migrated") or e.get("note"))]
        since = min((str(e["decided"]) for e in waiting if e.get("decided")), default="")
        stuck = (f", {len(waiting)} waiting at Later" + (f" since {since}" if since else "")
                 ) if waiting else ""
        lines.append(f"  {plural(later, 'card')} left for later{ascii_text(stuck)}")
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
                          excluded=(), edits=()):
    """Render flagged-card feedback as plain text, ready to paste into a message.

    `entries` is a list of {"deck", "front", "guid", "note"}, grouped by deck in first-seen
    order under the deck's leaf name. Fronts go through plain_text; notes only have their
    whitespace folded.

    `standing_declines`, when supplied, is the current declined-card registry, rendered
    after the entries as a state snapshot: GUIDs grouped by deck and decision date, one line
    per deck per state, without content hashes. Later cards in the `excluded` decks are left
    out of its count line.

    `edits` are the learner's own hand edits, written after the entries as a readable line
    plus exact JSON; they never make a digest on their own.

    Returns "" for no entries."""
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
    if edits:
        lines.extend(_edit_lines(edits))
    if standing_declines is not None:
        lines.extend(_decline_snapshot_lines(standing_declines, excluded))
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _edit_lines(edits):
    # The entries loop already ends on a blank line.
    lines = ["Cards you edited (your current wording, so the deck can use it)"]
    by_deck = {}
    for e in edits:
        by_deck.setdefault(e.get("deck") or "", {}).setdefault(e["guid"], []).append(e)
    for deck, cards in by_deck.items():
        lines.append("")
        lines.append(ascii_text(deck.split("::")[-1] if deck else "(unknown deck)"))
        for items in cards.values():
            first = items[0]
            lines.append(ascii_text(f'  "{plain_text(first.get("front"))}"'))
            lines.append(f'  guid {first.get("source") or first["guid"]}')
            for e in items:
                readable = truncate(plain_text(e["value"]), 120)
                lines.append(ascii_text(f'  > {e["field"]}: {readable}'))
                # JSON, not ascii_text: the deck repo needs the value byte for byte.
                lines.append(f'  edit {json.dumps(e["field"])} {e["shipped"]} '
                             f'{json.dumps(e["value"])}')
            lines.append("")
    return lines


def duplicate_dialog_rows(groups):
    """Heading and rows for the Clean up duplicates confirmation, from find_duplicate_groups
    output.

    Returns (heading, [{"label", "detail"}]). Each row has the card's label (escaped here)
    and says which copy is kept and which is archived. Copies in the same deck read as a
    copy count."""
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

    Only notes in `scoped_nids` (the learner's notes under the scope tag) are considered. A
    note whose cards are all empty is never removed, only reported, since removing them
    would delete the note.

    Returns (removable, skipped), each a list of {"nid", "card_ids"}."""
    removable, skipped = [], []
    for n in report_notes:
        if n["nid"] not in scoped_nids:
            continue
        entry = {"nid": n["nid"], "card_ids": list(n["card_ids"])}
        (skipped if n.get("will_delete_note") else removable).append(entry)
    return removable, skipped


def empty_cards_dialog_rows(rows, skipped=0):
    """Heading, rows and closing note for the Remove empty cards confirmation, from
    collection.find_empty_cards. Returns (heading, [{"label", "gone"}], tail); `gone` is the
    deletion numbers that went missing."""
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

    `flags` is {guid: the learner's note}; `index` is {guid: (deck, front)}. A flag whose
    GUID is not in the index is dropped.

    `decisions`, when given, is {guid: reader-facing state} for a decision made this run; a
    guid there with no note still gets an entry (empty "note") carrying the "decision" key.
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
    """Fold notes recovered from disk into this run's flags and card index, in place. A note
    already collected this run wins over a saved one. The index gets the saved deck/front
    for every restored card. Returns the number of notes restored."""
    restored = 0
    for guid, entry in (saved or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("note"), str):
            continue
        note = entry["note"].strip()
        if not note or guid in flags:
            continue
        flags[guid] = note
        index.setdefault(guid, (entry.get("deck", ""), entry.get("front", guid)))
        restored += 1
    return restored
