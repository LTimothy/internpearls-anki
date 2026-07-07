# Intern Pearls Deck Tools (Anki add-on)

Keeps a set of Anki study decks up to date without losing your review history or personal notes. Anki's built-in re-import overwrites fields, which means any notes you added to cards get wiped. This add-on avoids that by matching cards by GUID (so scheduling carries over), snapshotting and restoring the Notes field around each import, and backing up your whole collection automatically before it touches anything.

## Install

1. Download `internpearls.ankiaddon` from this repo.
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help.

## Menu reference

### Sync decks

The main button. It fetches `manifest.json` from your configured deck source, compares each deck's version hash against what you last synced, and only imports decks that changed. The confirmation dialog lists each affected deck with its card count, so you know the scope before anything happens. For each deck it:

1. Takes a fresh, timestamped backup of your whole collection first (the same mechanism Anki uses on its own, saved to your normal Anki backups folder). Nothing else runs until this succeeds, or you explicitly choose to continue without one.
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

### Check for add-on updates

Compares your installed version against `version.json` in this repo. If a newer version exists, it downloads and installs the `.ankiaddon`. You still need to restart Anki afterward.

### Advanced submenu

These are the individual steps that Sync runs automatically. They are exposed as fallbacks if something goes wrong or you want to run one piece in isolation.

**Import single deck (manual)** opens a file picker for one `.apkg`. It runs the same GUID matching, personalization, and automatic backup as Sync, writes a `.forreview.apkg` to disk, and tells you to double-click it. After importing, run Advanced > Restore my notes to get your Notes back.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Sync runs this before every import.

**Restore my notes** reads the last Notes snapshot from disk and writes those values back to your cards. The snapshot is created automatically during Sync. Use this if an import or manual edit accidentally overwrites your personal notes.

### About

Shows the installed version and a link to this repo.

## Updating decks

Just run Intern Pearls > Sync decks. Only changed decks are imported, and the add-on backs up your collection automatically before touching anything, so there's no separate step to remember. If you ever want a manual backup on top of that, it's File > Export > Anki Collection Package (check "include scheduling information").

## How history is preserved

Every sync and manual import starts with a fresh, timestamped backup of the whole collection, written to Anki's own backups folder. If a backup can't be created for some reason, you're asked whether to proceed anyway rather than being blocked or silently continuing.

Cards are matched by GUID, not by content, so your intervals, ease factors, and review counts carry over on every sync.

The Notes field is snapshotted before import and restored after, so even if the importer overwrites it, your text comes back.

Note types only gain fields; nothing is removed or renamed. If you have customized a note type, those customizations stay.

When a card's front text changes between deck versions, a `front_aliases` entry in the manifest maps the new wording to the old one. The add-on uses this to find your existing card instead of creating a duplicate. If that mapping can't be fetched for some reason, you're warned before anything imports, since any reworded card would otherwise import as new and lose its history silently.

Everything is scoped by `scope_tag` (default `InternPearls`). Cards outside that tag are ignored entirely.

With the automatic backup in place, any of this is fully reversible even if you skip the manual export step above.

## For developers

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.6.0 to 0.6.1): bug fix or internal cleanup, no UI changes.
- MINOR (0.6.0 to 0.7.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/__init__.py` and `version` in `version.json`, tag the commit `vX.Y.Z`, run `./build.sh`, and push.
