"""Pure logic for Intern Pearls Deck Tools.

Nothing here imports aqt or anki, so it's testable with plain pytest, no Anki
environment needed. If a function starts needing mw/col, it belongs in __init__.py
instead, not here.
"""
import html
import json
import os
import re
import sqlite3
import tempfile
import zipfile

FS = "\x1f"   # Anki's field separator inside a note's flds column


def bullets(items, cap=None):
    """Render a list as clean HTML for use inside a rich-text dialog.

    If `cap` is set and there are more items than that, show only the first `cap` plus
    a one-line "...and N more" summary instead of the full list. A long enough list
    (dozens of retired or relocated cards) is a wall of text no one reads line by line
    even when it's technically scrollable — capping is a readability fix, not just a
    sizing one. `cap=None` (the default) preserves the old uncapped behavior.
    """
    shown = items if cap is None or len(items) <= cap else items[:cap]
    extra = len(items) - len(shown)
    html = "<ul style='margin:4px 0 4px 0;'>" + "".join(
        f"<li>{item}</li>" for item in shown)
    if extra:
        html += f"<li><i>...and {extra} more</i></li>"
    return html + "</ul>"


def night_mode_image_css(enabled):
    """CSS that dims bright white-background images while Anki's Night Mode is on.

    Anki's own Night Mode already adds a "nightMode" class to the card body, so this
    only needs to define the rule; the browser applies it only when that class is
    present. Dims rather than inverts, since a full color invert looks wrong on a
    real photo mixed into an otherwise diagram-heavy deck.
    """
    if not enabled:
        return ""
    return "<style>.nightMode img { filter: brightness(0.7) contrast(0.92); }</style>"


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
    repo's CLAUDE.md). Missing `schema` means an old manifest predating this field,
    always readable, so it defaults to 1 (never newer than any real supported_schema).
    """
    return bool(manifest) and manifest.get("schema", 1) > supported_schema


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


def find_deck_moves_needed(moves_ledger, her_guid_to_deck, her_front_to_guid=None):
    """Which of the learner's cards need to move deck to match a pure reorg.

    `moves_ledger` is {guid: {from, to, front?}}: every note the deck repo has ever
    relocated without changing its GUID (see build_all.py's deck_moves.json). `front`,
    when present, is the note's current first field, used to find a learner's card
    even when its GUID no longer matches the ledger's (see below).
    `her_guid_to_deck` is {guid: current deck name} for her collection.
    `her_front_to_guid` is {first field: guid} for her collection (optional).

    Normally a card is matched to a ledger entry by GUID. But a card whose deck source
    changed its `id_seed` (say Anesthesia Pharmacology's v1 to v2) has a *different*
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


def find_retired_in_collection(retired_ledger, her_guids):
    """The retired cards a learner still has in her collection.

    When a deck splits, merges, or drops a card, the old card's GUID leaves the
    canonical set but her copy of it is never touched by a sync (sync only ever adds
    the replacements), so it lingers in her reviews as a duplicate. The deck repo
    records every such retirement in `retired.json`, shipped to us inside the manifest.

    `retired_ledger` is that ledger: {deck_name: {guid: {identity, reason,
    superseded_by, ...}}}. `her_guids` is the set of note GUIDs she has under the scope
    tag. Returns one dict per retired card she still has, so the reconcile flow can show
    and archive them:
        {guid, deck, identity, reason, superseded_by, replacements_present}
    `replacements_present` is how many of `superseded_by` are already in her collection
    — so the UI can distinguish "replaced by cards you already have" from "sync first to
    get the replacements." Sorted by deck then identity for stable display. Pure: the
    caller supplies her_guids and does anything collection-touching (tag checks, the
    archive itself).
    """
    her_guids = set(her_guids)
    out = []
    for deck, entries in (retired_ledger or {}).items():
        for guid, info in (entries or {}).items():
            if guid not in her_guids:
                continue
            sup = list(info.get("superseded_by") or [])
            out.append({
                "guid": guid,
                "deck": deck,
                "identity": info.get("identity", ""),
                "reason": info.get("reason", ""),
                "superseded_by": sup,
                "replacements_present": sum(1 for g in sup if g in her_guids),
            })
    out.sort(key=lambda r: (r["deck"], r["identity"]))
    return out


def apkg_notes(path):
    """Return (note_id, fields, guid) for every note in an .apkg file, where `fields` is
    the note's complete field list.

    Every field rather than just the front, because matching and display want different
    things from a note. Matching only ever keys on `fields[0]` (see remap_cards), but a
    caller that shows the card to a person needs the rest: an image note's first field
    is an <img> tag, not a prompt, so field zero alone renders as a broken image instead
    of naming the card. note_display_label picks the right field out of the whole list.
    """
    with zipfile.ZipFile(path) as z:
        if "collection.anki2" not in z.namelist():
            raise RuntimeError("Unexpected .apkg format (no collection.anki2).")
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            rows = [(rid, flds.split(FS), guid) for rid, guid, flds in
                    con.execute("select id, guid, flds from notes")]
            con.close()
    return rows


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
    """
    wanted = None if rids is None else set(rids)
    with zipfile.ZipFile(path) as z:
        if "collection.anki2" not in z.namelist():
            raise RuntimeError("Unexpected .apkg format (no collection.anki2).")
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            try:
                models = json.loads(con.execute("select models from col").fetchone()[0])
            except (sqlite3.Error, TypeError, ValueError, IndexError):
                models = {}
            rows = list(con.execute("select id, guid, mid, flds from notes"))
            con.close()

    names = {}
    for mid, m in (models or {}).items():
        ordered = sorted(m.get("flds", []), key=lambda f: f.get("ord", 0))
        names[str(mid)] = (m.get("name", ""), [f.get("name", "") for f in ordered])

    out = []
    for rid, guid, mid, flds in rows:
        if wanted is not None and rid not in wanted:
            continue
        notetype, field_names = names.get(str(mid), ("", []))
        labeled = [(field_names[i] if i < len(field_names) else f"Field {i + 1}", value)
                   for i, value in enumerate(flds.split(FS))]
        out.append({"rid": rid, "guid": guid, "notetype": notetype, "fields": labeled})
    return out


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

    Returns (remap, in_place, as_new, new_notes): `remap` is {note_id: guid} for notes
    whose GUID needs rewriting to match an existing card, `in_place`/`as_new` are counts
    for the confirmation dialogs. A GUID match needs no rewrite, so it never lands in
    `remap`.

    `new_notes` is [(note_id, fields, guid), ...] for the notes that will import as new,
    in the .apkg's own order, and `as_new` is exactly its length. It's returned from here
    rather than re-derived by a second function so the matching ladder above stays the
    single source of truth about what "new" means: a separate implementation would
    eventually disagree with this one, and the visible symptom would be a preview that
    lies to the learner about which cards are about to appear.
    """
    remap, in_place, new_notes = {}, 0, []
    her_guids = set(her.values())
    for rid, fields, apkg_guid in apkg_notes(src):
        if apkg_guid in her_guids:
            in_place += 1
            continue
        front = fields[0] if fields else ""
        her_guid = her.get(front)
        if her_guid is None and front in aliases:
            her_guid = her.get(aliases[front])
        if her_guid is None:
            new_notes.append((rid, fields, apkg_guid))
        else:
            in_place += 1
            if her_guid != apkg_guid:
                remap[rid] = her_guid
    return remap, in_place, len(new_notes), new_notes


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
    text = plain_text(value)
    names = [os.path.basename(src) for src in _IMG_SRC_RE.findall(value or "")]
    if not names:
        return text
    tag = f"[image: {', '.join(names)}]"
    return f"{text} {tag}" if text else tag


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
            if e.get("guid"):
                lines.append(f'  guid {e["guid"]}')
            lines.append(f'  > {plain_text(e.get("note"))}')
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def duplicate_dialog_html(groups):
    """Body of the Clean up duplicates confirmation, from find_duplicate_groups output.

    Each line leads with the card's readable label (the note's precomputed 'label',
    see collection._her_notes_summary; escaped here since it's data), then says which
    copy is kept and which is archived. When every copy sits in the same deck it reads
    as a copy count rather than repeating that deck name twice.
    """
    lines = []
    for g in groups:
        label = html.escape(g["keep"].get("label") or g["front"])
        keep_leaf = g["keep"]["deck"].split("::")[-1]
        arch = g["archive"]
        arch_leaves = [a["deck"].split("::")[-1] for a in arch]
        arch_reps = ", ".join(str(a["reps"]) for a in arch)
        if all(leaf == keep_leaf for leaf in arch_leaves):
            detail = (f"{1 + len(arch)} copies in {html.escape(keep_leaf)}: keeping the "
                      f"one with {g['keep']['reps']} review(s), archiving {len(arch)} "
                      f"({arch_reps} review(s))")
        else:
            detail = (f"keeping {html.escape(keep_leaf)} ({g['keep']['reps']} review(s)), "
                      f"archiving {html.escape(', '.join(arch_leaves))} "
                      f"({arch_reps} review(s))")
        lines.append(f"{label} <span style='color:gray;'>{detail}</span>")
    n_archive = sum(len(g["archive"]) for g in groups)
    n_cards = len(groups)
    copies = "copy" if n_archive == 1 else "copies"
    cards = "card" if n_cards == 1 else "cards"
    heading = (f"Found <b>{n_archive}</b> duplicate {copies} of <b>{n_cards}</b> {cards}. "
               "Each card was imported more than once. Archiving keeps one copy of each:")
    return heading + bullets(lines, cap=15)
