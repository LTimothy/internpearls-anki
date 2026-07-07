# Intern Pearls Deck Tools (Anki add-on)

One-click, **history-safe** updates for a set of Anki study decks. **Sync decks** pulls the
latest decks from a source you configure and applies each one so your **review history**
and personal **Notes** are preserved — Anki normally overwrites fields on a re-import, so
this matches cards by ID (to keep scheduling) and snapshots/restores your Notes.

## Install
1. Download **`internpearls.ankiaddon`** from this repo.
2. Anki → **Tools → Add-ons → Install from file…** → pick it → restart.

A top-level **Intern Pearls** menu appears (next to Help):
- **Sync decks** — the only button you normally need.
- **Configure deck source…** — set where decks come from (see below).
- **Check for add-on updates** — pulls the newest add-on from this repo.
- **Advanced** ▸ *Import single deck*, *Fix note types*, *Restore my notes* (fallbacks;
  Sync does these automatically).
- **About…**

## Configure the deck source
Run **Intern Pearls → Configure deck source…** and choose one:
- **GitHub** — enter a decks repository (`owner/name`) and a **read-only** access token.
  The token is stored locally in your add-on config and is never shared or committed.
- **Local folder** — a folder that contains `manifest.json` and the `.apkg` files.

(These are also editable under Tools → Add-ons → Intern Pearls Deck Tools → Config:
`github_decks_repo`, `github_token`, `decks_dir`, plus `scope_tag` / `protected_fields`.)

## Updating
1. **Back up:** File → Export → *Anki Collection Package* (include scheduling).
2. **Intern Pearls → Sync decks.** Only changed decks are touched (tracked by version).

## Safety
Scheduling stays on your cards; Notes are snapshotted/restored; note-type changes only
*add* fields. A pre-import backup makes any step reversible.

## Repackage after editing
```bash
./build.sh          # rebuilds internpearls.ankiaddon
```
