# Intern Pearls Deck Tools (Anki add-on)

[![Latest release](https://img.shields.io/github/v/release/LTimothy/internpearls-anki)](https://github.com/LTimothy/internpearls-anki/releases/latest)
[![License: MIT](https://img.shields.io/github/license/LTimothy/internpearls-anki)](LICENSE)

**Update shared Anki decks without losing your review history or the notes you've written on cards.**

**[Try the live demo](https://ltimothy.github.io/internpearls-anki/)**: the add-on's actual Python code running in your browser, with only Anki itself simulated. Publish a deck update, sync it through the real dialogs, and watch scheduling and personal notes survive. Nothing leaves the page.

Shared decks have no update path. Re-importing an updated `.apkg` overwrites every field, wiping the annotations people keep on their cards, and a reworded card silently loses its scheduling. This add-on gives a shared deck a real one: point it at a GitHub repo or a local folder once, and Update my decks handles the rest.

- **Syncs only what changed.** A version hash per deck means editing one deck doesn't re-import ten.
- **Keeps review history.** Cards match by GUID, with fallbacks for reworded fronts, so intervals and ease survive every update.
- **Keeps your annotations.** Fields you mark as yours (`Notes` by default) come through every import untouched.
- **Backs up first, always.** A timestamped `.apkg` of each affected deck is saved before any import, keeping the last 10 per deck.
- **Stays current on its own.** Optional background deck sync, and self-update for the add-on.

**Maintaining a deck of your own?** The add-on ships with no deck content. Host a small `manifest.json` next to your `.apkg` files and everyone studying from your deck gets the same one-click updates. See [Using this for your own decks](#using-this-for-your-own-decks). `CHANGELOG.md` records what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from the [latest release](https://github.com/LTimothy/internpearls-anki/releases/latest).
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

An "Intern Pearls" menu appears between Tools and Help. Update my decks and Manage decks sit at the top; occasional tools are under Advanced, newer features under Experimental, and Settings and About at the bottom.

No deck source yet? Open Manage decks, choose Configure source, and pick "Try the example deck" to watch a sync work end to end.

## Menu reference

### Update my decks

The main button. It fetches `manifest.json` from your deck source and works out everything pending in one pass: changed decks, retired cards still in your collection, and cards a deck reorganization needs to relocate. Each changed deck is downloaded and matched against your collection first, so the confirmation shows real counts ("12 kept · 3 new", plus "changed" for cards rewritten upstream). A progress bar with a working Cancel covers the download and the apply step; Cancel stops a transfer, never a half-finished import. The confirmation is a preview: nothing applies until you click Update.

**The card list.** Every pending card is a row with a chip: NEW (you don't have it), UPDATED (its content changed upstream, with your current field values shown under it), RETIRED (being archived because a split, reword or removal replaced it, with the source's reason when there is one), or MOVED (being relocated to match a reorganized deck). Click a NEW or UPDATED row to read the whole card. The list builds in batches as you scroll, so it opens instantly however much is pending.

**Decisions.** NEW rows offer Import / Skip / Never; UPDATED rows offer Apply / Keep yours / Never. The default is the choice that costs nothing. Skip, Keep yours and Never open a note box for an optional reason; any row has an "Add note" link once opened. Never collapses the row to "won't be offered again". A skipped card returns the next time its deck changes; a kept card returns only when that card's source content changes, pre-set to the same choice and wearing a SKIPPED or KEPT YOURS chip, with a "changed since" hint if the source moved since your decision. Never cards don't return as rows; the run reports how many are held back. Nothing here deletes a card you already have, and every decline can be undone from Manage decks > Declined cards.

**Why a card changed.** A deck source can ship a note with a changed or new card. Reviewer feedback appears quoted under the card's header, marked "from feedback"; a maintainer's note appears unquoted. Older feedback stays underneath as dated "earlier feedback". Maintainer notes show only for the exact content they describe. A first sync of a deck shows none. When one note explains several cards, it appears once with every row it caused beneath it; a group of five or more is folded behind the note with a "Show N cards" button, and its cards still apply when you press Update. A source can also label where a card came from (a question number, say); the label sits above any note, and labelled cards sort by it in numeric order within each deck.

**Your notes are kept.** What you type in a note box is saved to disk as you type. At the end of the run your notes, plus a snapshot of every standing Skip, Keep yours and Never choice, arrive as a digest you copy and send yourself; nothing is transmitted. Anything unsent is picked up by your next update.

**How cards are shown.** A fill-in-the-blank card shows its blanks filled in. When a field holds more than one blank group, each blank carries its group number (c1, c2). A hint shows in brackets beside its answer, and a change that only moves a hint is named as one. Pictures are named while a row is closed and rendered from the already-downloaded deck when you open it.

**Look and format changes.** A change to how cards look (a template or its CSS) is a checkbox on the confirmation, unticked by default, because applying it costs a one-time full AnkiWeb sync; leaving it keeps your current appearance and imports everything else. A change to a card's format (Q&A becoming fill-in-the-blank) costs the same and gets one yes or no after you click Update and after the backup, covering the whole run. Saying no imports those cards as new cards beside your old ones. A deck whose download failed twice, or whose new format needs a note type you don't have yet, stays pending with the reason given. Declined cards are never part of this.

Every question whose two answers cost different things names them on its buttons, and Return, Escape or closing the window all choose the one that costs nothing.

**On confirm**, content applies first, then retired cards archive and moved cards relocate, so a replacement is in your collection before its predecessor archives. Nothing downloaded for the preview is fetched again; a deck the preview couldn't download ("couldn't preview · still imports") is fetched again before anything is backed up. For each changed deck it:

1. Backs up the decks the run will change (see "How history is preserved"). Nothing else runs until this succeeds, or you explicitly continue without one.
2. Adds any missing fields to the note type (never removes or renames).
3. Snapshots your protected fields on every card in scope.
4. Matches each incoming card to yours: by GUID, then front text, then the manifest's rename maps.
5. Imports through Anki's importer without touching scheduling.
6. Restores your protected fields.

The run ends with one summary dialog, including the digest when you flagged or declined anything. If no source is configured it points you to Manage decks; if nothing is pending it says you're up to date. If the source can't be used it says whether the host never answered (check your connection) or answered with something unusable (a bad token, wrong repo or branch, missing or invalid `manifest.json`: change the source).

### Manage decks

Lists every deck the source offers, with a checkbox, its card count, and a NEW or UPDATED chip where one applies. Unchecking a deck stops future syncs for it; its cards stay in your collection and Reconcile leaves them alone. The panel also edits `protected_fields`. Save keeps your choices; Save and update now also runs Update my decks.

**Declined cards (N)** lists every card you skipped, kept your version of, or said Never to, grouped by decision, each with its deck, date, and an Offer again button that forgets the decision so your next update re-offers it.

**Configure source** (or Change source) sits beside the Source line and offers three sources, the example deck first:

- **GitHub repo**: `owner/name`, plus a read-only personal access token only for a private repo. The token is masked, stored only in your local config, and sent only to GitHub.
- **Local folder**: a directory holding `manifest.json` and the `.apkg` files.
- **Try the example deck**: points at [`LTimothy/internpearls-example-deck`](https://github.com/LTimothy/internpearls-example-deck) and temporarily sets `scope_tag` and `export_deck` to its values (if you hadn't customized them); choosing a real source later resets exactly those.

After saving, the add-on connects. If the manifest recommends a scope tag and backup deck, you're offered them; nothing changes unless you say yes. Manage decks then reopens showing either the decks found or exactly what went wrong. With nothing configured it still opens, with an empty list and the Configure source button.

The same settings are under Tools > Add-ons > Intern Pearls Deck Tools > Config:

| Key | What it does |
|---|---|
| `github_decks_repo` | GitHub repo, e.g. `owner/repo-name` |
| `github_token` | Read-only fine-grained token; blank for a public repo |
| `github_ref` | Branch or tag to pull from (default `main`) |
| `decks_dir` | Local folder, used when `github_decks_repo` is empty |
| `scope_tag` | Root tag of the cards this add-on manages (default `InternPearls`). Scopes snapshots and matching so other decks are never touched. |
| `protected_fields` | Fields kept as yours through every import (default `["Notes"]`). |
| `excluded_decks` | Decks opted out of syncing. |
| `export_deck` | The deck the automatic backup and the Backup/Restore/Export actions use (default `Intern Pearls::Intern Custom`). |
| `auto_sync_decks`, `auto_sync_interval_minutes`, `notify_addon_updates`, `auto_update_addon` | Automation; see Settings and `config.md`. |
| `dim_images_night_mode`, `dim_images_night_mode_percent` | Night Mode dimming for every deck (0-90%); see Experimental. |

### Experimental submenu

Newer features: **Generate cards (AI)**, **Night mode dimming**, and **Scan for duplicates**.

#### Generate cards (AI)

Drafts cards from material you paste or attach, through an AI coding-assistant CLI you have already installed and signed into. The add-on has no API key field and never reads, sends or stores a credential; it only runs a CLI you set up yourself.

**AI Backends** (opened from the wizard: "Set up an assistant", or the Setup link) holds everything about backends. Each backend is one row: a chip for what the check found (found, not responding, not found, ignored), its command, badges for image input and free-tier limits, the subscription it needs, and an install-guide link. The check is a free `--version` call, so "found" means the binary runs, not that you're signed in. One backend is preferred ("Use ..." switches; "ignore" sets one aside). Below the rows, a panel for the preferred backend sets its executable path (blank auto-detects), Model, Effort, and has Test connection, which runs one tiny real prompt and reports working or not working in plain words. It is disabled for a backend that wasn't found.

- **Claude Code** (Claude Pro or Max): tools fully restricted by the add-on. The strongest of the three.
- **Codex CLI** (any ChatGPT account; the free tier is capped at roughly 50 agentic messages a day): sandboxed read-only with no writes; its own web search is used when the installed version has it. Read-only still means it can read files on your machine and fold them into drafts.
- **Antigravity CLI** (a free, throttled Google tier; it replaced Gemini CLI for personal Google AI Pro and Ultra accounts): gets `--sandbox` when its help documents it, never a file-writing tool, and otherwise relies on its own approval defaults.

They are not equally sandboxed, and AI Backends says so. All three read attached images.

**Input.** Paste material, attach images or PDFs, or both. Attachments are read in the background with a Cancel; each file is limited to 25 MiB, and a PDF to 100 pages, 500,000 characters of text, and 50 images totalling 25 MiB. Add an optional focus line. Four status rows summarise the run, each with a link:

- **Backend**: the preferred assistant, model and effort, or NOT SET UP.
- **Cards and depth**: AUTO, THOROUGH or QUICK. By default the assistant decides the count, one card per point the source teaches, up to 40, and depth is Thorough for 1,500+ characters or any attachment, Quick otherwise. Advanced pins an exact count or a depth for that run only.
- **Deck**: the destination and the note types in play (Basic and Cloze; Image ID isn't offered, since a generated card can't fill an image-only field). Changed under Advanced.
- **Skills**: what is sent every run (the bundled authoring skill, a consented deck skill, your own rules), with View and Add/Edit my rules links.

**Modes.** Thorough (about 1 to 3 minutes) drafts and then self-reviews; Quick (about 15 seconds to a minute) is one drafting pass that may search only for images. What that means for web access differs by backend, and the Cards and depth row states it for yours:

- **Claude Code**: Quick gets up to 6 turns, web search for images, and read access to your attachments; Thorough gets up to 15 turns and web search.
- **Codex CLI**: commands sandboxed (read-only in Quick); Thorough verifies online and Quick searches for images with Codex's own web search. No turn cap.
- **Antigravity CLI**: modes differ only in the prompt; its own defaults may allow web access in either. No turn cap.

Claude Code defaults to `sonnet` at `medium` effort, because an unset model runs the account's default, which on Max is the top model and burns credits across Thorough's turns. Model and effort are stored per backend. Codex CLI and Antigravity CLI receive `--model` (and Antigravity `--effort`, with ids from `agy models`) only when you set one and the installed binary's help documents the flag.

**Progress.** A status row shows the phase, elapsed time (with the median of your last 10 runs in that mode once there are some), and a Cancel link; Escape also cancels. A feed below lists what the assistant is doing and counts cards as they stream. A run stops only after two minutes of silence (three in Thorough) or a 15 or 30 minute ceiling. A failed run's raw output is kept in `user_files/ai_last_run.log`, with any line that repeats your material, rules or the prompt left out.

**Review.** Drafted cards come back as rows like Update my decks, each with Include/Skip (Include by default) and a chip for its note type. Mechanical checks (likely duplicate, invalid cloze, overlong answer, image that failed to resolve) flag a card and default it to Skip. Edit opens every field and the tags in one dialog. Revise all sends the set back with your notes; unnoted cards come back unchanged. **Check facts** asks for a verdict per card in Thorough mode: Confirmed (with source links), Corrected (with the proposed text and Accept / Keep mine), or Unverified with a reason; on a Codex CLI without web search every card is Unverified. A new draft or revision clears old verdicts. Import writes only the cards on Include.

**Images.** No card ever carries an AI-generated raster image. An image comes from a real web source the assistant cites, a file you attached, or SVG the model draws (checked for scripts). With web tools available, in either mode, a figure of a real thing (an ECG, anatomy, radiology, a device) must be a found and cited image, and drawing is kept for simple schematics; without web tools, the figure is skipped. A PDF's embedded images usually can't be extracted without Pillow, which Anki's Python lacks; the wizard says so, and attaching figures as image files works. A card with an image starts on Skip until you've seen it. Images are resolved in the background during review, with a thumbnail and, for a web image, its host shown on the row. That download is the add-on itself contacting the host the model named, in every mode, including Quick. Import writes exactly what review resolved.

**Where cards land.** In `export_deck::Generated`, tagged `Generated` under your scope tag, each with a fresh local GUID no deck source uses, so no sync, reconcile or duplicate cleanup ever touches them. Import is one undoable step.

**What's kept.** Only your backend choice and CLI path, any deck-skill consent, a 7-day per-backend usage log (runs and approximate tokens), and the last 10 run durations per backend and mode. Your material, drafts, notes and the exchanged prompts live only in memory and are discarded, with the scratch folder of attachments and downloaded images, when the wizard closes; closing with unsaved drafts asks first. Leftover scratch folders from a crash are swept after startup (older than a day, or oldest first above 200 MB), only ever the add-on's own.

**Skills.** View shows exactly what is sent on top of your material: the bundled InternPearls authoring skill, a deck-specific skill only if your source offers one and you consented (the same link enables or disables it), and your own rules.

##### My rules

Add my rules (Edit my rules once saved) opens a plain-text box for standing instructions, sent after the bundled and deck skills on every run, up to 20,000 characters. They are yours alone. On style, wording and emphasis your rules win over the bundled skill; the output format and the no-raster-image rule always win. Saving an empty box clears them.

#### Night mode dimming

Softens bright content while Anki is in Night Mode, never in Day mode, for every deck in your collection. Native windows such as menus and dialogs are not affected.

- **Dim in Night Mode**, off by default.
- **Dim by N%**, default 30, range 0-90, with a live Normal/Dimmed preview using the exact transform Night Mode renders.
- Scope: **Bright images only** (default), or **Everything on cards and deck screens** (cards, deck list, overview and editor, dimmed as whole pages).

The images scope applies from the next card shown; the whole-page scope from the next screen that loads.

#### Scan for duplicates

Finds cards elsewhere in your collection that restate a fact your managed cards already carry, in different words, by comparing the words each card uses weighted by how rare they are. By default it compares this add-on's cards against the rest of your collection; either side can be narrowed to one deck, and a filtered deck's cards count under their home deck. It runs in the background over text already in your collection, with no network call.

- **Sensitivity** (Strict / Normal / Loose, remembered): Strict and Normal also require a pair to share at least two informative words (three for long cards) carrying a real share of the shorter card's vocabulary, so one shared rare word can't carry a match; Loose uses the raw score. A side with fewer than 50 cards is flagged as a thin comparison.
- **Exclude decks**: comma-separated names or partial names, applied to both sides. The summary reports what was excluded and flags an entry that matches no deck.

Each candidate row shows its band and score, both fronts, the other card's deck and note type, and the shared words that drove the match (an image filename never counts). Suspend ours, Suspend theirs, Keep both and Ignore pair are on the row; all are reversible, and Ignore pair keeps the pair from returning. Opening a row shows both answers. Copy list copies the candidates as text.

**Judge with AI** sends each pair's front and back text (no ids, no scheduling data) to your assistant in one Thorough turn and marks rows DUPLICATE or OVERLAPS; pairs judged different fold below "Judged different". It only changes what rows say.

### Advanced submenu

Occasional tools, in four groups: acting on the deck source, repairing the collection, backup and restore, and the add-on's own update check.

**Sync decks** is the content-only half of Update my decks: it imports only decks whose version changed, with the same backup, matching and field protection. Its confirmation lists decks rather than cards, since nothing is downloaded yet, and it doesn't archive or relocate.

**Reconcile my decks** is the other half, run on its own, driven by ledgers in the manifest:

- *Retired cards.* When a source splits, merges or reword-replaces a card, sync adds the new cards but never removes your old copy. Reconcile copies your protected-field text onto the replacement (only into a blank field), then moves the old card to a `…::Retired` subdeck, suspends it and tags it. **It never deletes anything.** Run alone, it warns if replacements aren't in your collection yet.
- *Reorganized decks.* When a source moves a card to another deck, sync updates its content but leaves it where it is. Reconcile moves it, but only if it's still exactly where the source last put it; a card you refiled yourself is left alone.

Both are schema-neutral, reversible by hand, backed up first, and no-ops when re-run. With auto-sync on, a pending backlog shows on this menu item ("Reconcile my decks (3 pending)").

**Import single deck (manual)** imports one `.apkg` from outside your source, with the same matching, backup, field protection and format-change question.

**Clean up duplicate cards** finds notes sharing a note type and front but with different GUIDs (usually left by a reorganization), keeps the copy with the most reviews (ties prefer the source's canonical deck), carries personal notes over, and archives the rest like Reconcile. Backed up first; nothing deleted.

**Remove empty cards** removes cards with nothing left to show, such as the "No cloze 3 found on card" leftovers when a source drops blanks from a card. It is Anki's Tools > Empty Cards limited to your scope tag, listing every card and missing blank number before acting. It is the one action that deletes rather than archives, because an empty card holds nothing: its note keeps every field. It never leaves a note with zero cards, skips any note whose cards are all empty, re-checks the report after you confirm, and backs up first.

**Fix note types** adds missing fields to the managed note types (Study Deck - Basic, Cloze, Image ID). It never removes or renames fields or touches scheduling. Every sync runs it.

**Backup intern pearls deck** makes an on-demand backup of `export_deck` with scheduling, like the automatic one.

**Restore intern pearls deck** re-imports a backup or export with scheduling (the picker opens at the backups folder). Matching cards update in place. The decks it restores are re-offered on your next update.

**Export intern pearls deck** writes a standalone `.apkg` of `export_deck` with history, options and media, wherever you choose.

**Backup full collection** takes a whole-collection backup on demand, kept per Anki's own backup settings.

**Restore full collection** opens Anki's backup picker. It replaces your entire collection, and every managed deck is re-offered on your next update.

**Check for add-on updates** compares your version with the repo's `version.json` and offers to install a newer one (restart required). It reads GitHub's API without a token, limited to 60 requests an hour per IP address; when that's exhausted it says so and when it resets. Setting a GitHub token in Manage decks (any repo, read access) raises the limit to 5,000.

### Settings

How automatic the add-on is, kept apart from Manage decks (which decks, which fields, from where):

- **Sync decks automatically when updates are available**, off by default. Checks the source in the background and applies changed decks, backing up first. It never applies a change that forces a full AnkiWeb sync (a template or a format change): that deck is held for the rest of the session with a tooltip pointing to Sync decks. It never archives or relocates; Reconcile my decks shows the backlog instead.
- **Check every N minutes**, default 15, from 1 minute to a week. Checks run off the main thread and fail fast if the source is unreachable.
- **Notify me when a new add-on version is out**, on by default, checked once per launch.
- **Install add-on updates automatically**, off by default. Installs during that check; a restart loads it.

### About

What the add-on does, a summary of your settings, a reminder that no deck content ships with it, and a link to this repo.

## Updating decks

Run Update my decks, or turn on automatic sync in Settings so content applies on its own (archiving and relocating always stay a manual confirm). Only changed decks import, and each run backs up first. Advanced > Backup full collection adds whole-collection protection on demand.

## How history is preserved

**Your collection holds exactly the cards you said yes to.** Every Skip, Keep yours and Never is checked before any import, interactive or background, and a declined card is removed from the downloaded package before it reaches your collection. A decline always wins over a match, and a declined card's format change is neither asked about nor applied. Nothing deletes a card you have: Never stops offering it, and Keep yours lasts until that card's source content changes or you choose Offer again.

**Backups.** Every sync and manual import starts with a timestamped, self-contained backup of the decks it is about to change, including scheduling: normally exactly `export_deck`, and one backup per top-level deck when a run reaches outside it. What counts as changed is read from where the affected cards actually sit, including archiving, relocating and merging. Backups are kept 10 per deck, each deck's files kept apart by a hash of its name. If a backup can't be made you're asked whether to continue (a background sync skips that round instead), and the same applies when your cards sit in no deck the add-on can export. A first sync, with nothing yet in your collection, skips the backup.

**Matching.** Cards match by GUID, so intervals, ease and review counts carry over. Matching runs strongest signal first: a GUID match needs no text comparison, so a source with stable GUIDs can reword fronts freely. When the GUID doesn't match (typically cards that predate stable ids), your card's current front is compared against the current wording, then against renamed wordings the manifest records (`front_aliases` for the most recent rename, `superseded_fronts` for any reworded card). If that map can't be fetched you're warned before importing. A card that matches nothing imports as a new card beside your old one; nothing is lost. Matching uses every scoped note even when several share a front, never guesses on an ambiguous front, and refuses a deck whose incoming GUID already belongs to a note outside your scope tag.

**Protected fields.** Your `protected_fields` (`Notes` by default, any field names you choose) are snapshotted by GUID before import and compared three ways against the value the source last shipped: an untouched field takes the source's correction, a field you edited keeps your version, and a clash with a source change is reported. A deck source can also protect one field on one card (`note_protected_fields` in its manifest); that protection follows the card even when your copy is matched by front text rather than GUID. Without a prior baseline, a non-empty value you already have is kept. Restoring happens before any bookkeeping is written, so a bookkeeping failure can't skip it.

**Imports.** Source content is applied even when your local edit is newer; restoring a backup applies the backup's content. Anki's own import results decide which fields get a new shipped baseline. A rejected import stays pending and its deck operation is rolled back. Legacy packages with a `collection.anki21` are read from that database, not the placeholder beside it.

**Note types.** Note types only gain fields; your customizations stay. An approved conversion between note types is refused if it would drop non-empty protected content with no destination field. Choosing "Import them as new" keeps your original note and its history as a local copy outside future matching, and later updates match the new note without repeated copies. If the following import fails, the conversion is rolled back.

**Appearance.** Imports never merge note types, which keeps AnkiWeb syncs incremental (see "For developers"), so a template or CSS change is only ever applied with your consent. Background sync never applies one.

**Scope.** The snapshot and matching cover only cards under `scope_tag` (default `InternPearls`); everything else is ignored. The backup is always a real Anki export.

**State.** Synced versions, shipped-field baselines and backups live in the add-on's `user_files/` folder, which survives add-on updates but not an uninstall, so export anything you want to keep before removing the add-on. They are separated by collection path and source under `user_files/collections/`; older unscoped files are left on disk but not trusted, so the next sync may re-offer decks. Backup filenames carry the full deck identity and a unique suffix. Restoring a backup clears the matching sync records (Restore full collection clears them all; Restore intern pearls deck clears only the decks in the file), so what came back is re-offered rather than reported up to date.

**Generated cards** sit outside all of this: their fresh local GUIDs never match anything a source ships, so no update, reconcile or cleanup ever touches them.

## Using this for your own decks

**The easy way: start from the example deck.** [LTimothy/internpearls-example-deck](https://github.com/LTimothy/internpearls-example-deck) is a template repository: click "Use this template", edit the JSON card specs in your browser, and its GitHub Action rebuilds the `.apkg` files and manifest when the cards change. Its README walks through creating, sharing and updating a deck.

For your own tooling, host a `manifest.json` in a GitHub repo (private or public) or a local folder, beside the `.apkg` files it references:

```json
{
  "schema": 3,
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

- `decks`: every deck to manage. `name` is its Anki deck name; `apkg` is its path relative to the root; `spec` is informational; `version` is any string that changes when the deck changes; `cards` is an optional count for the confirmation.
- `scope_tag`, `export_deck`: your recommended values for those config keys, offered to each subscriber when they configure your source.
- `front_aliases`: `{current front: previous front}` for cards whose front changed. `superseded_fronts` (`{old front: current front}`) covers every reworded card, so subscribers holding both wordings are merged onto one.
- **Stable GUIDs matter most.** Tools that derive a GUID from the front change it on every reword. Derive it from an explicit per-card id instead, and fronts can change freely. This add-on syncs pre-built decks; how you build GUIDs is up to your tooling.
- Optional ledgers for Reconcile my decks: `retired` (`{deck: {guid: {identity, reason, superseded_by}}}`) for split, merged or removed cards, and `deck_moves` (`{guid: {from, to, front}}`, full deck paths) for relocated cards.
- Optional per-card extras: `change_notes` (`{guid: [{on, kind, note}]}`, shown on the confirmation), `note_sources` (`{guid: label}`), `note_protected_fields` (`{guid: [field]}`), and `skill` (`{path, version}`, a card-authoring skill the AI wizard can use with the subscriber's consent).
- `schema` is the manifest format version. An add-on refuses a manifest newer than it understands, so raise it only for a change older add-ons can't read. Older add-ons ignore keys they don't know.

Then use Configure source in Manage decks, and everything above works against your content.

## For developers

### Code layout

Pure Python with no `aqt`/`anki` imports lives in `internpearls/logic.py` (apkg reading and rewriting, GUID matching, version comparison, the update decision, formatting), `ai_logic.py` and `dupes.py`. A new function belongs there if plain Python can test it.

Everything that touches Anki is split by concern:

- `__init__.py`: menu and startup wiring only.
- `config.py`: constants (including `ADDON_VERSION`), config access, and state under `user_files/`: `installed.json`, `card_feedback.json`, `shipped_fields.json`, `state.json`, and the declined-card registry `declined.json` (`{guid: {state, front, deck, decided, hash}}`).
- `ui.py`: the `_info` / `_warn` / `_ask` / `_prompt` dialog wrappers, the `_safe` / `_bg_safe` error decorators, and styling helpers.
- `palette.py`: every colour, in a light and a dark set.
- `platform.py`: background work and timers. Work runs on a worker thread and delivers on the main thread; timers are parented to the widget that owns them.
- `net.py`: HTTP and GitHub contents-API fetches, timeouts, and `TransportError` for a host that never answered.
- `collection.py`: everything that reads or writes `mw.col`: note types, backups, the protected-field snapshot and restore, import and export, and the Advanced actions.
- `sync.py`: source resolution, Sync decks, Reconcile my decks, Update my decks, and Import single deck.
- `updates.py`: self-update. `background.py`: the startup update check and the auto-sync poll, dispatched through `platform.py`.
- `dialogs.py`: Manage decks, Settings, Night mode dimming, About, source configuration.
- `review.py`: card rows and the feedback digest for the update and summary screens (kept out of `dialogs.py`, which imports `sync.py`).
- `widgets.py`: chips, headings, rows and `StreamingList`, shared by every list screen.
- `ai_cli.py`, `ai_setup.py`, `ai_dialog.py`, `dupes_dialog.py`, `nightmode.py`: the Experimental features.

### Running tests

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -q
```

No Anki install is needed. `tests/` runs against `tests/mock_anki.py`, stub `aqt`/`anki` modules plus a mock collection that reproduces the importer behaviour everything here defends against (a GUID-matched import overwrites every field):

- `test_release_integrity.py`: the committed `.ankiaddon` matches `internpearls/`, and `version.json` matches `ADDON_VERSION`.
- `test_logic.py`: `logic.py` against minimal `.apkg` files.
- `test_sync_flows.py`: the real sync, collection and background modules end to end, with dialog answers scripted.
- `test_dialogs.py`: the real dialogs and menu, serialized to a tree and driven by scripted clicks through the same protocol the live demo uses.

`qt_tests/` renders the real dialogs with real PyQt6 and asserts on what they paint, because Qt silently drops a stylesheet rule it dislikes. It runs as a separate command, since real and mock Qt can't share a process; see `CONTRIBUTING.md`.

The [live demo](https://ltimothy.github.io/internpearls-anki/) is generated from the code: `./build.sh` mirrors `internpearls/` into `docs/addon/` (byte equality enforced by `tests/test_demo_parity.py`), and the page runs those modules under Pyodide against the example deck's real files. `browser_tests/` checks the demo against a generated contract (`python3 tools/demo_npm.py run test:demo-contract`).

### Seeing a dialog actually render

`tools/render_dialog.py` renders a real dialog to a PNG with real PyQt6 and no Anki:

```bash
python3 -m pip install PyQt6
python3 tools/render_dialog.py --list
python3 tools/render_dialog.py confirm --expand 1 --feedback --out confirm.png
python3 tools/render_dialog.py confirm --dark          # see "Colors" below
```

It reuses `tests/mock_anki.py` for the fake Anki world and swaps in real Qt. Its card content is synthetic unless you pass `--apkg`. Use it for any change to a stylesheet, border, spacing or colour.

### Colors

Colours live in `internpearls/palette.py`, in a light and a dark set chosen from Anki's `theme_manager.night_mode`. No single mid-tone clears WCAG AA on both Anki backgrounds, so there is no theme-neutral value. `tests/test_palette.py` checks the values.

**If you hardcode a background, hardcode its foreground from the same set.** Text otherwise follows the platform palette, which flips with the theme while your background doesn't. A colour-only style is safe; a background-only style is not. When a block just needs to look sunken, use `background: palette(base); color: palette(text); border: 1px solid palette(mid)` and let Qt resolve it per theme. A source lint over every `setStyleSheet` call (`tests/test_review.py`) and the real-Qt contrast suite (`qt_tests/test_contrast.py`) back this up.

`--dark` selects the real dark set and approximates Anki's window colours through Qt's colour-scheme hint, so `palette()`-based colours may look slightly different from the real app.

### Repackage after editing

```bash
./build.sh          # zips internpearls/ into internpearls.ankiaddon and refreshes docs/addon/
```

### Versioning

Three-part semver: PATCH for a fix or internal change, MINOR for a new feature, MAJOR for a change that requires reconfiguring.

To release, bump `ADDON_VERSION` in `internpearls/config.py` and `version` in `version.json`, add a `CHANGELOG.md` entry, run `./build.sh`, commit, then push a `vX.Y.Z` tag. `.github/workflows/release.yml` runs both test suites, checks the tag against `version.json`, takes the notes from that version's changelog section, and attaches the committed `.ankiaddon`; any failure means no release. `tests/test_release_integrity.py` checks the same things locally, except the tag.

The release page is not how anyone gets the add-on. Self-update reads `version.json` and `internpearls.ankiaddon` from `main` through the contents API, so a late or missing release changes nothing about what people receive.
