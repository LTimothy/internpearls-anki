# Changelog

All notable changes to Intern Pearls Deck Tools. Versions follow the semver rules in
this repo's `README.md` ("Versioning"). Restart Anki after installing any update.

## v0.83.0

Faster imports for decks with pictures, and a choice when a deck can't be previewed.

- When Update my decks can't preview a deck, it asks whether to retry or skip it. A
  skipped deck waits for your next update.
- The end-of-update feedback window says the saved copy is under Advanced > Recent card
  feedback.
- A deck package with file names that point outside its own folder is refused.

## v0.82.0

Undo covers a deck restore, long lists act faster, and buttons show keyboard focus.

- Edit > Undo after Restore intern pearls deck also undoes the add-on's own records for
  that restore.
- Suspend, Ignore and Offer again are instant even far down a long list, and long lists
  keep loading when you use the keyboard or scroll bar.
- A failed restore names the real problem with the file.

## v0.81.0

Lower memory use, safer settings, and AI drafts that survive closing the wizard.

- A long Update my decks list builds rows only a little ahead of where you are, so a
  first sync of thousands of cards uses far less memory.
- Checking the deck source or for add-on updates no longer freezes Anki.
- Manage decks refuses a preserved field name that no note type has and suggests the
  closest one.
- An unfinished Generate cards (AI) draft is kept until you import or discard it.
  Attachments are not kept.
- Declined cards is now Later and declined cards, with search and filters.

## v0.80.0

A shorter card feedback digest that survives being texted, and a saved copy of each one.

- Standing declines take one line per deck, and the digest uses plain ASCII where it
  can, so a text message doesn't turn it into question marks.
- The last 20 digests are kept under Advanced > Recent card feedback, and a digest can
  be saved as a file.
- Notes typed on Update my decks are saved if Anki closes while the screen is open.

## v0.79.0

Card pictures through a proxy, AI verdicts that survive a rescan, and text shown as
written.

- A web card picture downloads through your system's https proxy when one is set.
- Scan for duplicates keeps its AI verdicts when you rescan, as long as neither card
  has changed.
- Deck names, file names and error messages show exactly as written, so a "<" or "&"
  no longer breaks a line.
- A config value of the wrong type falls back to its default.

## v0.78.0

Safer deck sources and AI output, a better Scan for duplicates, and fixes to restore
and auto-sync.

### Deck sources

- A deck package over 512 MB, with more than 50,000 files, or that would unpack to more
  than 2 GB (1 GB for one file) is refused with a reason. Restore intern pearls deck
  checks your file the same way.
- Deck and skill paths in a manifest must stay inside the deck source.
- Your GitHub token is never sent on a redirect to another host, and every download has
  an overall time limit.

### Generate cards (AI)

- The deck skill question shows the skill as written and points out hidden characters,
  which are removed.
- Only ordinary card formatting is kept from the assistant. Scripts, frames and
  non-https links are removed before review.
- Web pictures download over https only, never from a private address, and only if they
  are real images.
- Each draft is checked against your collection, and likely duplicates start on Skip.

### Other

- Scan for duplicates counts short tokens that carry meaning (T3, Type II, alpha-2), and
  long lists load as you scroll.
- Restore intern pearls deck backs up every deck it changes.
- Auto-sync keeps its state per profile.

## v0.77.1

Card text shows apostrophes, ampersands and quotes as themselves.

## v0.77.0

Later replaces Skip.

- New cards offer Import / Later / Never; changed cards offer Apply / Keep yours /
  Later / Never. A Later card comes back on your next Update my decks, whether or not
  its deck changed.
- Cards you had skipped become Later cards. The first update after upgrading shows them
  set to Later so none are imported by accident.
- The feedback digest lists only Never and Kept yours cards, plus a count left for later.

## v0.76.3

Update my decks says how many unopened cards were already decided earlier, and holds a
card whose earlier choice no longer fits.

## v0.76.2

Fixes for cases where an update, restore or undo could lose something of yours.

- A cloze card that would lose cards by changing format is no longer converted. The new
  version is added beside it.
- Updates keep your own tags, such as leech and marked.
- Your notes on cards are restored as each deck finishes, so a stopped update can't
  leave them blank.
- Edit > Undo after an update undoes the whole deck, and that deck is offered again.
- Restoring your oldest deck backup no longer deletes it first.

## v0.76.1

Update my decks stays quick with a very long list, including when switching filters.

## v0.76.0

Fixes and new tools in Update my decks and Generate cards (AI).

- Updates with 20 or more cards get a filter bar and a search box.
- Declined and held cards, consented skills and unsent notes are kept per profile.
  Existing decisions move to the first profile opened after updating.
- In Generate cards (AI), cards with an empty or duplicate front are flagged, a failed
  picture has Retry, Remove and Replace, and an interrupted import adds only the rest
  when run again.

## v0.75.0

Groups of changed cards are harder to miss.

- Cards changed because of reviewer feedback are never folded away.
- A folded group sits in its own box with a Show N cards button.
- One control decides a whole group: Apply all or Keep all yours, Import all or Skip
  all.

## v0.74.1

Update my decks opens faster when a large group of cards is folded.

## v0.74.0

The update confirmation has an "Update reviewed, hold N for later" button. It applies
the cards you opened or decided on and holds the rest until your next Update my decks.

## v0.73.1

Clearer network errors and a safer self-update.

- A connection that drops mid-download reads as a network problem, not a token problem.
- Self-update checks that it downloaded the version it announced. If not, nothing is
  installed and you are asked to try again later.

## v0.73.0

Generated cards come with real pictures much more often. The assistant searches for a
real image when a card is about something visual, in Quick mode as well as Thorough, and
Codex CLI uses its own web search when available.

## v0.72.4

Emptied protected fields stay empty, reworded cards keep their FSRS state, and
auto-sync applies nothing if you switch profile mid-download.

## v0.72.3

Update my decks lists a protected field change only when the update will apply it, and
no longer shows some cards twice while loading.

## v0.72.2

Closing a window during background work frees that work, and a missing card image reads
"no image at that address".

## v0.72.1

Closing Update my decks while its list is loading no longer raises an error.

## v0.72.0

Per-card protected fields and folded change groups.

- A deck source can protect one field on one card. The manifest carries these as
  `note_protected_fields`.
- Five or more cards that share one change note are folded behind a Show N cards
  button. They still apply when you press Update.

## v0.71.0

Keep yours remembers the exact revision you declined. A kept card comes back only when
its own content changes.

## v0.70.0

Every card feedback digest ends with your current standing declines, grouped by
decision.

## v0.69.0

Earlier feedback stays visible, dated, when a card is revised again.

## v0.68.0

Cards with source labels sort in natural order within each deck, so Q2 comes before Q10.

## v0.67.1

Deck updates no longer fail when older note types have different IDs or lack newer
fields. Fields are matched by name, and missing ones are added during a manual update.

After updating, run Update my decks again. A one-time field addition may need a full
AnkiWeb sync.

## v0.67.0

Fixes across deck updates, Scan for duplicates and Generate cards (AI).

- The preview reads the same data Anki imports, so it matches the result.
- An import that would overwrite a matching card outside your deck scope is refused.
- A rejected or partial import stays pending instead of being reported as done.
- A failed import rolls back note type conversions and keeps the original card.
- Update state and backups are kept separately per collection and source.
- Generate cards (AI) can work from attachments alone, with limits on PDF size and
  pages.
- Dialogs stay usable on short screens and have screen reader labels.

## v0.66.2

Fixes to Scan for duplicates and Generate cards (AI). A deck name with `_` or `*` is
matched literally, cancelling while images resolve returns you where you were, and an
image outside the scratch folder is never read.

## v0.66.1

Update my decks scrolls smoothly while it builds rows in the background.

## v0.66.0

Scan for duplicates reads the collection in one query and no longer freezes Anki. A new
scope compares the add-on's cards with only the other cards in its deck tree.

## v0.65.1

Scan for duplicates needs at least two shared informative words for a likely
duplicate, applies Exclude decks to both sides, and warns when either side has fewer
than 50 cards.

## v0.65.0

A note that explains several cards shows once above them, and a retired card can show a
short reason from the deck source.

## v0.64.0

Scan for duplicates shows a band (Likely duplicate, Similar, Weak match) and the words
behind each match, and a Sensitivity control (Strict, Normal, Loose) sets how close a
match must be.

## v0.63.1

Rescan and Judge with AI work again in Scan for duplicates, and menu labels are
sentence case throughout.

## v0.63.0

New Experimental tool: Scan for duplicates. It compares the add-on's cards with the
rest of your collection, or any two decks, by the words they use. Judge with AI sends
only the pairs' front and back text to your assistant.

## v0.62.0

The AI wizard uses real web images for real things and draws an SVG only for a simple
schematic. Edit opens one dialog for the whole card.

## v0.61.0

New Check facts button in the AI wizard. The assistant gives each card a verdict
(confirmed, corrected or unverified) with a reason and source. A correction changes
nothing until you accept it.

## v0.60.0

Sandboxed tools in Thorough mode, and per-assistant defaults.

- Thorough mode gives each assistant tools confined to the scratch folder. Claude Code
  gets file tools and web search with no shell, Codex CLI runs in its own sandbox, and
  Antigravity CLI stays read-only.
- Each assistant has its own default model and effort.

## v0.59.1

An Antigravity run that returns an empty reply is retried once and then explained in
one plain sentence.

## v0.59.0

AI runs fail after two minutes of silence (three in Thorough) instead of on a fixed
timer, and the progress page shows what the assistant is doing without showing prompts
or reply text.

## v0.58.2

Each AI run writes a log to `user_files/ai_last_run.log`, without the prompt, replaced by
the next run and capped at 2 MB.

## v0.58.1

Layout fixes in AI Backends and the AI wizard. Your My rules win on style and wording,
but never on output format, and the assistant is told so.

## v0.58.0

The AI wizard picks its own card count and depth.

- The count is one card per point the source teaches, up to 40. An exact count is in
  the new Advanced panel.
- Depth starts on Thorough for sources of 1,500 characters or more, or with any
  attachment. `ai_default_count` and `ai_default_depth` set the starting values.

## v0.57.1

Check for add-on updates says when GitHub's hourly request limit has run out, and uses
your GitHub token, if you set one, to raise that limit.

## v0.57.0

Antigravity CLI works properly and gets Model and Effort controls, attached images work
with every assistant, and leftover scratch folders older than a day are removed.

## v0.56.1

AI Backends opens from the Generate cards wizard instead of the Experimental menu, and
its rows no longer clip their last line.

## v0.56.0

New AI Backends window, wider Night mode dimming, and My rules.

- AI Backends shows one row per assistant, plus path, model, effort and Test connection
  for the preferred one. `ai_cli_path` is now per assistant; an old value carries over.
- Night mode dimming can dim everything Anki draws as a web page, images included.
- My rules holds your own instructions for the assistant, up to 20,000 characters.

## v0.55.1

The Model field is a list of known aliases plus Custom.

## v0.55.0

The AI wizard can pick a model and, for Claude Code, an effort level. Claude Code
defaults to sonnet at medium effort. Night mode dimming shows a side-by-side preview.

## v0.54.2

The AI wizard's review rows use the same layout as Update my decks, and a flagged card
shows its reason.

## v0.54.1

Layout fixes in the AI wizard. Buttons stay reachable however many cards were drafted.

## v0.54.0

New Experimental menu holding Generate cards (AI) and Night mode dimming, which now has
a percentage control. The default, 30, matches the old fixed amount.

## v0.53.5

Fixed a crash risk in the AI wizard's undo shortcut helper.

## v0.53.4

After an AI import, Edit > Undo is available at once, the deck list updates, and the
message shows the right Undo shortcut on macOS.

## v0.53.3

An error when generation finishes returns you to the input page instead of leaving the
wizard stuck.

## v0.53.2

The AI wizard offers only note types already in your collection, and its errors show in
the add-on's own dialog.

## v0.53.1

Attaching a PDF says when its images can't be extracted in Anki, so you can attach them
as image files instead.

## v0.53.0

New Generate cards with AI. It drafts cards from text you paste or files you attach,
using Claude Code, Codex CLI or Antigravity CLI already installed and signed in on your
computer. The add-on has no API key field and never handles credentials.

- Pictures come only from a real web source, a file you attached, or an SVG the model
  draws.
- Generated cards go into their own `Generated` subdeck with new GUIDs, so deck syncs
  never touch them.
- Nothing from a session is kept after the dialog closes except your assistant choice,
  consented deck skills and usage logs.

## v0.52.1

The rewritten-field line on a changed card no longer wastes vertical space.

## v0.52.0

A rewritten field shows as one line, such as "Why rewritten, shortened (104 > 66 words)",
with a Show yours link to see your old version.

## v0.51.0

A heavily rewritten field shows the old text plainly instead of a word diff, and a
card's source label moves into the chip column.

## v0.50.0

An opened changed card ends with a "What changed" group. A reworded field shows as one
line with removed words struck and added words highlighted. Moved cloze blanks are named.

## v0.49.1

The update screen opens larger and its reassurance text is shorter.

## v0.49.0

A changed card offers Never alongside Apply and Keep yours, Never opens a note box, and
a deck source can ship a short label saying where a card came from.

## v0.48.0

A deck source can attach a short note to a new or changed card explaining why it
changed. Reviewer feedback shows quoted.

## v0.47.3

Fixes to declining cards.

- A declined card's note type is no longer changed.
- A damaged declined-cards file can no longer lose your annotations.
- The feedback box no longer says notes are sent anywhere. The digest is on your
  clipboard for you to send.

## v0.47.2

Layout fixes for the decline controls on macOS.

## v0.47.1

Declined cards lists every entry, even one from a damaged file, and per-deck counts
leave out cards you said Never to.

## v0.47.0

Per-card decisions on Update my decks replace the "flag problems" setting.

- New cards offer Import / Skip for now / Never; changed cards offer Apply / Keep mine
  for now. Any row can take a note.
- Declined cards are removed from the package before import, including during
  auto-sync. Nothing in your collection is deleted.
- Manage decks has a Declined cards list with Offer again.

## v0.46.2

Safer consent dialogs and backups.

- Escape and the close button pick the safe answer, and Enter can't agree to a full
  AnkiWeb sync.
- Backups of two decks with similar names no longer overwrite each other.
- Remove empty cards and Clean up duplicates back up the decks they touch.

## v0.46.1

Follow-up fixes to v0.46.0.

- A deck whose preview failed retries its download when you apply the update.
- Deck opt-outs survive an unreachable source and a source switch.
- The pre-update backup covers every deck the run touches and says which it backed up.

## v0.46.0

Safety and dialog fixes.

- A `.apkg` in Anki's newer export format is read correctly where possible. Otherwise
  the add-on stops before changing anything and asks you to re-export it with "Support
  older Anki versions" ticked.
- Protected fields are restored straight after each import.
- Auto-sync and a manual sync can't run at the same time.
- Cancel works during a download.

## v0.45.2

Formulas written in MathJax show as readable text in the review dialogs.

## v0.45.1

Fixed Update my decks ending with a "QTimer has been deleted" error.

## v0.45.0

Every list uses the same aligned rows, with chips in their own column, and long lists
show everything. Choosing a deck source is its own screen.

## v0.44.0

New, changed, retired and relocated cards appear as rows in one list on the update
screen, which loads as you scroll.

## v0.43.0

Every colour has a light and a dark version that meets WCAG AA contrast. "Import intern
pearls deck" is renamed "Restore intern pearls deck".

## v0.42.0

The review shows pictures, covers changed cards as well as new ones, and marks each row
NEW or UPDATED.

## v0.41.1

The flagged-card summary is readable in Night Mode.

## v0.41.0

Notes on new cards are saved as you type, and a busy update shows half as many dialogs.

## v0.40.0

Cloze blanks in the review show their group number (c1, c2) when a field has more than
one group.

## v0.39.0

New Advanced item: Remove empty cards. It runs Anki's Empty Cards report limited to your
scope tag and never leaves a note with zero cards. It is the one action that deletes
rather than archives.

## v0.38.3

The new-card review keeps tables, lists, bold and line breaks.

## v0.38.2

Cards in decks with no update are no longer reported as conflicting with your notes.

## v0.38.1

Fixed updates failing with a `ChangeNotetypeRequest` error on decks that reformat a
card. Run Update my decks again to apply the decks that failed.

## v0.38.0

When a card splits into several cloze blanks, FSRS memory state carries over with the
interval.

## v0.37.0

When a card is reformatted into several cloze blanks, every blank keeps your progress,
at half the original interval. A card with its own reviews is never overwritten.

## v0.36.0

Format conversion finds cards on renamed copies of a note type (such as "Basic+"), and
the newer manifest format is supported.

## v0.35.0

A card that changes from Q&A to cloze can keep its review history: sync offers to move
your cards to the new note type first, which needs a one-time full AnkiWeb sync.
Auto-sync never does this on its own.

## v0.34.0

A preserved field takes the deck's corrections until you edit it. When your edit and an
update hit the same field, yours is kept and the summary names the card.

## v0.33.0

Update my decks repairs a card you hold twice after a reword: it moves your progress and
notes onto the current wording and archives the old copy. Nothing is deleted.

## v0.32.3

Reconcile finds retired cards by front text when your copy has an older GUID.

## v0.32.2

The dosing block on a new card is readable in Night Mode.

## v0.32.1

The new-card review list is tighter and its rules draw correctly.

## v0.32.0

Restoring a backup clears the add-on's record of installed decks, so the next update
re-offers whatever rolled back. The new-card review is a list of collapsible rows.

## v0.31.0

Update my decks can show every new card in full before it is added. You can flag a card
with a note; the notes become a plain-text summary on your clipboard to send to the
deck maintainer.

## v0.30.0

Configuring a deck source offers its recommended `scope_tag` and `export_deck`.

## v0.29.2

The Clean up duplicate cards confirmation is easier to read.

## v0.29.1

A reorganised deck that kept being offered as "needs update" now relocates its cards by
front text and stops being offered.

## v0.29.0

New Advanced item: Clean up duplicate cards. It archives the copy with fewer reviews,
carries its notes to the kept copy, and backs up first. Nothing is deleted.

## v0.28.0

New setting to dim bright images in Night Mode, off by default.

## v0.27.1

Update my decks reuses downloads within a session, and the first network timeout is 10
seconds.

## v0.27.0

Update my decks and Sync decks show a progress bar with Cancel. Cancelling happens
between decks, never mid-import.

## v0.26.1

Decks with subdecks are no longer treated as uninstalled on every check, and the update
confirmation shows real per-deck counts.

## v0.26.0

New top-level Update my decks. It finds everything pending (content changes, retired
cards, cards to relocate) and shows one confirmation. Auto-sync still applies content
only.

## v0.25.2

A partial collection revert is caught per deck.

## v0.25.1

After restoring a collection backup, decks the backup lost are synced again instead of
reported as up to date.

## v0.25.0

The Reconcile my decks confirmation scrolls, so its buttons stay reachable with a long
list.

## v0.24.0

Check what will sync also reports what Reconcile my decks has pending.

## v0.23.0

Sync refuses a manifest schema newer than the add-on understands and asks you to update
the add-on. The menu and About show the latest known add-on version.

## v0.22.0

Reconcile my decks relocates cards a deck reorganisation moved, unless you've filed them
somewhere else.

## v0.21.0

New Advanced item: Reconcile my decks. It archives retired cards (moves them to a
Retired subdeck, suspends and tags them) after a backup. Nothing is deleted.

## v0.20.1

Try the example deck backs up all of the example decks.

## v0.20.0

Cards match by GUID first, then front text, then `front_aliases`. Sync offers to apply a
changed card template, warning that it needs a one-time full AnkiWeb sync.

## v0.19.0

Sync decks shows progress for each deck, and GitHub source setup is one dialog.

## v0.18.2

Internal restructure. No change in behaviour.

## v0.18.1

Auto-sync downloads decks in the background instead of on the main thread.

## v0.18.0

A GitHub token is optional for a public repo, and a new Try the example deck button
sets up a demo source.

## v0.17.0

The top-level menu is Sync decks and Manage decks; the rest moved under Advanced.
Configure source lives inside Manage decks.

## v0.16.0

Faster update detection, a Settings dialog, and an option to install add-on updates
automatically. Auto-sync defaults to every 15 minutes and checks off the main thread.

## v0.15.0

A startup notice shows when a newer add-on version exists, and a new auto-sync option
(off by default) backs up first and skips the round if the backup fails.

## v0.14.1

Check what will sync is disabled when every deck is up to date.

## v0.14.0

Manage decks has a Check what will sync button, replacing the Preview sync menu item.

## v0.13.1

Clearer Manage decks save messages, and Preview sync moved under Advanced.

## v0.13.0

New Manage decks panel: choose which decks to sync and edit Preserved fields
(`excluded_decks`).

## v0.12.1

Imports use `merge_notetypes=False`, so AnkiWeb no longer asks for a full sync after
every deck update. Network checks time out instead of freezing Anki.

## v0.12.0

New Preview sync shows what a sync would change without changing anything.

## v0.11.0

Clearer error messages, a marker for new decks, and manifest paths with subfolders.

## v0.10.2

About and the README say the add-on ships with no deck content.

## v0.10.1

Fixed a crash in Import intern pearls deck, removed Restore my notes, and renamed the
backup items for consistency.

## v0.10.0

No ellipses in menu items, `export_deck` is a config key, and `config.md` documents
every config key.

## v0.9.0

The automatic pre-sync backup exports only the configured deck and keeps the 10 most
recent. Added deck backup and restore, and full collection backup.

## v0.8.0

Added Export intern pearls deck.

## v0.7.1

Every dialog carries the Intern Pearls title.

## v0.7.0

Sync state is kept in `user_files/`, so add-on updates no longer reset it. Added Restore
from backup.

## v0.6.0

Sync decks and Import single deck back up automatically first, and confirmations show
per-deck card counts.

## v0.5.1

Fixed menu items disappearing on macOS.

## v0.5.0

Version numbers use three parts.

## Earlier

The menu, history-safe sync and GitHub-based distribution.
