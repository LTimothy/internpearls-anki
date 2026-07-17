# Intern Pearls Deck Tools (Anki add-on)

[![Latest release](https://img.shields.io/github/v/release/LTimothy/internpearls-anki)](https://github.com/LTimothy/internpearls-anki/releases/latest)
[![License: MIT](https://img.shields.io/github/license/LTimothy/internpearls-anki)](LICENSE)

**Update shared Anki decks without losing your review history or the notes you've written on cards.**

**[Try the live demo](https://ltimothy.github.io/internpearls-anki/)**: the add-on's actual Python code running in your browser (only Anki itself is simulated). Publish a deck update, sync it through the real dialogs, and watch scheduling and personal notes survive. No install, and nothing leaves the page.

If you maintain a deck for a study group (or subscribe to one someone else maintains), you've hit the problem: shared decks have no update path. Re-importing an updated `.apkg` overwrites every field, wiping the annotations people keep on their cards, and a reworded card silently loses its scheduling. This add-on gives a shared deck a real one: point it at a GitHub repo or local folder once, and Update my decks handles the rest.

- **Syncs only what changed.** A version hash per deck means editing one deck doesn't re-import ten.
- **Keeps review history.** Cards match by GUID (with a rename map for reworded fronts), so intervals and ease factors survive every update.
- **Keeps your annotations.** Fields you mark as yours (`Notes` by default) are snapshotted before import and restored after.
- **Backs up first, always.** A timestamped `.apkg` of the deck is saved before any import, pruned to the last 10.
- **Stays current on its own.** Optional background auto-sync for decks, and self-update for the add-on.

**Maintaining a deck of your own?** The add-on ships with no deck content; it syncs whatever source you point it at. Host a small `manifest.json` next to your `.apkg` files, in a GitHub repo or a shared folder, and everyone studying from your deck gets the same one-click updates. See [Using this for your own decks](#using-this-for-your-own-decks).

See `CHANGELOG.md` for what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from the [latest release](https://github.com/LTimothy/internpearls-anki/releases/latest).
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help. Two primary actions sit at the top (Update my decks, Manage decks); occasional tools live under Advanced (including Sync decks and Reconcile my decks on their own, for anyone who wants just one half); Settings and About sit at the bottom.

No deck source yet? Open Manage decks > Configure source and pick "Try the example deck" — it points the add-on at a small public demo repo so you can watch a sync work end to end, then swap in your own source later.

## Menu reference

### Update my decks

The main button, and the only one most people ever need. It fetches `manifest.json` from your configured deck source and figures out everything pending in one pass: which decks changed, which retired cards are still lingering in your collection, and which cards a deck reorg needs to relocate — the same two kinds of housekeeping "Reconcile my decks" handles on its own (see the Advanced entry below). For any changed deck, it downloads and matches it against your collection before showing you anything, so the confirmation lists real per-deck counts ("12 kept · 3 new"), not just how big the deck is. A real progress bar with a working Cancel button covers this step (and the later apply step), since it's a live download per deck and a multi-deck check on a slow connection would otherwise look like a frozen add-on with no way out. One confirmation covers all of it — changed decks with their real counts, retired cards, and relocations, and it's explicitly a preview, nothing applies until you click Update — so you know the full scope before anything happens. Cancelling the apply step partway through is safe: whatever decks already finished stay applied, and archiving/relocating is skipped for that run rather than run against a partial sync.

On confirm, content updates apply first, then retired cards archive and reorganized cards relocate — in that order, so a retired card's replacement is already in your collection before the old card gets archived out, instead of you having to remember to run a sync first. Nothing already downloaded for the preview is fetched again. For each changed deck it:

1. Takes a fresh, timestamped backup of just the configured deck first (a self-contained `.apkg` with scheduling included, saved internally and pruned to the most recent 10). Nothing else runs until this succeeds, or you explicitly choose to continue without one.
2. Adds any missing fields to the note type (never removes or renames existing fields).
3. Snapshots your `protected_fields` on every card in scope.
4. Matches each incoming card to your existing card — by GUID first, then by front text, then via the `front_aliases` map in the manifest for a card whose front was reworded since your last sync — so review history carries over.
5. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
6. Restores the preserved fields from the snapshot.

If an update also changes how cards *look* (a card template or its CSS — the one thing these imports deliberately never touch, see "How history is preserved"), it says so afterward and offers to apply the new look. Saying yes updates the note type's templates and styling, which Anki treats as a schema change: your next AnkiWeb sync will be a one-time full sync ("Upload to AnkiWeb"). Saying no keeps your current card appearance; the content update has already imported either way, and the next template change will offer again.

If no deck source is configured, it tells you to open Manage decks and use Configure source. If nothing at all is pending, it just says you're up to date.

### Manage decks

A panel listing every deck the source offers, each with a checkbox, a status pill (New, Update available, or Up to date), and its card count. Unchecking a deck stops future syncs for it; cards already imported stay in your collection until you delete them yourself in Anki. The same panel edits `protected_fields`. Save keeps the choices for your next update; Save and update now also runs Update my decks right away. What's actually pending — real per-deck kept/new counts, retired cards, relocations — is Update my decks' own confirmation, not something this panel previews separately.

Deck-source configuration lives here too, behind a button next to the "Source" line at the top: "Configure source" if nothing is set up yet, "Change source" once something is. It opens the same dialog either way, with three buttons plus Cancel:

- GitHub repo: enter the repo (`owner/name`) and, only if the repo is private, a read-only personal access token — leave the token blank for a public repo. The token field is masked as you type, and the value is stored only in your local add-on config; it never leaves your machine except in requests to GitHub.
- Local folder: point it at a directory that contains `manifest.json` and the `.apkg` files.
- Try the example deck: points the add-on at [`LTimothy/internpearls-example-deck`](https://github.com/LTimothy/internpearls-example-deck), a small public demo repo, so you can watch a sync work before you have any deck source of your own. Choosing it also points `scope_tag` and `export_deck` at the example deck's values (only if you haven't customized them), so field preservation and the automatic backup work in the demo too; picking a GitHub repo or local folder later resets exactly those injected values.

Either way, as soon as you save, the add-on connects to the source. If the source's manifest recommends a scope tag and backup deck (see "Using this for your own decks"), you're offered them right then, so field protection and the automatic backup cover that source's decks without editing raw config; nothing is applied unless you say yes. Then Manage decks reopens against the source: how many decks it found, or (shown as an error in the Source line, with an empty deck list and the same button waiting) exactly what went wrong, a bad token, an unreachable repo, or a wrong folder, so you're never left staring at a dead end. If nothing is configured at all, Manage decks still opens; it just shows an empty list and the Configure source button, instead of a warning that sends you hunting for a different menu item.

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

Occasional tools, tucked away from the two primary actions at the top — including the two halves Update my decks normally runs together, for anyone who wants just one of them on its own:

**Sync decks** is the content-only half of Update my decks: it fetches `manifest.json`, compares each deck's version hash against what you last synced, and imports only the decks that changed, with the same confirmation, backup, GUID matching, and field preservation described above. It just doesn't also archive retired cards or relocate reorganized ones — use Update my decks for that in one pass, or Reconcile my decks below to run just that half.

**Import single deck (manual)** picks one `.apkg` outside your configured source, for a deck someone sent you directly or a build you're testing before pushing it live. It runs the same personalization, automatic backup, and note restore as a sync, just for the one file you choose.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Every sync runs this before every import.

**Reconcile my decks** is the archive/relocate half of Update my decks, runnable on its own. It does two kinds of housekeeping a plain content sync can't, both driven by ledgers the deck source ships in its manifest:

- *Retired cards.* When a deck splits one bulky card into several focused ones, or reword-replaces a card, the old version's identity changes — so a sync adds the new cards but never removes your copy of the old one, and it lingers as a duplicate in your reviews. Reconcile finds each retired card you still have, copies any personal text you've written in a protected field (like Notes) onto its replacement(s) — but only if the replacement's field is still blank, so it never overwrites something you've already written there — then moves the old card to an `…::Retired` subdeck, suspends it, and tags it `…::retired`. **It never deletes anything**: your review history is kept, and you can bring any card back by unsuspending it or moving it out of the Retired deck. Run on its own (not via Update my decks, which always syncs content first), it warns if some replacements aren't in your collection yet and suggests running Sync decks first.
- *Reorganized decks.* When the deck source moves a card into a different deck without changing its identity (a topic getting its own deck, say), a normal sync updates the card's content in place but never relocates it — only a brand-new card gets filed into the deck the source specifies. Reconcile finds any card still sitting exactly where the source last put it and moves it to match. If you've since filed that card into a deck of your own choosing, it's left alone — Reconcile only ever follows a card that's still where the source's last known location was, never overrides your own organization.

Both are schema-neutral (no forced full AnkiWeb sync) and trivially reversible by hand. A backup is taken automatically first, and re-running it is a no-op on anything already handled. If you've turned on background auto-sync (see Settings below), which only ever applies deck content on its own, a pending retired/relocated backlog shows up right on this menu item itself — "Reconcile my decks (3 pending)" — with a one-time tooltip when it first appears or grows, so a backlog auto-sync can't clear by itself never piles up unnoticed.

**Backup intern pearls deck** is the manual, on-demand version of the automatic pre-sync backup: a fresh `.apkg` of just the configured deck (`export_deck`), with scheduling included, saved internally and pruned to the most recent 10. Use it right before poking at cards yourself outside the add-on.

**Import intern pearls deck** brings a previous deck backup or export back in. The file picker defaults to the internal backups folder, but you can browse to any matching `.apkg`. Since the file's own GUIDs already came from a real collection, this is a plain import with scheduling restored, matching cards update in place and anything missing is added as new; no personalization step is needed the way Sync and Import single deck need it for a spec-authored deck from someone else's collection. Whatever deck this rolls back is re-offered on your next Update my decks (see "How history is preserved").

**Export intern pearls deck** writes a standalone `.apkg` of just the configured deck, with your review history, deck options, and media all included, the same result as Anki's own File > Export > Anki Deck Package with every checkbox checked. This is the same export the automatic backup and Backup intern pearls deck use, just prompting you for where to save it, meant to be kept or shared on its own rather than used purely to undo a sync.

**Backup full collection** takes a full, whole-collection backup on demand, the same kind that used to run automatically before every sync. Use this for broader protection than the deck-scoped default covers. Retention for these is whatever Anki's own preferences specify, not this add-on's 10-backup limit, which only applies to the deck-scoped backups above.

**Restore full collection** opens Anki's own backup picker (the same one under File > Switch Profile > Open Backup) pointed at your backups folder, so you can revert a full collection backup if something looks wrong. This replaces your entire collection, every deck, not just the ones this add-on manages, since that's what a real collection backup contains. Anki asks you to confirm the specific backup file before doing anything. Every deck this add-on manages is re-offered on your next Update my decks (see "How history is preserved").

**Check for add-on updates** compares your installed version against the public repo's `version.json`. If a newer version exists, it offers to download and install the `.ankiaddon`. You still need to restart Anki afterward. This is the on-demand version of what the Settings toggles below do on their own, which is why most people never need it: it's here as a fallback, not the primary way to stay current.

### Settings

Sync automation and add-on update behavior, kept separate from Manage decks since those answer a different question ("which decks, which fields, from where" versus "how automatic, how often"):

- **Sync decks automatically when updates are available**, off by default. When on, the add-on checks the source in the background on the interval below and applies any changed decks without asking, backing up first the same as a manual sync. The one thing it never applies unattended is a card-template change (that would force a one-time full AnkiWeb sync without anyone consenting to it): a deck update that includes one is held back and stays pending, and a tooltip points you at Sync decks to review it. It also never archives retired cards or relocates reorganized ones on its own — that stays a one-click confirm via Reconcile my decks, which the same check keeps nudged about (see the Advanced entry above) so a backlog can't pile up silently just because content sync is unattended.
- **Check every N minutes**, default 15, minimum 1. The check runs off the main thread when Anki supports it (essentially all current versions do), so it doesn't freeze Anki even at a short interval; if it can't reach the source, it fails within a few seconds and just tries again next time.
- **Notify me when a new add-on version is out**, on by default. A tooltip once per new release, no installation.
- **Install add-on updates automatically**, off by default. Downloads and installs a newer version as part of the same once-per-launch check, no confirmation. A restart is still needed to load it, same as installing by hand.
- **Let me flag problems with new cards as they sync**, off by default. Update my decks lets you preview each card a sync would add before it's imported. With this off, that preview is a quick, read-only list: no note boxes, nothing to send. Turn it on and a note box appears under each card, and closing the preview offers a copyable summary of whatever you flagged, whether or not you go ahead with the update.

### About

A short description of what the add-on does, a summary of your current settings (auto-sync, add-on updates, preserved fields), a reminder that no deck content ships with it, and a link to this repo.

## Updating decks

Run Intern Pearls > Update my decks, or turn on "Sync decks automatically when updates are available" in Settings so deck content applies on its own (retiring/relocating cards always stays a manual, one-click confirm — see Reconcile my decks above). Either way, only changed decks are imported, and the add-on backs up the deck automatically before touching anything, so there's no separate step to remember. For broader protection on top of that, Advanced > Backup full collection takes a whole-collection backup on demand.

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

As of v0.32.0, restoring a backup clears the relevant part of that record too, so a rollback is re-offered on your next check instead of the add-on reporting you're up to date over stale cards. Restore full collection clears it entirely, since every deck could have rolled back. Import intern pearls deck only clears the decks actually in the file you're importing (falling back to clearing all of them if the file can't be read), so restoring one deck's backup doesn't force a recheck of every other deck too. Either way, the next Update my decks re-offers whatever came back, and the re-import still matches by GUID, so review history carries over as always.

## Using this for your own decks

**The easy way: start from the example deck.** [LTimothy/internpearls-example-deck](https://github.com/LTimothy/internpearls-example-deck) is a template repository: click "Use this template" on GitHub, edit the JSON card specs right in your browser, and its bundled GitHub Action rebuilds the `.apkg` files and manifest whenever the cards change. No terminal, no installs. Its README walks through creating a deck, sharing it with a study group, and publishing updates, step by step.

The rest of this section documents the manifest format itself, for anyone building decks with their own tooling. Nothing about Sync decks, Manage decks, or the backup/export/import tools is specific to any particular deck's content. To point this add-on at your own decks, host a `manifest.json` in a GitHub repo (private or public) or a local folder, alongside the `.apkg` files it references:

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
  "scope_tag": "YourTag",
  "export_deck": "Your Deck",
  "front_aliases": {}
}
```

- `decks` lists every deck Sync should manage. `name` is the deck name as it should appear in Anki; `apkg` is the path to fetch, relative to the repo/folder root (a flat filename or nested in a subfolder like `decks/your-deck.apkg`, both work); `spec` is informational only (not read by the add-on); `version` is any string that changes when the deck changes (a hash, a date, a counter) and drives which decks Sync considers "changed"; `cards` is optional, shown as a count in the sync confirmation.
- `scope_tag` and `export_deck` (optional, v0.30.0+) are your recommended values for the add-on config keys of the same names: the root tag your cards carry, and the deck whose export the automatic backup should cover. When someone configures your source, the add-on offers to apply whichever differ from their current settings; without them, subscribers have to set both by hand for field protection and backups to cover your decks. Older add-on versions ignore these keys.
- `front_aliases` maps a card's current front-field text to its previous wording, for any card whose front changed since the last version someone might be syncing from. Omit entries for cards whose front never changed. See "How history is preserved" above for exactly how this is used and its limits.
- Each `.apkg`'s notes need a stable GUID scheme of your own choosing. Most Anki deck-building tools default to a content hash of the front, which changes whenever you reword it — that's why `front_aliases` exists. The better scheme is a GUID derived from an explicit per-card id that never changes: the add-on matches by GUID before any text comparison, so with stable GUIDs you can reword fronts freely and never touch `front_aliases` again. This add-on doesn't generate decks, only syncs pre-built ones; how you build stable GUIDs into your `.apkg` is up to your own tooling.
- Two optional (schema 2) ledgers back the Reconcile my decks action: `retired` — `{deck_name: {guid: {identity, reason, superseded_by}}}` — for cards you've deliberately split, merged, or removed; and `deck_moves` — `{guid: {from, to}}` (full Anki deck paths) — for notes you've relocated to a different deck without changing their identity. Both are additive and optional; an add-on version that predates one simply ignores it.

Use Configure source, inside Manage decks, to point at your repo (with a read-only token if private) or folder, and Sync decks, Manage decks, and the Advanced tools all work exactly as described above, just against your own content. If your manifest carries `scope_tag` and `export_deck`, configuring the source offers those values to each subscriber automatically; otherwise they need to set both in Config to match your deck's tag and name, if those differ from the `InternPearls` / `Intern Pearls::Intern Custom` defaults.

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
- `internpearls/sync.py` — the sync and reconcile flows: source resolution
  (`_fetch_manifest`), Sync decks, Reconcile my decks, the unified Update my decks
  front door, the shared `_run_sync`/`_reconcile_pending` sequences, Import single
  deck.
- `internpearls/updates.py` — add-on self-update: version fetch, package download,
  the manual check.
- `internpearls/background.py` — `_run_in_background` (QueryOp dispatch), the startup
  update check, the auto-sync poll and its timer.
- `internpearls/dialogs.py` — Manage decks, Settings, About, and source configuration.
- `internpearls/review.py`: the new-card review dialog and the feedback digest it
  produces. Kept out of `dialogs.py` because that module imports `sync.py`, and this is
  opened from `sync.py`'s update flow, which would make the import circular.

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

- `tests/test_release_integrity.py` checks the two release steps that are manual and
  therefore forgettable: that the committed `internpearls.ankiaddon` matches
  `internpearls/`, and that `version.json` and `ADDON_VERSION` agree. Both fail the
  same way, silently and permanently, so both are pinned rather than remembered.
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

### Seeing a dialog actually render

Every layer above uses mock Qt, which can prove a widget tree's *structure* but never
that Qt painted it. That gap is real: Qt drops a stylesheet declaration it doesn't
like without raising, so a rule can read correctly, pass review, pass its tests, and
still be invisible. Two shipped rules were (v0.32.1).

`tools/render_dialog.py` closes it by rendering a real dialog to a PNG, with real
PyQt6 and no Anki:

```bash
pip install PyQt6
python3 tools/render_dialog.py --list
python3 tools/render_dialog.py review --expand 1 --feedback --out review.png
python3 tools/render_dialog.py review --dark          # see "Colors" below
python3 tools/render_dialog.py review --apkg ~/deck.apkg
```

It reuses `tests/mock_anki.py` for the whole fake Anki world and swaps only that
harness's fake Qt for real PyQt6, so the two are opposites rather than duplicates:
mock Qt for structure in CI, real Qt for pixels locally. It's a developer tool and
isn't packaged into the `.ankiaddon`. Its default card content is synthetic; `--apkg`
reads a real deck through the add-on's own `apkg_note_details`.

Reach for it whenever a change involves a stylesheet, a border, spacing, or a color.

### Colors

Nothing here branches on Anki's Night Mode. Dialog colors are picked as saturated
mid-tones that read on both themes, which keeps one code path instead of two.

That convention has one rule that's easy to miss: **if you hardcode a background,
hardcode the foreground with it.** Text color otherwise comes from the platform
palette, which flips with the theme while your background doesn't, so a light block
ends up with white text on it in dark mode. A color-only style is safe; a
background-only style is not.

`--dark` approximates a dark theme via Qt's color-scheme hint. It is *not* Anki's
night theme, so treat it as a check on whether hardcoded colors survive a dark
background at all, not as proof night mode is right.

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon
```

### Versioning

The add-on uses three-part semver: `MAJOR.MINOR.PATCH`.

- PATCH (0.11.0 to 0.11.1): bug fix or internal cleanup, no UI changes.
- MINOR (0.11.0 to 0.12.0): new feature or menu item, backwards compatible.
- MAJOR (0.x to 1.0.0): breaking change that requires the user to reconfigure.

On each release, bump `ADDON_VERSION` in `internpearls/config.py` and `version` in `version.json`, add an entry to `CHANGELOG.md`, run `./build.sh`, and commit.

Then tag `vX.Y.Z` and push the tag. That publishes the GitHub release on its own
(`.github/workflows/release.yml`): it runs the tests, checks the tag against
`version.json`, cuts the notes from that version's `CHANGELOG.md` section, and attaches
the committed `internpearls.ankiaddon`. Any of those failing means no release is
created, so a tag pushed against a stale package or an undocumented version fails loudly
rather than shipping.

Run `pytest tests/ -q` before tagging and none of that should ever fire:
`tests/test_release_integrity.py` checks the same things locally, except the tag itself,
which only exists once you push it.

The release page is not how anyone gets the add-on. Self-update reads `version.json` and
`internpearls.ankiaddon` from `main` through the Contents API, so a release that is late,
or missing, changes nothing about what people receive. It is a shopfront for humans.
