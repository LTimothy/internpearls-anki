# Intern Pearls Deck Tools (Anki add-on)

Keeps a set of Anki study decks up to date without losing your review history or personal notes. Anki's built-in re-import overwrites fields, which means any notes you added to cards get wiped. This add-on avoids that by matching cards by GUID (so scheduling carries over) and snapshotting/restoring the Notes field around each import.

## Install

1. Download `internpearls.ankiaddon` from this repo.
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help.

## Menu reference

### Sync decks

The main button. It fetches `manifest.json` from your configured deck source, compares each deck's version hash against what you last synced, and only imports decks that changed. For each deck it:

1. Adds any missing fields to the note type (never removes or renames existing fields).
2. Snapshots your Notes field on every card in scope.
3. Matches each incoming card to your existing card by GUID so review history carries over. If a card's front was reworded since your last sync, it checks the `front_aliases` map in the manifest to find the match.
4. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
5. Restores Notes from the snapshot.

If no deck source is configured, it tells you to run Configure deck source first.

### Configure deck source

A dialog with two options:

- GitHub: enter the repo (`owner/name`) and a read-only personal access token. The token is stored in your local add-on config and never leaves your machine.
- Local folder: point it at a directory that contains `manifest.json` and the `.apkg` files.

You can also edit these directly under Tools > Add-ons > Intern Pearls Deck Tools > Config:

| Key | What it does |
|---|---|
| `github_decks_repo` | GitHub repo, e.g. `owner/repo-name` |
| `github_token` | Read-only fine-grained personal access token |
| `github_ref` | Branch or tag to pull from (default: `main`) |
| `decks_dir` | Local folder path; if set, GitHub is ignored |
| `scope_tag` | Root tag identifying cards this add-on manages (default: `InternPearls`). Scopes snapshots and GUID matching so your other decks are never touched. |
| `protected_fields` | Field names to snapshot and restore (default: `["Notes"]`). Add any field where you keep your own content. |

### Check for add-on updates

Compares your installed version against `version.json` in this repo. If a newer version exists, it downloads and installs the `.ankiaddon`. You still need to restart Anki afterward.

### Advanced submenu

These are the individual steps that Sync runs automatically. They are exposed as fallbacks if something goes wrong or you want to run one piece in isolation.

**Import single deck (manual)** opens a file picker for one `.apkg`. It runs the same GUID matching and personalization as Sync, writes a `.forreview.apkg` to disk, and tells you to double-click it. After importing, run Advanced > Restore my notes to get your Notes back.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Sync runs this before every import.

**Restore my notes** reads the last Notes snapshot from disk and writes those values back to your cards. The snapshot is created automatically during Sync. Use this if an import or manual edit accidentally overwrites your personal notes.

### About

Shows the installed version and a link to this repo.

## Updating decks

1. Back up first: File > Export > Anki Collection Package (check "include scheduling information").
2. Intern Pearls > Sync decks. Only changed decks are imported.

## How history is preserved

Cards are matched by GUID, not by content, so your intervals, ease factors, and review counts carry over on every sync.

The Notes field is snapshotted before import and restored after, so even if the importer overwrites it, your text comes back.

Note types only gain fields; nothing is removed or renamed. If you have customized a note type, those customizations stay.

When a card's front text changes between deck versions, a `front_aliases` entry in the manifest maps the new wording to the old one. The add-on uses this to find your existing card instead of creating a duplicate.

Everything is scoped by `scope_tag` (default `InternPearls`). Cards outside that tag are ignored entirely.

Back up before syncing and any of this is fully reversible.

## For developers

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.5.0 to 0.5.1): bug fix or internal cleanup, no UI changes.
- MINOR (0.5.1 to 0.6.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/__init__.py` and `version` in `version.json`, tag the commit `vX.Y.Z`, run `./build.sh`, and push.
