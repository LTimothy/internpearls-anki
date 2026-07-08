"""Pure logic for Intern Pearls Deck Tools.

Nothing here imports aqt or anki, so it's testable with plain pytest, no Anki
environment needed. If a function starts needing mw/col, it belongs in __init__.py
instead, not here.
"""
import os
import re
import sqlite3
import tempfile
import zipfile

FS = "\x1f"   # Anki's field separator inside a note's flds column


def bullets(items):
    """Render a list as clean HTML for use inside a rich-text dialog."""
    return "<ul style='margin:4px 0 4px 0;'>" + "".join(
        f"<li>{item}</li>" for item in items) + "</ul>"


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


def decks_to_update(manifest, installed):
    """Decks from the manifest whose version differs from what's already installed.

    `installed` is {deck_name: version_last_applied}. A deck missing from it is new; a
    deck whose version changed needs re-sync; matching versions are skipped. Shared by
    Sync (to know what to apply) and Preview sync (to report the same set without
    touching the collection), so the two can never disagree about what's pending.
    """
    return [d for d in (manifest or {}).get("decks", [])
            if installed.get(d["name"]) != d["version"]]


def apkg_notes(path):
    """Return (note_id, front_text, guid) for every note in an .apkg file."""
    with zipfile.ZipFile(path) as z:
        if "collection.anki2" not in z.namelist():
            raise RuntimeError("Unexpected .apkg format (no collection.anki2).")
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            rows = [(rid, flds.split(FS)[0], guid) for rid, guid, flds in
                    con.execute("select id, guid, flds from notes")]
            con.close()
    return rows


def write_personalized(src, remap, out):
    """Copy the .apkg at `src` to `out`, rewriting note GUIDs per `remap`.

    `remap` is {note_id: new_guid}. Notes not in `remap` are left untouched.
    """
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(src) as z:
            z.extractall(d)
        con = sqlite3.connect(os.path.join(d, "collection.anki2"))
        for rid, g in remap.items():
            con.execute("update notes set guid=? where id=?", (g, rid))
        con.commit()
        con.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))


def remap_cards(src, her, aliases):
    """Match each note in the .apkg at `src` to an existing card by front text.

    `her` is {front_text: guid} for the learner's existing cards; `aliases` is
    {current_front: previous_front} for cards whose wording changed. Returns
    (remap, in_place, as_new): `remap` is {note_id: guid} for notes whose GUID needs
    rewriting to match an existing card, `in_place`/`as_new` are counts for the
    confirmation dialogs.
    """
    remap, in_place, as_new = {}, 0, 0
    for rid, front, apkg_guid in apkg_notes(src):
        her_guid = her.get(front)
        if her_guid is None and front in aliases:
            her_guid = her.get(aliases[front])
        if her_guid is None:
            as_new += 1
        else:
            in_place += 1
            if her_guid != apkg_guid:
                remap[rid] = her_guid
    return remap, in_place, as_new
