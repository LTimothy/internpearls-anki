# Changelog

All notable changes to Intern Pearls Deck Tools. Versions follow the semver rules in
this repo's `README.md` ("Versioning").

## v0.26.1

- Fixed a real bug in the collection-revert reconciliation added in v0.25.2: it
  required an *exact* match between a manifest deck's name and an Anki deck you
  actually have, but a deck spec's `deck_name` is routinely just the parent path —
  cards land in `deck_name::<subdeck>` for any spec using subdecks, which is the
  normal case (the public example deck included). That meant every subdeck-based
  deck was silently treated as "not installed" on every single check, forever,
  forcing a pointless resync each time — caught via the live demo constantly
  offering an update with nothing actually changed. Now matches the manifest name
  itself or any subdeck beneath it.
- Update my decks' confirmation now downloads and matches each pending deck before
  showing it, the same way the old "Check what will sync" preview did — real
  "N kept · M new" counts per deck, not just how big the deck is. A progress window
  covers the check itself, since it's a live download per deck. Nothing already
  downloaded for this preview is fetched again during the actual update.

## v0.26.0

- Added **Update my decks**, a new top-level menu item and the recommended way to
  stay current from now on. It computes everything pending in one pass — deck
  content changes, retired cards still in your collection, and cards a reorg needs
  to relocate — and shows one confirmation covering all of it, instead of the old
  multi-step dance of syncing, then separately digging into Advanced to reconcile.
  Content updates apply first, then archiving/relocating, so a retired card's
  replacement is already there before the old card archives out. Sync decks and
  Reconcile my decks still exist under Advanced for running either half on its own.
- Manage decks no longer has its own "Check what will sync" preview button — that
  same preview is now Update my decks' own confirmation, so there was no reason to
  ask twice. "Save and sync now" is renamed "Save and update now" and routes through
  the new unified flow.
- Auto-sync (Settings) still only ever applies deck content on its own, never
  archives or relocates — but it now keeps the "Reconcile my decks" menu item
  labeled with a live pending count (e.g. "Reconcile my decks (3 pending)") and
  shows a one-time tooltip when a backlog first appears or grows, so retired or
  reorganized cards can no longer pile up silently between manual checks just
  because auto-sync is unattended.

## v0.25.2

- Fixed a gap in v0.25.1's collection-revert fix: it only detected a *total* wipe
  (every synced note gone at once), so a revert that only rolled back part of the
  collection — the common case, e.g. one deck's cards erased while others stayed
  intact — still left that one deck wrongly reporting "up to date". The check is now
  per deck: a deck counts as synced only if the collection currently has a note under
  it actually sitting in an Anki deck of that name, so a partial revert is caught and
  recovered the same way a full one is.

## v0.25.1

- Fixed a real bug: restoring an Anki collection backup ("collection revert") after a
  sync could leave Sync decks, Check what will sync, and the Manage decks status pills
  all reporting "up to date" even though the revert had erased the synced cards. The
  add-on's own sync bookkeeping (`installed.json`) lives outside the collection file,
  so it never rolled back along with it — nothing was actually being compared against
  the collection's real contents. All three now reconcile that bookkeeping against the
  collection first, so a deck the collection has lost is treated as not-yet-synced
  again and a normal sync recovers it. Same fix applies to the unattended auto-sync
  poll.

## v0.25.0

- Fixed a real bug: Reconcile my decks' confirmation could become unusable after a
  large backlog (e.g. dozens of cards relocated by one reorg) — it used a plain
  message box with no scroll area, so a long enough list pushed the Yes/No buttons
  off-screen with no way to reach them. The confirmation now scrolls in a fixed-height
  viewport with the buttons pinned outside it, so they're always reachable regardless
  of content length, and the card list itself is capped to the first 15 plus a "...and
  N more" summary so it also reads as a short list rather than a wall of text. A large
  first run also now says up front that it's a one-time catch-up, since the length
  alone can otherwise read as something having gone wrong.
- Tightened the archive/relocate confirmation's copy: one shared "nothing is deleted,
  here's how to undo it" note instead of repeating the same reassurance once per
  section, and action-specific buttons ("Archive", "Relocate", "Archive and relocate")
  instead of a generic Yes/No.

## v0.24.0

- "Check what will sync" (Manage decks) now also reports what Reconcile my decks has
  pending — retired cards still in your collection and cards a deck reorg needs to
  relocate — not just the per-deck kept/new breakdown. Read-only, same as the rest of
  the preview; nothing is archived or moved until you actually run Reconcile.

## v0.23.0

- Sync now refuses to run against a deck source whose `manifest.json` `schema` is
  newer than this add-on version understands, with a clear message to update first,
  instead of attempting an import against a manifest shape it can't fully interpret.
  Auto-sync applies the same check and pauses quietly (one tooltip per session) rather
  than looping every poll interval. This is a forward-looking safety net — today's
  manifest schema (2) is unchanged and every existing source keeps syncing normally.
- Add-on updates are no longer only visible in the 8-second startup tooltip: the
  "Check for add-on updates" menu item now shows the known-available version right on
  the label (persists across the tooltip fading and across restarts, seeded from the
  last check), and About shows the same "latest known" line next to the installed
  version.

## v0.22.0

- "Reconcile my decks" now also relocates cards a deck reorg has moved to a
  different deck without changing their identity (e.g. a topic getting split
  into its own deck) — a normal sync updates such a card's content in place but
  never its deck, since only a brand-new card gets filed into the source's
  declared deck. Reconcile reads a new `deck_moves` ledger the source ships in
  its manifest and moves any card still sitting exactly where the source last
  filed it; a card you've since filed somewhere of your own choosing is left
  alone. Schema-neutral and trivially reversible, same as the retired-card
  archiving this action already did.
- Reconcile also now carries a personal note (or any other protected field) from
  a retired card onto its replacement(s) before archiving it, as long as the
  replacement's field is still blank — so annotating a card doesn't get stranded
  the moment it's superseded by a split or reword.

## v0.21.0

- New Advanced action, "Reconcile my decks": finds retired cards still in your
  collection — older versions of cards a deck has since split into focused ones or
  reworded — and archives them so they stop showing up as duplicates in your reviews.
  Each is moved to an `…::Retired` subdeck, suspended, and tagged; **nothing is
  deleted**, review history is kept, and anything can be brought back by unsuspending
  it or moving it out of the Retired deck. A backup is taken automatically first.
  Reading a new `retired` ledger the deck source ships in its manifest; older add-on
  versions ignore it.

## v0.20.1

- "Try the example deck" now scopes its automatic backups to the parent
  `Example Decks` deck instead of a single subdeck, so all of the example repo's
  decks (it now ships more than one) are covered by the pre-sync backup.
- Live demo: the default source is the example GitHub repo (exactly what "Try the
  example deck" configures in real Anki) instead of a local folder; the demo serves
  that repo's files from its in-page copy so the maintainer buttons still take
  effect instantly.

## v0.20.0

- Cards now match by GUID first, before front text and `front_aliases`. Deck sources
  that keep GUIDs stable (an explicit per-card `id` in the spec) can reword a card's
  front any number of times without an alias entry, and the learner's review history
  still carries over — the single-hop limit of `front_aliases` no longer applies to
  those cards. Front-text and alias matching remain as fallbacks for collections whose
  GUIDs predate stable ids.
- Sync now detects when an updated deck changes a card template or its CSS (the one
  thing `merge_notetypes=False` imports deliberately never propagate) and offers to
  apply the new look, explaining that doing so makes the next AnkiWeb sync a one-time
  full sync. Declining keeps the current appearance; content and history import
  either way. Import single deck gets the same offer.
- The unattended auto-sync poll never applies a template change (no one is there to
  consent to a full sync): a deck update that includes one is held back, stays
  pending, and a tooltip points at Sync decks to review it — mentioned once per
  session, not on every poll.

## v0.19.0

- Sync decks now shows Anki's progress window while each deck downloads and imports
  ("Syncing <deck> (2 of 5)"), instead of appearing frozen on a slow connection. The
  unattended auto-sync poll is unchanged; it already ran its downloads off the main
  thread and reports through tooltips.
- The GitHub source setup is one dialog with both fields (repo, optional masked
  token) instead of two prompts in a row, so cancelling the token question no longer
  throws away the repo you just typed.
- The blocking waits that remain on the main thread (opening Manage decks, testing a
  just-saved source, "Check what will sync") now show the busy cursor while they run.

## v0.18.2

- Internal restructure, no behavior change: the single 1,600-line `__init__.py` is now
  nine modules split by concern (`config`, `ui`, `net`, `collection`, `sync`,
  `updates`, `background`, `dialogs`, with `__init__.py` reduced to menu and startup
  wiring). `ADDON_VERSION` moved to `internpearls/config.py`. See "Code layout" in the
  README.
- Dialog headings, hints, and link-style buttons now share styling helpers in `ui.py`
  instead of per-dialog stylesheet strings; the three link-style buttons in Manage
  decks now render at one consistent size.
- `build.sh` packages every `internpearls/*.py` file (the previous hardcoded two-file
  list would have shipped a broken add-on after this split) and removes the old
  archive before zipping, so a deleted module can't linger inside the package.

## v0.18.1

- Auto-sync no longer downloads decks on the main thread. Only the manifest check
  moved off-thread in earlier work; the per-deck `.apkg` download (the part that can
  actually take a while on a big deck or a slow link) still ran inside the completion
  callback, so it could still freeze Anki mid-review, which is exactly what background
  sync is supposed to avoid. Downloads now happen alongside the manifest fetch in the
  background step; a per-deck download failure is still reported per-deck (not a fetch
  that takes down the whole sync), same as before.

## v0.18.0

- Fixed public GitHub repos as a deck source: the token is now genuinely optional.
  Previously a GitHub source was only used when a token was set, so following the
  documented "leave the token blank for a public repo" advice silently fell through to
  "no source configured".
- Added "Try the example deck" to the Configure source dialog: one click points the
  add-on at the public `internpearls-example-deck` demo repo, so someone with no deck
  source of their own can watch a sync work end to end. It also points `scope_tag` and
  `export_deck` at the example deck's values (only when they're still at their
  defaults), so field preservation and the pre-sync backup work in the demo too;
  configuring a GitHub repo or local folder later resets exactly those injected values.
- The Sync completion dialog no longer claims a pre-sync backup was saved when none was
  taken (first sync, or the backup failed and you chose to continue); it now says so.
- "Check what will sync" can be run again after it completes (the button re-enables as
  "Check again"), instead of sticking disabled at "Preview updated".
- The background auto-sync poll's manifest fetch now actually uses the tight unattended
  timeout the docs already claimed for it, rather than the interactive 6-second one.
- Docs: token-optional-for-public-repos everywhere the token is mentioned; corrected
  `decks_dir` precedence (GitHub wins when both are somehow set); noted that deck
  backups live in `user_files/` and are removed by an add-on uninstall.

## v0.17.0

- Cleaned up the menu bar. Top level is now just Sync decks and Manage decks; everything
  occasional, including the manual "Check for add-on updates" (most people never need it
  since the background notice already covers that job), moved under Advanced; Settings
  and About now sit together at the bottom, in that order.
- Configure deck source is no longer its own menu item. It lives inside Manage decks now,
  behind a "Configure source" (nothing set up yet) or "Change source" (something is)
  button next to the Source line, since it only ever mattered in the context of what
  decks are available to manage.
- Manage decks no longer dead-ends when no source is configured or the configured one is
  unreachable. It still opens, with an empty deck list, the reason shown right in the
  Source line, and the same button waiting, instead of a warning that sends you off to a
  different menu item that no longer exists.
- Tests: full coverage of the new bootstrap paths (nothing configured, source unreachable,
  source working, and the change-source-then-reopen flow) plus an exact assertion on the
  new menu structure, exercised against a mocked Anki environment.

## v0.16.0

- Fixed the root cause of "Check for add-on updates" sometimes not seeing a version
  that had already shipped: the add-on's own version check fetched `version.json` from
  `raw.githubusercontent.com`, a CDN endpoint that can lag well behind a push. Confirmed
  directly: right after a push, the GitHub contents API reflected the new file
  immediately while the raw CDN link for the same file and branch still served the old
  one more than two minutes later. Both the version check and the package download now
  go through the contents API instead, the same way deck content already did.
- Added a Settings dialog (moved out of Manage decks, since "which decks" and "how
  automatic" are different kinds of choices): sync automation and add-on update
  behavior in one place, with an interval field instead of a fixed hourly check.
- Added "Install add-on updates automatically," off by default, alongside the existing
  notify-only option. When on, a newer version downloads and installs itself as part of
  the once-per-launch check; a restart is still needed to load it either way.
- Lowered the auto-sync poll's default to 15 minutes and its floor to 1 minute (previously
  60 and 15), so decks that need to land within the hour actually can. To make a 1-minute
  floor safe, both background checks (add-on updates and the deck poll) now run their
  network fetch through Anki's QueryOp, off the main thread, so neither one can freeze
  Anki no matter how often it fires. Only backing up and importing, which happens on the
  main thread just like a manual sync, and only once something actually changed, is
  unaffected; the check that runs constantly is what needed to stop blocking. Falls back
  to a short, bounded synchronous call on an Anki build old enough to lack QueryOp.
- Generalized "Notes restored on N card(s)" to "Preserved fields restored on N card(s)"
  everywhere it appears, since the add-on has supported any configured field, or
  several, for a while now, not just Notes.
- Expanded About: it now shows your current auto-sync, add-on update, and preserved-
  field settings, not just a static description.
- Test suite grown to 51: full coverage of the new pure decision logic (interval
  clamping, which action a version check should take given the notify/auto-update
  toggles) plus the auto-sync fetch/apply split and the QueryOp/fallback dispatch,
  exercised with a mocked Anki environment.

## v0.15.0

- Startup update notice: once per launch, a silent check compares your version against
  the public repo's and shows a brief tooltip if a newer one exists — at most once per
  new release, not every launch. Never auto-installs; "Check for add-on updates" is
  still the explicit action that does that. Fixes the confusing case where pushing a
  fix to GitHub doesn't change what's running until you notice and update yourself.
  Toggle with the new `notify_addon_updates` config key (default on).
- Auto-sync decks: a new checkbox in Manage decks ("Automatically sync when updates are
  available", off by default). When on, decks sync in the background — once shortly
  after startup, then on a repeating poll (default every 60 minutes, floored at 15) —
  without asking each time. A backup is still taken first, same guarantee as a manual
  sync; if the backup fails, that round is skipped rather than importing unprotected.
  Results show as a transient tooltip, never a blocking dialog, since this can fire
  mid-review. Toggling the checkbox takes effect immediately, no restart needed.
- The interactive Sync decks and the new background auto-sync now share one
  implementation of the actual import sequence (`_run_sync`) instead of two, so there's
  exactly one place the history-preserving logic lives.
- GitHub load at the default cadence is trivial: one small `manifest.json` fetch per
  poll, well under the unauthenticated 60-req/hour limit even at the 15-minute floor.

## v0.14.1

- When every deck in Manage decks is already up to date, the "Check what will sync"
  button now shows "All decks up to date" and is disabled, instead of looking like
  there's something to check and then reporting nothing on click.

## v0.14.0

- Manage decks can now preview changes in place: a "Check what will sync" button
  downloads the changed decks and fills in each row with how many cards would update in
  place (history kept) vs. be added as new — read-only, nothing is imported. It runs on
  click, not on open, so the panel still opens instantly.
- Retired the separate Preview sync menu item — it's now fully covered by the button
  above, so there's one place to see what a sync will do instead of two. (Sync decks
  still shows its own confirmation and backs up before importing.)

## v0.13.1

- Clearer Manage decks flow. After Save (without syncing), the confirmation now says
  plainly that nothing has synced yet and to run Sync decks when ready, instead of the
  misleading "N decks will sync". Added an inline hint next to the buttons explaining
  that Save keeps the choices for the next sync while Save and sync now also pulls right
  away.
- Decluttered the top menu: Preview sync moved under Advanced (Sync decks already shows
  a per-deck confirmation and takes a backup, so a separate dry run is a power-user
  tool, not a primary action). Top level is now Sync decks, Manage decks, Configure
  deck source, Check for add-on updates.
- Field parsing for the preserved-fields box moved into a tested pure helper
  (`parse_fields`) that also de-dupes.

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
