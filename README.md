# Intern Pearls Deck Tools (Anki add-on)

History-safe deck management for Anki. Keeps a set of study decks up to date without losing your review history or the annotations you keep on a card, and lets you back up, export, or import a deck without leaving Anki. Anki's built-in re-import overwrites fields, which means any personal notes you added to cards get wiped. This add-on avoids that by matching cards by GUID (so scheduling carries over) and snapshotting and restoring whichever fields you've configured as preserved (`Notes` by default) around each import, and it backs up the deck automatically before it touches anything. It can also sync and update itself on a schedule, if you turn that on.

No deck content ships with the add-on itself; it only syncs whatever you point it at. Point Configure deck source at your own GitHub repo or local folder and it works the same way for any decks that follow the same manifest format. See "Using this for your own decks" below.

See `CHANGELOG.md` for what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from this repo.
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help.

## Menu reference

### Sync decks

The main button. It fetches `manifest.json` from your configured deck source, compares each deck's version hash against what you last synced, and only imports decks that changed. The confirmation dialog lists each affected deck with its card count, flagging any deck you've never synced before as a new deck, so you know the scope before anything happens. For each deck it:

1. Takes a fresh, timestamped backup of just the configured deck first (a self-contained `.apkg` with scheduling included, saved internally and pruned to the most recent 10). Nothing else runs until this succeeds, or you explicitly choose to continue without one.
2. Adds any missing fields to the note type (never removes or renames existing fields).
3. Snapshots your `protected_fields` on every card in scope.
4. Matches each incoming card to your existing card by GUID so review history carries over. If a card's front was reworded since your last sync, it checks the `front_aliases` map in the manifest to find the match.
5. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
6. Restores the preserved fields from the snapshot.

If no deck source is configured, it tells you to run Configure deck source first.

### Manage decks

A panel listing every deck the source offers, each with a checkbox, a status pill (New, Update available, or Up to date), and its card count. Unchecking a deck stops future syncs for it; cards already imported stay in your collection until you delete them yourself in Anki. A "Check what will sync" button downloads the changed decks and fills in, per deck, how many cards would update in place versus be added as new; nothing is imported by clicking it. The same panel edits `protected_fields`. Save keeps the choices for your next sync; Save and sync now also runs Sync decks right away.

### Configure deck source

A dialog with two buttons, GitHub repo or Local folder, plus Cancel:

- GitHub: enter the repo (`owner/name`) and a read-only personal access token. The token field is masked as you type, and the value is stored only in your local add-on config; it never leaves your machine except in requests to GitHub.
- Local folder: point it at a directory that contains `manifest.json` and the `.apkg` files.

Either way, as soon as you save, the add-on connects to the source and reports back immediately: how many decks it found, or exactly what went wrong (bad token, unreachable repo, wrong folder) so you're not left guessing until the next Sync.

You can also edit these directly under Tools > Add-ons > Intern Pearls Deck Tools > Config:

| Key | What it does |
|---|---|
| `github_decks_repo` | GitHub repo, e.g. `owner/repo-name` |
| `github_token` | Read-only fine-grained personal access token |
| `github_ref` | Branch or tag to pull from (default: `main`) |
| `decks_dir` | Local folder path; if set, GitHub is ignored |
| `scope_tag` | Root tag identifying cards this add-on manages (default: `InternPearls`). Scopes snapshots and GUID matching so your other decks are never touched. |
| `protected_fields` | Field names to snapshot and restore (default: `["Notes"]`). Add any field where you keep your own content. Also editable from Manage decks. |
| `excluded_decks` | Deck names opted out of syncing. Also editable from Manage decks. |
| `export_deck` | The deck that Backup/Import/Export intern pearls deck and the automatic pre-sync backup operate on (default: `Intern Pearls::Intern Custom`). |
| `auto_sync_decks`, `auto_sync_interval_minutes`, `notify_addon_updates`, `auto_update_addon` | Sync and update automation, see Settings below and `config.md` for details on each. |

### Settings

Sync automation and add-on update behavior, kept separate from Manage decks since those answer a different question ("which decks, which fields" versus "how automatic, how often"):

- **Sync decks automatically when updates are available**, off by default. When on, the add-on checks the source in the background on the interval below and applies any changed decks without asking, backing up first the same as a manual sync.
- **Check every N minutes**, default 15, minimum 1. The check runs off the main thread when Anki supports it (essentially all current versions do), so it doesn't freeze Anki even at a short interval; if it can't reach the source, it fails within a few seconds and just tries again next time.
- **Notify me when a new add-on version is out**, on by default. A tooltip once per new release, no installation.
- **Install add-on updates automatically**, off by default. Downloads and installs a newer version as part of the same once-per-launch check, no confirmation. A restart is still needed to load it, same as installing by hand.

### Check for add-on updates

Compares your installed version against the public repo's `version.json`. If a newer version exists, it offers to download and install the `.ankiaddon`. You still need to restart Anki afterward. This is the on-demand version of what the Settings toggles above do on their own.

### Advanced submenu

**Import single deck (manual)** picks one `.apkg` outside your configured source, for a deck someone sent you directly or a build you're testing before pushing it live. It runs the same personalization, automatic backup, and note restore as Sync, just for the one file you choose.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Sync runs this before every import.

**Backup intern pearls deck** is the manual, on-demand version of the automatic pre-sync backup: a fresh `.apkg` of just the configured deck (`export_deck`), with scheduling included, saved internally and pruned to the most recent 10. Use it right before poking at cards yourself outside the add-on.

**Import intern pearls deck** brings a previous deck backup or export back in. The file picker defaults to the internal backups folder, but you can browse to any matching `.apkg`. Since the file's own GUIDs already came from a real collection, this is a plain import with scheduling restored, matching cards update in place and anything missing is added as new; no personalization step is needed the way Sync and Import single deck need it for a spec-authored deck from someone else's collection.

**Export intern pearls deck** writes a standalone `.apkg` of just the configured deck, with your review history, deck options, and media all included, the same result as Anki's own File > Export > Anki Deck Package with every checkbox checked. This is the same export the automatic backup and Backup intern pearls deck use, just prompting you for where to save it, meant to be kept or shared on its own rather than used purely to undo a sync.

**Backup full collection** takes a full, whole-collection backup on demand, the same kind that used to run automatically before every sync. Use this for broader protection than the deck-scoped default covers. Retention for these is whatever Anki's own preferences specify, not this add-on's 10-backup limit, which only applies to the deck-scoped backups above.

**Restore full collection** opens Anki's own backup picker (the same one under File > Switch Profile > Open Backup) pointed at your backups folder, so you can revert a full collection backup if something looks wrong. This replaces your entire collection, every deck, not just the ones this add-on manages, since that's what a real collection backup contains. Anki asks you to confirm the specific backup file before doing anything.

### About

A short description of what the add-on does, a summary of your current settings (auto-sync, add-on updates, preserved fields), a reminder that no deck content ships with it, and a link to this repo.

## Updating decks

Run Intern Pearls > Sync decks, or turn on "Sync decks automatically when updates are available" in Settings so it happens on its own. Either way, only changed decks are imported, and the add-on backs up the deck automatically before touching anything, so there's no separate step to remember. For broader protection on top of that, Advanced > Backup full collection takes a whole-collection backup on demand.

## How history is preserved

Every sync and manual import starts with a fresh, timestamped backup, by default scoped to just the configured deck (fast, self-contained, includes scheduling). A full, whole-collection backup is still one click away under Advanced > Backup full collection for broader protection; it's just no longer the automatic default, since most syncs only ever need to undo changes to this one deck. If a backup can't be created for some reason, you're asked whether to proceed anyway rather than being blocked or silently continuing (an automatic background sync skips that round instead of asking, since there's no one there to answer). On someone's very first sync, before the deck exists yet, there's nothing to back up and this step is skipped entirely.

Cards are matched by GUID, not by content, so your intervals, ease factors, and review counts carry over on every sync.

Your `protected_fields` (`Notes` by default, configurable to any field name, or several) are snapshotted before import and restored after, so even if the importer overwrites them, your text comes back. Specifically: before anything runs, every note tagged under `scope_tag` has its `protected_fields` values read and saved by GUID; after the import, whatever note currently holds that GUID gets those exact values written back. It's a read-before, write-after round trip, not a merge, and it only protects notes that keep their GUID through the import. A card that imports as new (see below) has no old snapshot value to restore, since there was nothing recorded for a GUID that didn't exist before.

Note types only gain fields; nothing is removed or renamed. If you have customized a note type, those customizations stay.

When a card's front text changes between deck versions, a `front_aliases` entry in the manifest maps the new wording to the one immediately before it. The add-on checks your card's current front text against the live spec wording first, then against that one prior wording if the first check fails. If that mapping can't be fetched for some reason, you're warned before anything imports, since any reworded card would otherwise import as new and lose its history silently.

This means `front_aliases` only bridges the *most recent* rename of a given card, not its full history. On a brand new install, whether a specific card's history carries over depends only on whether your current front text matches the live spec wording or that one recorded alias, nothing earlier. Cards whose front has never changed match by plain text equality and need no alias at all, which covers most of them. A card reworded before this tracking convention existed, with no alias entry to bridge the gap, imports as a new, separate card instead of updating your existing one, your old card isn't touched or lost, you'd just end up with both.

The field snapshot and GUID matching (though not the backup, which is always a real Anki export/backup regardless of scope) are limited to `scope_tag` (default `InternPearls`). Cards outside that tag are ignored entirely.

With the automatic backup in place, any of this is fully reversible even if you skip a manual export.

The add-on's own record of which deck versions you've already synced lives in a `user_files/` subfolder, which Anki preserves across add-on updates (everything else in the add-on's folder gets replaced fresh). Earlier versions kept this file elsewhere, so updating the add-on itself would reset it and make the next Sync treat every deck as new; that's fixed as of v0.7.0.

## Using this for your own decks

Nothing about Sync decks, Configure deck source, or the backup/export/import tools is specific to any particular deck's content. To point this add-on at your own decks, host a `manifest.json` in a GitHub repo (private or public) or a local folder, alongside the `.apkg` files it references:

```json
{
  "schema": 1,
  "decks": [
    {
      "name": "Your Deck::Subdeck",
      "apkg": "decks/your-deck.apkg",
      "spec": "specs/your-deck.json",
      "version": "a1b2c3d4",
      "cards": 42
    }
  ],
  "front_aliases": {}
}
```

- `decks` lists every deck Sync should manage. `name` is the deck name as it should appear in Anki; `apkg` is the path to fetch, relative to the repo/folder root (a flat filename or nested in a subfolder like `decks/your-deck.apkg`, both work); `spec` is informational only (not read by the add-on); `version` is any string that changes when the deck changes (a hash, a date, a counter) and drives which decks Sync considers "changed"; `cards` is optional, shown as a count in the sync confirmation.
- `front_aliases` maps a card's current front-field text to its previous wording, for any card whose front changed since the last version someone might be syncing from. Omit entries for cards whose front never changed. See "How history is preserved" above for exactly how this is used and its limits.
- Each `.apkg`'s notes need a stable GUID scheme of your own choosing (most Anki deck-building tools default to a content hash of the front, which changes whenever you reword it, hence `front_aliases` existing at all). This add-on doesn't generate decks, only syncs pre-built ones; how you build stable GUIDs into your `.apkg` is up to your own tooling.

Point Configure deck source at your repo (with a read-only token if private) or folder, and Sync decks, Configure deck source, and the Advanced tools all work exactly as described above, just against your own content. Set `scope_tag` and `export_deck` in Config to match your own deck's tag and name if they differ from the `InternPearls` / `Intern Pearls::Intern Custom` defaults.

## For developers

### Code layout

`internpearls/logic.py` holds everything that doesn't touch `aqt`/`anki`: apkg reading
and rewriting, GUID matching, version comparison, interval clamping, the add-on-update
decision (`decide_addon_update_action`), HTML formatting. Everything else (dialogs, menu
wiring, the actual Anki API calls) stays in `internpearls/__init__.py`. A new function
belongs in `logic.py` if it could be tested with plain Python and no Anki install; if it
needs `mw` or `col`, it belongs in `__init__.py`.

The two checks that run on their own (the add-on-update check and the deck auto-sync
poll) dispatch their network work through `_run_in_background()`, which uses Anki's
`QueryOp` to run off the main thread when it's available, falling back to running
inline if not. Only the part that actually touches `mw.col` (backing up and importing,
which only happens when something changed) runs on the main thread; that matches the
cost a manual Sync decks click already pays, so it isn't the part that needed fixing.

### Running tests

```bash
pip install pytest
cd addon && pytest tests/ -v
```

Tests only cover `logic.py`, no Anki install or running Anki instance needed. They
build a minimal fake `.apkg` (just a `notes` table, the only part this code reads or
writes) rather than a full Anki collection.

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.11.0 to 0.11.1): bug fix or internal cleanup, no UI changes.
- MINOR (0.11.0 to 0.12.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/__init__.py` and `version` in `version.json`, tag the commit `vX.Y.Z`, add an entry to `CHANGELOG.md`, run `./build.sh`, and push.
