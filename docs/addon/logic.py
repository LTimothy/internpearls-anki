"""Pure logic for Intern Pearls Deck Tools.

Nothing here imports aqt or anki, so it's testable with plain pytest, no Anki
environment needed. If a function starts needing mw/col, it belongs in __init__.py
instead, not here.
"""
import json
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
    """
    excluded = set(excluded or ())
    return [d for d in (manifest or {}).get("decks", [])
            if d["name"] not in excluded and installed.get(d["name"]) != d["version"]]


def deck_status(manifest, installed, excluded=None):
    """One row per available deck for the deck-manager UI.

    Returns dicts with the deck's full `name`, a short display label, its `cards` count,
    whether it's `enabled` (not opted out), and a `state` relative to the collection:
    "new" (never synced), "update" (a newer version is available), or "current" (already
    up to date). Pure so the manager dialog stays a thin rendering layer over it.
    """
    excluded = set(excluded or ())
    rows = []
    for d in (manifest or {}).get("decks", []):
        name = d["name"]
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


def clamp_interval_minutes(minutes, floor_minutes=1, default_minutes=15):
    """Sanitize a configured poll interval: a missing or non-numeric value falls back to
    `default_minutes`; anything below `floor_minutes` is raised to the floor so a typo
    (or a 0) can't turn into a busy-poll loop against the deck source.
    """
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = default_minutes
    return max(floor_minutes, m)


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


def apkg_models(path):
    """Return {notetype_name: {"css": str, "tmpls": [(name, qfmt, afmt), ...]}} for
    every note type carried by the .apkg at `path`.

    Reads the legacy `col.models` JSON column, the format genanki (and Anki's own
    legacy exporter) writes — the same collection.anki2 assumption apkg_notes makes.
    """
    with zipfile.ZipFile(path) as z:
        if "collection.anki2" not in z.namelist():
            raise RuntimeError("Unexpected .apkg format (no collection.anki2).")
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            models_json = con.execute("select models from col").fetchone()[0]
            con.close()
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
    """Match each note in the .apkg at `src` to one of the learner's existing cards.

    Matching order, strongest signal first:
      1. GUID: the incoming note's GUID already belongs to one of her cards. This is
         the durable path — deck specs give every card an explicit stable `id`, so a
         reworded front no longer changes the GUID and needs no alias at all.
      2. Front text: her card currently shows this exact front (`her` is
         {front_text: guid}). Covers collections whose GUIDs predate stable ids.
      3. `aliases` ({current_front: previous_front}): her card still shows the one
         prior wording of a renamed front.

    Returns (remap, in_place, as_new): `remap` is {note_id: guid} for notes whose GUID
    needs rewriting to match an existing card, `in_place`/`as_new` are counts for the
    confirmation dialogs. A GUID match needs no rewrite, so it never lands in `remap`.
    """
    remap, in_place, as_new = {}, 0, 0
    her_guids = set(her.values())
    for rid, front, apkg_guid in apkg_notes(src):
        if apkg_guid in her_guids:
            in_place += 1
            continue
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
