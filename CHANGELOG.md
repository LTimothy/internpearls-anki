# Changelog

All notable changes to Intern Pearls Deck Tools. Versions follow the semver rules in
this repo's `README.md` ("Versioning").

## v0.13.0

- Added Manage decks: a clean panel listing every deck the source offers, each with a
  checkbox and a status pill (New / Update available / Up to date) and its card count.
  Uncheck a deck to stop syncing it (already-imported cards are left alone); Select
  all / none for quick toggling. Sync and Preview sync now honor the selection.
- The same panel edits Preserved fields (the fields snapshotted and restored around
  every import so your personal annotations are never overwritten) — previously only
  reachable by hand-editing the add-on config.
- Save, or Save and sync now, straight from the panel.
- New config key `excluded_decks` backs the selection; an empty list (the default)
  syncs everything, so existing setups are unchanged.

## v0.12.1

- Fixed the biggest source of post-sync friction: after syncing, AnkiWeb often forced a
  one-way "upload from local" full sync instead of a normal incremental one. Cause: the
  importer ran with `merge_notetypes=True`, which rewrites note types on every import and
  bumps Anki's schema modification time — and any schema change forces AnkiWeb into a
  full sync. Imports now run with `merge_notetypes=False`; note types are still kept
  compatible ahead of time by the existing Fix-note-types step, which only touches the
  schema when it genuinely adds a missing field. Steady-state syncs now leave the schema
  alone, so AnkiWeb stays incremental. (Trade-off: a changed card *template/CSS* no
  longer propagates automatically — run Advanced → Fix note types, or accept one full
  sync, when a template itself changes.)
- Fail fast when offline: network calls used a 30-second timeout on Anki's UI thread, so
  an unreachable host or captive portal froze the app (beachball) for 10+ seconds.
  First-contact calls (manifest, update check) now use a 6-second timeout and show a
  clear "network isn't responding" message; only the actual deck download keeps a longer
  timeout, and it's reached only after connectivity is already confirmed.

## v0.12.0

- Added Preview sync: a dry run that shows exactly what Sync would change — per deck,
  how many cards update in place (history kept) versus get added as new — without
  taking a backup, importing, or writing anything. The "show me first" companion to
  Sync decks.
- Factored the "which decks are pending" decision into `logic.decks_to_update` so Sync
  and Preview sync compute the identical set and can't drift apart.

## v0.11.0

- Sync's confirmation dialog now flags any deck you've never synced before as a new
  deck, separately from its card count.
- Network errors (bad token, wrong repo/branch, unreachable host) now show a specific,
  actionable message instead of a raw urllib exception.
- Every menu action is wrapped so an unexpected bug shows a plain warning dialog
  instead of Anki's traceback box; the full traceback still prints to Anki's debug
  console for troubleshooting.
- Split the add-on's code: `internpearls/logic.py` holds everything that doesn't touch
  `aqt`/`anki` (apkg reading/rewriting, GUID matching, version comparison), so it's
  unit-testable with plain `pytest`, no Anki install needed. Added a test suite
  covering it.
- Deck `.apkg` and spec paths in the manifest can now include subfolders (the private
  decks repo moved its built decks into a `decks/` folder); fixed a bug where the
  GitHub-fetch path assumed a flat filename and would have failed to write the
  downloaded file.

## v0.10.2

- Fixed a factual error in About and the README: the add-on doesn't ship with any deck
  content, it only syncs whatever you point it at.
- "Notes restored on 0 card(s)" no longer shows on a fresh sync, where it's always zero
  and reads like something's missing.

## v0.10.1

- Fixed a crash on every use of Import intern pearls deck (`getFile()` rejects being
  passed both `dir` and `key`).
- Removed Restore my notes. Modernizing Import single deck to do a full one-click
  import, matching how Sync already worked, meant nothing wrote the notes-snapshot
  file anymore, so the button had quietly stopped doing anything.
- Renamed for consistency: Backup intern pearls deck now to Backup intern pearls deck,
  Full collection backup now to Backup full collection, Restore from backup to Restore
  full collection.

## v0.10.0

- Dropped "..." from every menu item, including ones that open a file picker.
- "Intern Pearls" goes lowercase inside Advanced submenu labels (still capitalized as
  the top-level menu name and in dialog titles).
- `export_deck` is a config key now instead of a hardcoded constant, so the
  deck-scoped backup/export/import tools work against any deck hierarchy.
- Added `config.md` so Anki's Config editor documents every key in place.
- Expanded About and added a "Using this for your own decks" section to the README.

## v0.9.0

- The automatic pre-sync backup now defaults to a fast, self-contained export of just
  the configured deck instead of the whole collection, pruned to the 10 most recent.
- Added Backup/Import intern pearls deck (the deck-scoped pair) and Full collection
  backup now (kept for anyone who wants broader protection).
- On a genuinely first sync, before the deck exists, the backup step is skipped rather
  than failing and asking to proceed.

## v0.8.0

- Added Export intern pearls deck: a standalone `.apkg` of just the configured deck,
  with scheduling, deck options, and media included, meant to be kept or shared.

## v0.7.1

- Every dialog now carries the "Intern Pearls" title bar (Anki's helpers default to
  the generic "Anki") and list-style messages render as real HTML bullets.
- Dropped the ellipsis from "About", which doesn't need one.

## v0.7.0

- Fixed sync state getting reset on every add-on update: `installed.json` and the
  notes snapshot used to live next to `__init__.py`, which Anki's add-on manager wipes
  and re-extracts on every update. Both now live under `user_files/`, the one
  subfolder Anki preserves across reinstalls.
- Added Restore from backup, opening Anki's own backup picker.

## v0.6.0

- Sync decks and Import single deck take a real backup automatically before touching
  anything, instead of just asking the user to remember to export one first.
- Confirmation dialogs show per-deck card counts and say plainly what's about to happen.
- Configure deck source became a proper multi-button dialog instead of a Yes/No
  question standing in for a choice; the access token field is masked; saving tests
  the connection immediately.
- Fixed a silent failure: if the front-alias list can't be fetched, the user is now
  warned instead of reworded cards quietly losing history.

## v0.5.1

- Fixed menu items vanishing on macOS: Qt auto-detects labels like "Configure..." and
  "About..." and relocates those actions into the native app menu, which can hide them
  entirely if Anki already owns that role slot.

## v0.5.0

- Adopted three-part semver (`0.5.0`, not `0.5`) and made the update comparator treat
  `0.5` and `0.5.0` as equal.

## Earlier

Menu, one-click history-safe sync (fix note types, snapshot notes, match GUIDs, import,
restore notes), and GitHub-based distribution were built out before this changelog
started; see `REGISTRY.md` in the private decks repo for that history.
