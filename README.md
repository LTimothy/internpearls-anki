# Intern Pearls Deck Tools (Anki add-on)

[![Latest release](https://img.shields.io/github/v/release/LTimothy/internpearls-anki)](https://github.com/LTimothy/internpearls-anki/releases/latest)
[![License: MIT](https://img.shields.io/github/license/LTimothy/internpearls-anki)](LICENSE)

**Update shared Anki decks without losing your review history or the notes you've written on cards.**

**[Try the live demo](https://ltimothy.github.io/internpearls-anki/)** — the add-on's actual Python code running in your browser (only Anki itself is simulated): publish a deck update, sync it through the real dialogs, and watch scheduling and personal notes survive. No install, nothing leaves the page.

If you maintain a deck for a study group (or subscribe to one someone else maintains), you've hit the problem: re-importing an updated `.apkg` overwrites every field, wiping the personal annotations people keep on their cards, and a reworded card silently loses its scheduling. This add-on fixes both. One click pulls only the decks that changed from a GitHub repo or local folder, matches cards by GUID so intervals and ease factors carry over, snapshots and restores whichever fields you've marked as yours (`Notes` by default), and backs up the deck automatically before touching anything. It can also sync and update itself on a schedule, if you turn that on.

What it does, in short:

- **Sync only what changed** — a manifest hash per deck means editing one deck doesn't re-import ten.
- **Keep review history** — cards match by GUID (with a rename map for reworded fronts), so scheduling survives every update.
- **Keep your annotations** — configurable preserved fields are snapshotted before import and restored after.
- **Back up first, always** — a timestamped `.apkg` of the deck is taken before any import, pruned to the last 10.
- **Stay current on its own** — optional background auto-sync for decks, and self-update for the add-on.

No deck content ships with the add-on itself; it only syncs whatever you point it at. Point Manage decks at your own GitHub repo or local folder and it works the same way for any decks that follow the same manifest format. See "Using this for your own decks" below.

See `CHANGELOG.md` for what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from the [latest release](https://github.com/LTimothy/internpearls-anki/releases/latest).
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help. Two primary actions sit at the top (Sync decks, Manage decks); occasional tools live under Advanced; Settings and About sit at the bottom.

No deck source yet? Open Manage decks > Configure source and pick "Try the example deck" — it points the add-on at a small public demo repo so you can watch a sync work end to end, then swap in your own source later.

## Menu reference

### Sync decks

The main button. It fetches `manifest.json` from your configured deck source, compares each deck's version hash against what you last synced, and only imports decks that changed. The confirmation dialog lists each affected deck with its card count, flagging any deck you've never synced before as a new deck, so you know the scope before anything happens. For each deck it:

1. Takes a fresh, timestamped backup of just the configured deck first (a self-contained `.apkg` with scheduling included, saved internally and pruned to the most recent 10). Nothing else runs until this succeeds, or you explicitly choose to continue without one.
2. Adds any missing fields to the note type (never removes or renames existing fields).
3. Snapshots your `protected_fields` on every card in scope.
4. Matches each incoming card to your existing card — by GUID first, then by front text, then via the `front_aliases` map in the manifest for a card whose front was reworded since your last sync — so review history carries over.
5. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
6. Restores the preserved fields from the snapshot.

If an update also changes how cards *look* (a card template or its CSS — the one thing these imports deliberately never touch, see "How history is preserved"), Sync says so afterward and offers to apply the new look. Saying yes updates the note type's templates and styling, which Anki treats as a schema change: your next AnkiWeb sync will be a one-time full sync ("Upload to AnkiWeb"). Saying no keeps your current card appearance; the content update has already imported either way, and the next template change will offer again.

If no deck source is configured, it tells you to open Manage decks and use Configure source.

### Manage decks

A panel listing every deck the source offers, each with a checkbox, a status pill (New, Update available, or Up to date), and its card count. Unchecking a deck stops future syncs for it; cards already imported stay in your collection until you delete them yourself in Anki. A "Check what will sync" button downloads the changed decks and fills in, per deck, how many cards would update in place versus be added as new; nothing is imported by clicking it. The same panel edits `protected_fields`. Save keeps the choices for your next sync; Save and sync now also runs Sync decks right away.

Deck-source configuration lives here too, behind a button next to the "Source" line at the top: "Configure source" if nothing is set up yet, "Change source" once something is. It opens the same dialog either way, with three buttons plus Cancel:

- GitHub repo: enter the repo (`owner/name`) and, only if the repo is private, a read-only personal access token — leave the token blank for a public repo. The token field is masked as you type, and the value is stored only in your local add-on config; it never leaves your machine except in requests to GitHub.
- Local folder: point it at a directory that contains `manifest.json` and the `.apkg` files.
- Try the example deck: points the add-on at [`LTimothy/internpearls-example-deck`](https://github.com/LTimothy/internpearls-example-deck), a small public demo repo, so you can watch a sync work before you have any deck source of your own. Choosing it also points `scope_tag` and `export_deck` at the example deck's values (only if you haven't customized them), so field preservation and the automatic backup work in the demo too; picking a GitHub repo or local folder later resets exactly those injected values.

Either way, as soon as you save, the add-on connects to the source, and Manage decks reopens against it: how many decks it found, or (shown as an error in the Source line, with an empty deck list and the same button waiting) exactly what went wrong, a bad token, an unreachable repo, or a wrong folder, so you're never left staring at a dead end. If nothing is configured at all, Manage decks still opens; it just shows an empty list and the Configure source button, instead of a warning that sends you hunting for a different menu item.

You can also edit these directly under Tools > Add-ons > Intern Pearls Deck Tools > Config:

| Key | What it does |
|---|---|
| `github_decks_repo` | GitHub repo, e.g. `owner/repo-name` |
| `github_token` | Read-only fine-grained personal access token; leave blank for a public repo |
| `github_ref` | Branch or tag to pull from (default: `main`) |
| `decks_dir` | Local folder path, used when `github_decks_repo` is empty |
| `scope_tag` | Root tag identifying cards this add-on manages (default: `InternPearls`). Scopes snapshots and GUID matching so your other decks are never touched. |
| `protected_fields` | Field names to snapshot and restore (default: `["Notes"]`). Add any field where you keep your own content. Also editable from Manage decks. |
| `excluded_decks` | Deck names opted out of syncing. Also editable from Manage decks. |
| `export_deck` | The deck that Backup/Import/Export intern pearls deck and the automatic pre-sync backup operate on (default: `Intern Pearls::Intern Custom`). |
| `auto_sync_decks`, `auto_sync_interval_minutes`, `notify_addon_updates`, `auto_update_addon` | Sync and update automation, see Settings below and `config.md` for details on each. |

### Advanced submenu

Occasional tools, tucked away from the two primary actions at the top:

**Import single deck (manual)** picks one `.apkg` outside your configured source, for a deck someone sent you directly or a build you're testing before pushing it live. It runs the same personalization, automatic backup, and note restore as Sync, just for the one file you choose.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Sync runs this before every import.

**Reconcile my decks** finds *retired* cards still in your collection and archives them out of the way. When a deck splits one bulky card into several focused ones, or reword-replaces a card, the old version's identity changes — so a sync adds the new cards but never removes your copy of the old one, and it lingers as a duplicate in your reviews. This reads the retirement ledger the deck source ships (in its manifest) and, for each retired card you still have, moves it to an `…::Retired` subdeck, suspends it, and tags it `…::retired`. **It never deletes anything**: your review history is kept, and you can bring any card back by unsuspending it or moving it out of the Retired deck. A backup is taken automatically first, and re-running it skips cards it already archived. If some replacements aren't in your collection yet, it says so and suggests running Sync decks first.

**Backup intern pearls deck** is the manual, on-demand version of the automatic pre-sync backup: a fresh `.apkg` of just the configured deck (`export_deck`), with scheduling included, saved internally and pruned to the most recent 10. Use it right before poking at cards yourself outside the add-on.

**Import intern pearls deck** brings a previous deck backup or export back in. The file picker defaults to the internal backups folder, but you can browse to any matching `.apkg`. Since the file's own GUIDs already came from a real collection, this is a plain import with scheduling restored, matching cards update in place and anything missing is added as new; no personalization step is needed the way Sync and Import single deck need it for a spec-authored deck from someone else's collection.

**Export intern pearls deck** writes a standalone `.apkg` of just the configured deck, with your review history, deck options, and media all included, the same result as Anki's own File > Export > Anki Deck Package with every checkbox checked. This is the same export the automatic backup and Backup intern pearls deck use, just prompting you for where to save it, meant to be kept or shared on its own rather than used purely to undo a sync.

**Backup full collection** takes a full, whole-collection backup on demand, the same kind that used to run automatically before every sync. Use this for broader protection than the deck-scoped default covers. Retention for these is whatever Anki's own preferences specify, not this add-on's 10-backup limit, which only applies to the deck-scoped backups above.

**Restore full collection** opens Anki's own backup picker (the same one under File > Switch Profile > Open Backup) pointed at your backups folder, so you can revert a full collection backup if something looks wrong. This replaces your entire collection, every deck, not just the ones this add-on manages, since that's what a real collection backup contains. Anki asks you to confirm the specific backup file before doing anything.

**Check for add-on updates** compares your installed version against the public repo's `version.json`. If a newer version exists, it offers to download and install the `.ankiaddon`. You still need to restart Anki afterward. This is the on-demand version of what the Settings toggles below do on their own, which is why most people never need it: it's here as a fallback, not the primary way to stay current.

### Settings

Sync automation and add-on update behavior, kept separate from Manage decks since those answer a different question ("which decks, which fields, from where" versus "how automatic, how often"):

- **Sync decks automatically when updates are available**, off by default. When on, the add-on checks the source in the background on the interval below and applies any changed decks without asking, backing up first the same as a manual sync. The one thing it never applies unattended is a card-template change (that would force a one-time full AnkiWeb sync without anyone consenting to it): a deck update that includes one is held back and stays pending, and a tooltip points you at Sync decks to review it.
- **Check every N minutes**, default 15, minimum 1. The check runs off the main thread when Anki supports it (essentially all current versions do), so it doesn't freeze Anki even at a short interval; if it can't reach the source, it fails within a few seconds and just tries again next time.
- **Notify me when a new add-on version is out**, on by default. A tooltip once per new release, no installation.
- **Install add-on updates automatically**, off by default. Downloads and installs a newer version as part of the same once-per-launch check, no confirmation. A restart is still needed to load it, same as installing by hand.

### About

A short description of what the add-on does, a summary of your current settings (auto-sync, add-on updates, preserved fields), a reminder that no deck content ships with it, and a link to this repo.

## Updating decks

Run Intern Pearls > Sync decks, or turn on "Sync decks automatically when updates are available" in Settings so it happens on its own. Either way, only changed decks are imported, and the add-on backs up the deck automatically before touching anything, so there's no separate step to remember. For broader protection on top of that, Advanced > Backup full collection takes a whole-collection backup on demand.

## How history is preserved

Every sync and manual import starts with a fresh, timestamped backup, by default scoped to just the configured deck (fast, self-contained, includes scheduling). A full, whole-collection backup is still one click away under Advanced > Backup full collection for broader protection; it's just no longer the automatic default, since most syncs only ever need to undo changes to this one deck. If a backup can't be created for some reason, you're asked whether to proceed anyway rather than being blocked or silently continuing (an automatic background sync skips that round instead of asking, since there's no one there to answer). On someone's very first sync, before the deck exists yet, there's nothing to back up and this step is skipped entirely.

Cards are matched by GUID, not by content, so your intervals, ease factors, and review counts carry over on every sync.

Your `protected_fields` (`Notes` by default, configurable to any field name, or several) are snapshotted before import and restored after, so even if the importer overwrites them, your text comes back. Specifically: before anything runs, every note tagged under `scope_tag` has its `protected_fields` values read and saved by GUID; after the import, whatever note currently holds that GUID gets those exact values written back. It's a read-before, write-after round trip, not a merge, and it only protects notes that keep their GUID through the import. A card that imports as new (see below) has no old snapshot value to restore, since there was nothing recorded for a GUID that didn't exist before.

Note types only gain fields; nothing is removed or renamed. If you have customized a note type, those customizations stay.

Matching runs strongest-signal-first. If an incoming note's GUID already belongs to one of your cards, that's the match — no text comparison needed. Deck sources that keep GUIDs stable (an explicit per-card `id` in the deck spec, so rewording a front doesn't re-identify the card) get this for every card, which means a front can be reworded any number of times between your syncs and history still carries over, with no alias bookkeeping at all.

For cards where the GUID doesn't match (typically a collection whose cards predate stable ids), matching falls back to front text: your card's current front is compared against the live spec wording first, then — when a card's front changed between deck versions — against the one prior wording recorded in the manifest's `front_aliases` map. If that mapping can't be fetched for some reason, you're warned before anything imports, since a reworded card would otherwise import as new and lose its history silently.

`front_aliases` only bridges the *most recent* rename of a given card, not its full history. So on the fallback path, whether a specific card's history carries over depends on whether your current front text matches the live spec wording or that one recorded alias, nothing earlier. Cards whose front has never changed match by plain text equality and need no alias at all, which covers most of them. A card that misses on GUID, front, and alias imports as a new, separate card instead of updating your existing one — your old card isn't touched or lost, you'd just end up with both.

Card *appearance* is handled separately from card content. Imports here run with note-type merging off (see the trade-off in "For developers" — it keeps AnkiWeb syncs incremental), so a template or CSS change in a rebuilt deck never applies silently: Sync detects it, tells you, and asks before applying, since applying costs a one-time full AnkiWeb sync. Background auto-sync never applies one; it holds that deck for a manual sync instead.

The field snapshot and GUID matching (though not the backup, which is always a real Anki export/backup regardless of scope) are limited to `scope_tag` (default `InternPearls`). Cards outside that tag are ignored entirely.

With the automatic backup in place, any of this is fully reversible even if you skip a manual export.

The automatic deck-scoped backups also live in that `user_files/` subfolder inside the add-on's own directory, so they survive add-on updates but not an add-on *uninstall* — export anything you want to keep long-term (Advanced > Export intern pearls deck) before removing the add-on.

The add-on's own record of which deck versions you've already synced lives in a `user_files/` subfolder, which Anki preserves across add-on updates (everything else in the add-on's folder gets replaced fresh). Earlier versions kept this file elsewhere, so updating the add-on itself would reset it and make the next Sync treat every deck as new; that's fixed as of v0.7.0.

## Using this for your own decks

Nothing about Sync decks, Manage decks, or the backup/export/import tools is specific to any particular deck's content. To point this add-on at your own decks, host a `manifest.json` in a GitHub repo (private or public) or a local folder, alongside the `.apkg` files it references:

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
- Each `.apkg`'s notes need a stable GUID scheme of your own choosing. Most Anki deck-building tools default to a content hash of the front, which changes whenever you reword it — that's why `front_aliases` exists. The better scheme is a GUID derived from an explicit per-card id that never changes: the add-on matches by GUID before any text comparison, so with stable GUIDs you can reword fronts freely and never touch `front_aliases` again. This add-on doesn't generate decks, only syncs pre-built ones; how you build stable GUIDs into your `.apkg` is up to your own tooling.

Use Configure source, inside Manage decks, to point at your repo (with a read-only token if private) or folder, and Sync decks, Manage decks, and the Advanced tools all work exactly as described above, just against your own content. Set `scope_tag` and `export_deck` in Config to match your own deck's tag and name if they differ from the `InternPearls` / `Intern Pearls::Intern Custom` defaults.

## For developers

### Code layout

`internpearls/logic.py` holds everything that doesn't touch `aqt`/`anki`: apkg reading
and rewriting, GUID matching, version comparison, interval clamping, the add-on-update
decision (`decide_addon_update_action`), HTML formatting. A new function belongs in
`logic.py` if it could be tested with plain Python and no Anki install.

Everything that does touch Anki is split by concern:

- `internpearls/__init__.py` — the menu and startup hook wiring only.
- `internpearls/config.py` — constants (including `ADDON_VERSION`), config access,
  persistent state under `user_files/`.
- `internpearls/ui.py` — the `_info`/`_warn`/`_ask`/`_prompt` dialog wrappers, the
  `_safe`/`_bg_safe` error decorators, and shared label/button styling helpers.
- `internpearls/net.py` — HTTP and GitHub contents-API fetches, the timeout policy.
- `internpearls/collection.py` — everything that reads or writes `mw.col`: note-type
  reconciliation, backups, the protected-fields snapshot/restore, apkg import/export,
  and the Advanced menu actions over those helpers.
- `internpearls/sync.py` — the sync flows: source resolution (`_fetch_manifest`),
  Sync decks, the shared `_run_sync` sequence, Import single deck.
- `internpearls/updates.py` — add-on self-update: version fetch, package download,
  the manual check.
- `internpearls/background.py` — `_run_in_background` (QueryOp dispatch), the startup
  update check, the auto-sync poll and its timer.
- `internpearls/dialogs.py` — Manage decks, Settings, About, and source configuration.

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

No Anki install or running Anki instance is needed for any of them. Three layers,
all built on the mock Anki in `tests/mock_anki.py` (stub `aqt`/`anki` modules plus a
mock collection that emulates the one importer behavior everything here defends
against — a GUID-matched import overwrites every field):

- `tests/test_logic.py` unit-tests `logic.py` against minimal mock `.apkg` files.
- `tests/test_sync_flows.py` drives the real `sync`, `collection`, and `background`
  modules end to end, with dialog answers scripted per test — first sync,
  protected-field restore, reworded fronts, template consent, auto-sync deferral.
- `tests/test_dialogs.py` drives the real Qt dialog code in `dialogs.py` and the
  real menu from `__init__.py`: the mock Qt widgets serialize each dialog to a
  tree, tests script clicks and edits against it, and a snapshot-and-replay runner
  re-executes the flow deterministically past each answer. This is the same
  protocol the live demo uses, so the demo executes exactly what these tests cover.

The [live demo](https://ltimothy.github.io/internpearls-anki/) is generated from the
code, not written alongside it: `./build.sh` mirrors `internpearls/` into
`docs/addon/` (byte-equality enforced by `tests/test_demo_parity.py`), and the page
runs those modules under Pyodide against the example deck repo's real files.

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.11.0 to 0.11.1): bug fix or internal cleanup, no UI changes.
- MINOR (0.11.0 to 0.12.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/config.py` and `version` in `version.json`, tag the commit `vX.Y.Z`, add an entry to `CHANGELOG.md`, run `./build.sh`, and push.
