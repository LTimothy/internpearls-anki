# Intern Pearls Deck Tools (Anki add-on)

History-safe deck management for Anki. Keeps a set of study decks up to date without losing your review history or personal notes, and lets you back up, export, or import a deck without leaving Anki. Anki's built-in re-import overwrites fields, which means any notes you added to cards get wiped. This add-on avoids that by matching cards by GUID (so scheduling carries over), snapshotting and restoring the Notes field around each import, and backing up the deck automatically before it touches anything.

The decks that ship with it are one set of anesthesia study material, but the add-on itself doesn't care what's in them. Point Configure deck source at your own GitHub repo or local folder and it works the same way for any decks that follow the same manifest format. See "Using this for your own decks" below.

## Install

1. Download `internpearls.ankiaddon` from this repo.
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help.

## Menu reference

### Sync decks

The main button. It fetches `manifest.json` from your configured deck source, compares each deck's version hash against what you last synced, and only imports decks that changed. The confirmation dialog lists each affected deck with its card count, so you know the scope before anything happens. For each deck it:

1. Takes a fresh, timestamped backup of just the configured deck first (a self-contained `.apkg` with scheduling included, saved internally and pruned to the most recent 10). Nothing else runs until this succeeds, or you explicitly choose to continue without one.
2. Adds any missing fields to the note type (never removes or renames existing fields).
3. Snapshots your Notes field on every card in scope.
4. Matches each incoming card to your existing card by GUID so review history carries over. If a card's front was reworded since your last sync, it checks the `front_aliases` map in the manifest to find the match.
5. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
6. Restores Notes from the snapshot.

If no deck source is configured, it tells you to run Configure deck source first.

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
| `protected_fields` | Field names to snapshot and restore (default: `["Notes"]`). Add any field where you keep your own content. |
| `export_deck` | The deck that Backup/Import/Export intern pearls deck and the automatic pre-sync backup operate on (default: `Intern Pearls::Intern Custom`). |

### Check for add-on updates

Compares your installed version against `version.json` in this repo. If a newer version exists, it downloads and installs the `.ankiaddon`. You still need to restart Anki afterward.

### Advanced submenu

**Import single deck (manual)** picks one `.apkg` outside your configured source, for a deck someone sent you directly or a build you're testing before pushing it live. It runs the same personalization, automatic backup, and note restore as Sync, just for the one file you choose.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Sync runs this before every import.

**Backup intern pearls deck** is the manual, on-demand version of the automatic pre-sync backup: a fresh `.apkg` of just the configured deck (`export_deck`), with scheduling included, saved internally and pruned to the most recent 10. Use it right before poking at cards yourself outside the add-on.

**Import intern pearls deck** brings a previous deck backup or export back in. The file picker defaults to the internal backups folder, but you can browse to any matching `.apkg`. Since the file's own GUIDs already came from a real collection, this is a plain import with scheduling restored, matching cards update in place and anything missing is added as new; no personalization step is needed the way Sync and Import single deck need it for a spec-authored deck from someone else's collection.

**Export intern pearls deck** writes a standalone `.apkg` of just the configured deck, with your review history, deck options, and media all included, the same result as Anki's own File > Export > Anki Deck Package with every checkbox checked. This is the same export the automatic backup and Backup intern pearls deck use, just prompting you for where to save it, meant to be kept or shared on its own rather than used purely to undo a sync.

**Backup full collection** takes a full, whole-collection backup on demand, the same kind that used to run automatically before every sync. Use this for broader protection than the deck-scoped default covers. Retention for these is whatever Anki's own preferences specify, not this add-on's 10-backup limit, which only applies to the deck-scoped backups above.

**Restore full collection** opens Anki's own backup picker (the same one under File > Switch Profile > Open Backup) pointed at your backups folder, so you can revert a full collection backup if something looks wrong. This replaces your entire collection, every deck, not just the ones this add-on manages, since that's what a real collection backup contains. Anki asks you to confirm the specific backup file before doing anything.

### About

An overview of what the add-on does, a short description of each menu item, and a link to this repo.

## Updating decks

Just run Intern Pearls > Sync decks. Only changed decks are imported, and the add-on backs up the deck automatically before touching anything, so there's no separate step to remember. For broader protection on top of that, Advanced > Backup full collection takes a whole-collection backup on demand.

## How history is preserved

Every sync and manual import starts with a fresh, timestamped backup, by default scoped to just the configured deck (fast, self-contained, includes scheduling). A full, whole-collection backup is still one click away under Advanced > Backup full collection for broader protection, it's just no longer the automatic default, since most syncs only ever need to undo changes to this one deck. If a backup can't be created for some reason, you're asked whether to proceed anyway rather than being blocked or silently continuing; on someone's very first sync, before the deck exists yet, there's nothing to back up and this step is skipped entirely.

Cards are matched by GUID, not by content, so your intervals, ease factors, and review counts carry over on every sync.

The Notes field is snapshotted before import and restored after, so even if the importer overwrites it, your text comes back. Specifically: before anything runs, every note tagged under `scope_tag` has its `protected_fields` values read and saved by GUID; after the import, whatever note currently holds that GUID gets those exact values written back. It's a read-before, write-after round trip, not a merge, and it only protects notes that keep their GUID through the import. A card that imports as new (see below) has no old snapshot value to restore, since there was nothing recorded for a GUID that didn't exist before.

Note types only gain fields; nothing is removed or renamed. If you have customized a note type, those customizations stay.

When a card's front text changes between deck versions, a `front_aliases` entry in the manifest maps the new wording to the one immediately before it. The add-on checks your card's current front text against the live spec wording first, then against that one prior wording if the first check fails. If that mapping can't be fetched for some reason, you're warned before anything imports, since any reworded card would otherwise import as new and lose its history silently.

This means `front_aliases` only bridges the *most recent* rename of a given card, not its full history. On a brand new install, whether a specific card's history carries over depends only on whether your current front text matches the live spec wording or that one recorded alias, nothing earlier. Cards whose front has never changed match by plain text equality and need no alias at all, which covers most of them. A card reworded before this tracking convention existed, with no alias entry to bridge the gap, imports as a new, separate card instead of updating your existing one, your old card isn't touched or lost, you'd just end up with both.

The Notes snapshot and GUID matching (though not the backup, which is always a real Anki export/backup regardless of scope) are limited to `scope_tag` (default `InternPearls`). Cards outside that tag are ignored entirely.

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
      "apkg": "your-deck.apkg",
      "spec": "your-deck.json",
      "version": "a1b2c3d4",
      "cards": 42
    }
  ],
  "front_aliases": {}
}
```

- `decks` lists every deck Sync should manage. `name` is the deck name as it should appear in Anki; `apkg` is the filename to fetch (relative to the repo/folder root); `spec` is informational only (not read by the add-on); `version` is any string that changes when the deck changes (a hash, a date, a counter) and drives which decks Sync considers "changed"; `cards` is optional, shown as a count in the sync confirmation.
- `front_aliases` maps a card's current front-field text to its previous wording, for any card whose front changed since the last version someone might be syncing from. Omit entries for cards whose front never changed. See "How history is preserved" above for exactly how this is used and its limits.
- Each `.apkg`'s notes need a stable GUID scheme of your own choosing (most Anki deck-building tools default to a content hash of the front, which changes whenever you reword it, hence `front_aliases` existing at all). This add-on doesn't generate decks, only syncs pre-built ones; how you build stable GUIDs into your `.apkg` is up to your own tooling.

Point Configure deck source at your repo (with a read-only token if private) or folder, and Sync decks, Configure deck source, and the Advanced tools all work exactly as described above, just against your own content. Set `scope_tag` and `export_deck` in Config to match your own deck's tag and name if they differ from the `InternPearls` / `Intern Pearls::Intern Custom` defaults.

## For developers

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.10.1 to 0.10.2): bug fix or internal cleanup, no UI changes.
- MINOR (0.10.1 to 0.11.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/__init__.py` and `version` in `version.json`, tag the commit `vX.Y.Z`, run `./build.sh`, and push.
