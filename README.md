# Intern Pearls Deck Tools (Anki add-on)

[![Latest release](https://img.shields.io/github/v/release/LTimothy/internpearls-anki)](https://github.com/LTimothy/internpearls-anki/releases/latest)
[![License: MIT](https://img.shields.io/github/license/LTimothy/internpearls-anki)](LICENSE)

Update shared Anki decks without losing your review history or the notes you keep on cards. It is for people who study from a deck someone else maintains, and for the people who maintain those decks.

[Try the live demo](https://ltimothy.github.io/internpearls-anki/). It runs the add-on's own Python code in your browser against a simulated Anki, so you can publish a deck update and sync it through the real dialogs. Nothing leaves the page.

Re-importing an updated `.apkg` overwrites every field on every matched card, and a card whose GUID changed with its wording imports as a new card with no history. This add-on replaces that with an update step. You point it at a GitHub repo or a local folder once, then run Update my decks when the source changes. It:

- imports only the decks whose version changed
- matches cards by GUID, with fallbacks for reworded fronts, so intervals and ease carry over
- keeps the fields you mark as yours (`Notes` by default) through every import
- backs up each affected deck first and keeps the last 10 backups per deck

The add-on ships with no deck content. To publish a deck for it, see [Using this for your own decks](#using-this-for-your-own-decks). [CHANGELOG.md](CHANGELOG.md) lists what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from the [latest release](https://github.com/LTimothy/internpearls-anki/releases/latest).
2. In Anki, choose Tools > Add-ons > Install from file, pick the file, and restart Anki.

An Intern Pearls menu appears before Help. To try it without a deck source, open Manage decks, choose Configure source, then Try the example deck.

## Menu reference

### Update my decks

This is the main command. It fetches the source's `manifest.json`, downloads each changed deck and matches it against your collection before showing anything, so the confirmation shows real counts. It also lists retired cards still in your collection and cards that a deck reorganization moves. Nothing changes until you click Update. Cancel stops a download but never an import partway through.

Each pending card is a row marked NEW, UPDATED (with your current field values under it), RETIRED (with the source's reason, if any) or MOVED. Open a row to read the whole card. With 20 or more cards pending, a bar above the list filters by kind or by cards you have not decided on, and a search box matches card fronts. Filtering only hides rows; your decisions and notes stay.

Each new card offers Import, Later or Never. Each updated card offers Apply, Keep yours, Later or Never. The default is the choice that costs nothing.

- Later keeps the card out of your collection, automatic sync included, and offers it again on your next run. A Later card with a note waits until its source content changes.
- Keep yours comes back only when that card's source content changes.
- Never stops offering the card, or its updates. The run reports how many cards it hides.
- Manage decks > Later and declined cards undoes any of these.

The "Update, and leave N unopened for later" button applies the cards you opened or decided on and sets the rest to Later. Cards with an earlier decision keep it.

A deck source can attach a note to a changed card. Reviewer feedback shows quoted, a maintainer's note unquoted. Cards that share a note are grouped under it, with one control for the whole group. A maintainer's group of five or more cards is folded, and its cards still apply when you click Update. A feedback group is never folded. A source can also label cards (with a question number, say), and labelled cards sort by it.

Notes you type on rows are saved as you type. At the end of the run they appear in a digest, along with your standing Never and Keep yours cards, that you can copy or save. Nothing is sent anywhere. Advanced > Recent card feedback keeps the last 20 digests.

A change to a card's template or CSS costs a one-time full AnkiWeb sync, so it is a checkbox on the confirmation, off by default. A change of card format (Q&A to fill-in-the-blank, or back) costs the same and gets one yes or no for the whole run. Saying no imports those cards as new cards beside the old ones. A conversion that would delete any of a note's cards is never applied. Each of these questions names the cost on its buttons, and Return, Escape or closing the window picks the free choice.

When you confirm, for each changed deck it:

1. Backs up the decks the run will change. Nothing else runs until this succeeds or you choose to continue without a backup.
2. Adds any missing fields to the note type. It never removes or renames a field.
3. Snapshots your protected fields and tags.
4. Matches each incoming card to yours by GUID, then front text, then the manifest's rename maps.
5. Imports through Anki's importer without touching scheduling.
6. Restores your protected fields and your own tags.

Retired cards are archived and moved cards relocated after that, so a replacement is in your collection before the card it replaces is archived. A deck that fails to preview can be retried or skipped. Skipping also leaves its retirements and moves for the next run. If the source fails, the message says whether the host never answered (check your connection) or answered with something unusable (check the token, repo, branch or manifest).

### Manage decks

The panel lists the source's decks, each with a checkbox. Unchecking a deck stops syncing it and leaves its cards alone. This panel also edits the list of protected fields.

Later and declined cards lists every card set to Later, Keep yours or Never, with search and a filter. Offer again forgets the decision, so the card returns on your next update.

Configure source offers three sources:

- GitHub repo: `owner/name`, plus a read-only token for a private repo. The token is stored only in your local config and sent only to GitHub.
- Local folder: a folder with `manifest.json` and the `.apkg` files.
- Try the example deck: [LTimothy/internpearls-example-deck](https://github.com/LTimothy/internpearls-example-deck). It sets `scope_tag` and `export_deck` to the example's values if you had not changed them, and choosing a real source later resets them.

If a manifest recommends a scope tag and backup deck, you are asked before either is applied. The same settings are under Tools > Add-ons > Intern Pearls Deck Tools > Config, and [`config.md`](internpearls/config.md) describes every key. The main ones:

| Key | What it does |
|---|---|
| `github_decks_repo`, `github_token`, `github_ref` | GitHub source, read-only token (blank for a public repo), and branch or tag (default `main`) |
| `decks_dir` | Local folder, used when `github_decks_repo` is empty |
| `scope_tag` | Root tag of the cards the add-on manages (default `InternPearls`). Other cards are never touched. |
| `protected_fields` | Fields kept as yours through every import (default `["Notes"]`) |
| `excluded_decks` | Decks you opted out of |
| `export_deck` | The deck that backups, restore and export use (default `Intern Pearls::Intern Custom`) |

Keys for automatic sync, Night Mode dimming, AI card generation and the duplicate scan are set from their own windows.

### Advanced

- Sync decks: the import half of Update my decks on its own. It lists decks rather than cards, and archives or moves nothing.
- Reconcile my decks: the other half, driven by ledgers in the manifest. A retired card (split, merged or reworded) has its protected-field text copied into the replacement's blank fields, then moves to a `::Retired` subdeck, suspended and tagged. A card the source moved to another deck follows it, unless you refiled it yourself. Nothing is deleted, and running it again changes nothing. With automatic sync on, the menu item shows how many are pending.
- Import single deck (manual): imports one `.apkg` from outside your source, with the same backup, matching and field protection.
- Recent card feedback: reopens the last 20 digests.
- Clean up duplicate cards: finds notes with the same note type and front but different GUIDs, keeps the copy with the most reviews, carries notes over and archives the rest.
- Remove empty cards: Anki's Empty Cards, limited to your scope tag, for blanks a source dropped. It is the one action that deletes cards, and it never leaves a note with no cards.
- Fix note types: adds missing fields to the managed note types. Every sync runs it.
- Backup, Restore and Export intern pearls deck: work on `export_deck`, with scheduling. Restore puts the file's content into matching cards and keeps their scheduling. It backs up first, is one Edit > Undo step, and the restored decks are offered again on your next update.
- Backup full collection and Restore full collection: Anki's own backups. A full restore offers every managed deck again on your next update.
- Check for add-on updates: compares your version with the repo's `version.json` and offers a newer one. Without a token, GitHub allows 60 checks an hour per IP address. A token set in Manage decks raises that to 5,000.

### Experimental

- Generate cards (AI): drafts cards from pasted text, images or PDFs through an AI coding-assistant CLI you have already installed and signed into (Claude Code, Codex CLI or Antigravity CLI). The add-on stores no API key or credential. The three are not sandboxed the same way: Claude Code runs with its tools restricted, Codex CLI runs read-only but can still read files on your computer, and Antigravity CLI mostly relies on its own defaults. The AI Backends window says this per backend, and shows whether each CLI was found, is not responding or was not found. Drafts come back as rows you can edit, fact-check, include or skip. Fields are sanitised, likely duplicates are flagged, and pictures come from a cited web source, your attachments or a drawn diagram, never an AI-generated image. The add-on downloads web pictures itself, over https only, and refuses private or local addresses. Imported cards go to `export_deck::Generated` with fresh GUIDs that no sync touches, in one undoable step. An unfinished draft is saved, without its attachments, and offered next time.
- Night mode dimming: dims bright images, or whole card and deck screens, by 0 to 90% while Anki is in Night Mode.
- Scan for duplicates: finds cards elsewhere in your collection that restate one of your managed cards in other words. It runs locally. You can suspend either card, keep both or ignore the pair, and all of these are reversible. Judge with AI sends each pair's text, without ids or scheduling, to your AI assistant.

### Settings

- Sync decks automatically when updates are available: off by default. Checks the source in the background (every 15 minutes by default, from 1 minute to a week) and applies changed decks after a backup. It never applies a template or format change, which would force a full AnkiWeb sync; those are held back for a manual run. It never archives or moves cards.
- Notify me when a new add-on version is out: on by default.
- Install add-on updates automatically: off by default. A restart loads the new version.

### About

About shows the installed version, your current settings and a link to this repo.

## How history is preserved

Your collection holds only the cards you accepted. Later, Keep yours and Never are checked before every import, background sync included, and a declined card is removed from the package before it is imported. Nothing deletes a card you have, except Remove empty cards.

### Backups

Every sync and import first saves a self-contained backup, with scheduling, of the decks it will change. That is usually `export_deck`, or each top-level deck the run reaches outside it. The last 10 per deck are kept. If a backup fails you are asked whether to continue, and a background sync skips that round. A first sync, with nothing yet to back up, skips it.

### Matching

Cards match by GUID first, so intervals, ease and review counts carry over and a source with stable GUIDs can reword fronts freely. Without a GUID match, your card's front is compared with the current wording, then with renamed wordings the manifest records (`front_aliases`, `superseded_fronts`). Each of your cards matches at most one incoming card. An ambiguous front never matches, and a card that matches nothing imports as a new card beside yours. A deck whose GUID already belongs to a note outside your scope tag is refused.

### Protected fields

Before import, each protected field is compared with the value the source last shipped. A field you never edited takes the source's correction, a field you edited keeps your text, and a clash with a source change is reported. A source can also protect one field on one card (`note_protected_fields`). Fields are restored right after each deck's import, and if that fails the import is rolled back.

### Tags and note types

Updated cards take the source's tags under `scope_tag` and keep every other tag, such as `leech`, `marked` and your own. Note types only gain fields. Imports never merge note types, which keeps AnkiWeb syncs incremental, so a template or CSS change applies only with your consent and never in background sync.

### Undo and restore

Each deck update and each restore is one Edit > Undo step. Undoing an update offers that deck again on your next update. Redo puts the cards back but not the add-on's own records. Restoring a backup clears the matching sync records, so what came back is offered again rather than reported up to date.

### Scope and state

Only cards under `scope_tag` are snapshotted and matched. Synced versions, shipped-field baselines, decisions, unsent notes and backups live in the add-on's `user_files/` folder, kept apart per collection and source so profiles never share decisions. That folder survives add-on updates but not uninstalling. The AI feature's rules, usage log and last failed run log are there too, shared by all profiles.

### Package checks

A deck package is refused before it is opened if it is over 512 MB, holds more than 50,000 files, unpacks to more than 2 GB, or has any one file over 1 GB unpacked. It is also refused if a file name inside it is an absolute path, has a drive prefix, contains `..` or contains a NUL byte.

## Using this for your own decks

The quickest start is [LTimothy/internpearls-example-deck](https://github.com/LTimothy/internpearls-example-deck), a template repository. Click "Use this template", edit the JSON card specs in your browser, and its GitHub Action rebuilds the `.apkg` files and manifest. Its README covers creating, sharing and updating a deck.

With your own tooling, put a `manifest.json` in a GitHub repo (public or private) or a local folder, beside the `.apkg` files it lists:

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

- `decks`: `name` is the Anki deck name, `apkg` the path from the root, `version` any string that changes when the deck changes, and `cards` an optional count. `spec` is informational.
- `scope_tag`, `export_deck`: the values you recommend to subscribers.
- `front_aliases` (`{current front: previous front}`) and `superseded_fronts` (`{old front: current front}`): renamed fronts, so a card matched by text survives a reword.
- Stable GUIDs matter most. Derive each GUID from an explicit per-card id, not the front, and fronts can change freely.
- Optional ledgers for Reconcile my decks: `retired` (`{deck: {guid: {identity, reason, superseded_by}}}`) and `deck_moves` (`{guid: {from, to, front}}`, full deck paths).
- Optional extras: `change_notes` (`{guid: [{on, kind, note}]}`), `note_sources` (`{guid: label}`), `note_protected_fields` (`{guid: [field]}`) and `skill` (`{path, version}`, card-writing instructions the AI feature can use with the subscriber's consent).
- `schema` is the manifest format version. The add-on refuses a newer schema than it knows, so raise it only for a change older versions cannot read. Unknown keys are ignored.

Then choose Configure source in Manage decks.

## For developers

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, both test suites, conventions and pull requests. Neither suite needs Anki: `tests/` runs against `tests/mock_anki.py`, and `qt_tests/` renders the real dialogs with PyQt6.

Code that plain Python can test lives in `internpearls/logic.py`, `ai_logic.py` and `dupes.py`, with no `aqt` or `anki` imports. Code that touches Anki is split by concern:

- `__init__.py`: menu and startup wiring only
- `config.py`: constants, config and the state files under `user_files/`
- `ui.py`: dialog wrappers and error handling
- `palette.py`: every colour
- `platform.py`: background work and timers
- `net.py`: fetches
- `collection.py`: collection reads and writes, and the Advanced actions
- `sync.py`: the sync, reconcile and import flows
- `updates.py`, `background.py`: self-update and the background checks
- `dialogs.py`, `review.py`, `widgets.py`: panels, the update and result screens, and the shared rows and lists
- `ai_*.py`, `dupes_dialog.py`, `nightmode.py`: the Experimental features

The [live demo](https://ltimothy.github.io/internpearls-anki/) runs the same modules under Pyodide. `./build.sh` packages `internpearls/` into `internpearls.ankiaddon` and copies it to `docs/addon/`, and a test checks the copy matches. `tools/render_dialog.py` renders a real dialog to a PNG (`--list` shows which, `--dark` uses the dark palette).

### Colors

Colours live in `internpearls/palette.py`, in a light and a dark set, because no single mid-tone is readable on both of Anki's backgrounds. If you set a background, set its text colour from the same set; otherwise the text follows the system theme and the background does not. For a plain sunken block, use `background: palette(base); color: palette(text); border: 1px solid palette(mid)` and let Qt pick per theme. `tests/test_palette.py`, a stylesheet check in `tests/test_review.py` and `qt_tests/test_contrast.py` enforce this.

### Versioning

Versions follow semver: PATCH for a fix or internal change, MINOR for a new feature, MAJOR for a change that requires reconfiguring.

To release, bump `ADDON_VERSION` in `internpearls/config.py` and `version` in `version.json` together, add a `CHANGELOG.md` entry, run `./build.sh`, commit, and push a `vX.Y.Z` tag. The release workflow runs both test suites, checks the tag against `version.json` and attaches the committed `.ankiaddon`. `tests/test_release_integrity.py` checks the same locally, except the tag.

Self-update reads `version.json` and `internpearls.ankiaddon` from `main` through the GitHub contents API, so the release page does not affect what people receive.
