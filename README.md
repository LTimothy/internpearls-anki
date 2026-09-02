# Intern Pearls Deck Tools (Anki add-on)

[![Latest release](https://img.shields.io/github/v/release/LTimothy/internpearls-anki)](https://github.com/LTimothy/internpearls-anki/releases/latest)
[![License: MIT](https://img.shields.io/github/license/LTimothy/internpearls-anki)](LICENSE)

**Update shared Anki decks without losing your review history or the notes you've written on cards.**

**[Try the live demo](https://ltimothy.github.io/internpearls-anki/)**: the add-on's actual Python code running in your browser (only Anki itself is simulated). Publish a deck update, sync it through the real dialogs, and watch scheduling and personal notes survive. No install, and nothing leaves the page.

If you maintain a deck for a study group (or subscribe to one someone else maintains), you've hit the problem: shared decks have no update path. Re-importing an updated `.apkg` overwrites every field, wiping the annotations people keep on their cards, and a reworded card silently loses its scheduling. This add-on gives a shared deck a real one: point it at a GitHub repo or local folder once, and Update my decks handles the rest.

- **Syncs only what changed.** A version hash per deck means editing one deck doesn't re-import ten.
- **Keeps review history.** Cards match by GUID (with a rename map for reworded fronts), so intervals and ease factors survive every update.
- **Keeps your annotations.** Fields you mark as yours (`Notes` by default) are snapshotted before import and restored after.
- **Backs up first, always.** A timestamped `.apkg` of the deck is saved before any import, keeping the last 10 of each deck backed up.
- **Stays current on its own.** Optional background auto-sync for decks, and self-update for the add-on.

**Maintaining a deck of your own?** The add-on ships with no deck content; it syncs whatever source you point it at. Host a small `manifest.json` next to your `.apkg` files, in a GitHub repo or a shared folder, and everyone studying from your deck gets the same one-click updates. See [Using this for your own decks](#using-this-for-your-own-decks).

See `CHANGELOG.md` for what changed in each version.

## Install

1. Download `internpearls.ankiaddon` from the [latest release](https://github.com/LTimothy/internpearls-anki/releases/latest).
2. In Anki, go to Tools > Add-ons > Install from file, pick the file, and restart Anki.

After restarting, an "Intern Pearls" menu appears in the menu bar between Tools and Help. Two primary actions sit at the top (Update my decks, Manage decks); occasional tools live under Advanced (including Sync decks and Reconcile my decks on their own, for anyone who wants just one half); features that are new or still settling live under Experimental; Settings and About sit at the bottom.

No deck source yet? Open Manage decks → Configure source and pick "Try the example deck" — it points the add-on at a small public demo repo so you can watch a sync work end to end, then swap in your own source later.

## Menu reference

### Update my decks

The main button, and the only one most people ever need. It fetches `manifest.json` from your configured deck source and figures out everything pending in one pass: which decks changed, which retired cards are still lingering in your collection, and which cards a deck reorg needs to relocate — the same two kinds of housekeeping "Reconcile my decks" handles on its own (see the Advanced entry below). For any changed deck, it downloads and matches it against your collection before showing you anything, so the confirmation lists real per-deck counts ("12 kept · 3 new", plus a "changed" count too when a deck has cards whose content was rewritten upstream), not just how big the deck is. A real progress bar with a working Cancel button covers this step (and the later apply step), since it's a live download per deck and a multi-deck check on a slow connection would otherwise look like a frozen add-on with no way out. Cancel is answered during a download too, not only in the gap between two decks, so a single big deck on a slow link can still be backed out of; it stops the transfer, never a half-finished import. One confirmation covers all of it — changed decks with their real counts, retired cards, and relocations, and it's explicitly a preview, nothing applies until you click Update — so you know the full scope before anything happens. Cancelling the apply step partway through is safe: whatever decks already finished stay applied, including a look change you ticked the box for, and archiving/relocating is skipped for that run rather than run against a partial sync.

A card already in your collection can come back with its content rewritten upstream, not only a card you don't have yet, and the confirmation shows both, as rows in one list right on the confirmation itself, not behind a button. Every pending card carries a small chip naming what it is: NEW for a card you don't have yet, UPDATED for one whose content changed upstream (with what each changed field currently says in your collection shown right under it), RETIRED for a card being archived because a split, reword, or removal upstream has replaced it, and MOVED for a card whose deck was reorganized and is being relocated to match. Click a NEW or UPDATED row to open it and read the whole card; RETIRED and MOVED rows are a single line each, since there's nothing more to read out of the collection for either kind. The list builds itself in batches as you scroll, so it opens instantly whether a handful of cards are pending or thousands, and your review history stays on every card either way.

Every NEW and UPDATED row also carries its own decision control, next to its header, set to the answer that costs nothing by default. A NEW row gets Import / Skip / Never; an UPDATED row gets Apply / Keep yours. Choosing Skip or Keep yours opens a small feedback box under that card so you can note why, entirely optional; every other row still carries a quiet "Add note" link at the end of the card once you open it, so writing something down is never tied to declining a card. Choosing Never collapses the row to a single struck-through line reading "won't be offered again" and drops the card out of this and every future import; nothing you choose here ever deletes a card already in your collection, and a decline can always be undone later (see Manage decks below). A card you skipped or kept comes back the next time that deck changes, pre-set to the same choice and wearing a single SKIPPED or KEPT YOURS chip in place of its usual one, with a short "changed since you skipped/kept it" hint under the header if the upstream content has moved since your last active decision on it; leaving the card untouched keeps that hint showing, and it only clears once you actively decline it again (Skip or Keep yours once more), which refreshes what's on file to match the current content. Cards you said Never to don't show up as rows again at all; the run just reports how many are being held back and where to bring one back.

A deck source can also ship a short note with a changed or newly added card, saying why it changed. When that note is reviewer feedback it appears quoted under the card's header, marked "from feedback", so you can see what a rewrite is responding to before choosing Apply or Keep yours; a maintainer's own note appears the same way, unquoted. Either kind shows only for the exact content it describes, and on a newly added card only in a deck you already have, since a first sync stays quiet rather than captioning every card with history it doesn't have yet. RETIRED and MOVED rows never carry one, since there's no content left to describe.

A changed card offers Apply, Keep yours and Never. Keep yours sets that one change aside and the card is offered again the next time the deck changes; Never keeps your version and stops offering changes to that card at all. Neither touches the card you already have, and Never here is reversible from Manage decks → Declined cards, where it is listed apart from cards you never imported.

Skip, Keep yours and Never each open a note box on the click, since those are the decisions worth a word of explanation; Import and Apply leave it shut behind the quiet "Add note" link inside the card's body. Nothing is transmitted either way: notes are collected into a digest at the end of the run for you to copy and send yourself.

A deck source can also ship a short label saying where a card came from, such as a question number in the bank the card was written from. It sits just above any note, on both newly added and changed rows, and it is shown exactly as the deck source wrote it. Unlike a note it says nothing about why the card changed, so it appears whenever the deck ships one.

Whatever you type in a feedback box is saved to disk as you type it, not just when the box closes. The digest comes at the end of a run, after an import that can fail, and your notes are the one part of a run that clicking Update again cannot reproduce, so anything still unsent is picked up automatically by your next update and included there. Nothing to remember, and no recovery prompt to click through.

In this list, a fill-in-the-blank card shows its blanks filled in rather than hidden, since the point is to check the fact is right. When one field holds more than one group of blanks, meaning it generates more than one card, each blank carries its group number as a small superscript (c1, c2), so you can see which blanks belong to the same card. A field with only one group is left unlabelled. A blank written with a hint shows that hint in brackets beside its answer, since the hint is what you read while the blank is still hidden, and a change that only moves a hint is named as one rather than reported as blanks moving. A picture on a card is named rather than shown while its row is collapsed; opening the row extracts it from the deck file already downloaded and renders it in place, so a long list of pending cards stays cheap to scroll and a card you never open costs nothing to extract.

If an update also changes how cards look, that choice is a checkbox on this same confirmation rather than a separate question part-way through the run. It is unticked by default, because applying it costs a one-time full AnkiWeb sync; leaving it alone still imports all the content and just keeps your current card appearance, and the next update carrying a look change offers it again.

An update can also change a card's *format* (a question and answer becoming a fill-in-the-blank), which costs that same one-time full sync and so needs its own yes or no. The confirmation says up front when a run includes one, and the question itself comes once, after you click Update and after the backup, covering the whole run rather than interrupting each deck as it imports. Saying no still imports those cards; they just arrive as separate new cards beside the ones you already have, leaving your progress on the old versions. Two cases are held back rather than decided for you: a deck whose download failed both before the preview and again before that question is left pending instead of imported with a format change nothing asked about, and a format your collection has no note type for yet (an all-basic collection meeting its source's first fill-in-the-blank card) imports as usual but is left pending too, since the import itself is what adds that note type and the next run is the first one that can actually move your cards across. Either way the run says which deck and why, and nothing is recorded as up to date until it really is. Declined cards are never part of this: a card you skipped, kept, or said Never to is dropped from the import, so its format change is not offered and not counted.

Every question here whose two answers cost different things names them on its buttons rather than reading Yes/No, and the answer that costs nothing is the default one: pressing Return, pressing Escape, or closing the window all mean that answer, so nothing that changes your collection's schema can be agreed to without a deliberate click on the button that says so.

On confirm, content updates apply first, then retired cards archive and reorganized cards relocate — in that order, so a retired card's replacement is already in your collection before the old card gets archived out, instead of you having to remember to run a sync first. Nothing already downloaded for the preview is fetched again, and a deck the preview could not download (its row reads "couldn't preview · still imports") is fetched again right after you confirm, before anything is backed up or imported, so a passing hiccup while checking doesn't cost you that deck's update and doesn't cost you the say over what is in it either: a format change in such a deck joins the one question below, and a look change in it is asked about outright once the run has finished, since the checkbox above never named it. For each changed deck it:

1. Takes a fresh, timestamped backup first (a self-contained `.apkg` with scheduling included, saved internally, keeping the most recent 10 of each deck it backs up). It covers the decks the run is actually about to change: normally that is just the configured `export_deck`, and a run reaching outside it gets one backup per top-level deck involved instead. Nothing else runs until this succeeds, or you explicitly choose to continue without one.
2. Adds any missing fields to the note type (never removes or renames existing fields).
3. Snapshots your `protected_fields` on every card in scope.
4. Matches each incoming card to your existing card — by GUID first, then by front text, then via the `front_aliases` map in the manifest for a card whose front was reworded since your last sync — so review history carries over.
5. Imports through Anki's built-in importer with scheduling disabled, so your intervals and ease factors stay put.
6. Restores the preserved fields from the snapshot.

That look change is a card template or its CSS, the one thing these imports deliberately never touch (see "How history is preserved"), which is why applying it is a separate consent at all: Anki treats it as a schema change, so your next AnkiWeb sync becomes a one-time full sync ("Upload to AnkiWeb"). Sync decks, the content-only half under Advanced, still asks about it in its own dialog afterward rather than up front.

When the run ends, the summary of what happened and (if you flagged, skipped, kept, or never-imported any cards) a copyable digest of those decisions and any notes you wrote arrive together in one dialog rather than one after the other.

If no deck source is configured, it tells you to open Manage decks and use Configure source. If nothing at all is pending, it just says you're up to date. If the source can't be used, the message says which of the two problems it is and gives the advice that fits: a host that never answered (no connection, a name that doesn't resolve, a timeout) points you at your connection, while a source that did answer (a bad token, a wrong repo or branch, a folder with no `manifest.json`, a `manifest.json` that isn't valid) points you at Change source.

### Manage decks

A panel listing every deck the source offers, each with a checkbox, its card count, and the same chip the sync confirmations use for that fact: NEW for a deck you have none of yet, UPDATED for one with a content update waiting. A deck that's already up to date takes no chip and says so in quiet muted text instead. Unchecking a deck stops future syncs for it; cards already imported stay in your collection until you delete them yourself in Anki, and Reconcile my decks leaves them alone too, so nothing archives or relocates a card in a deck you have opted out of. The same panel edits `protected_fields`. Save keeps the choices for your next update; Save and update now also runs Update my decks right away. What's actually pending — real per-deck kept/new counts, retired cards, relocations — is Update my decks' own confirmation, not something this panel previews separately.

A "Declined cards (N)" button opens a dialog listing every card you've skipped, kept your version of, or said Never to, grouped under Never imported / Skipped for now / Kept yours (an "Other" group catches anything in an unrecognized state). Each entry shows its front, deck, and when you decided, with an Offer again button that forgets the decision and marks its deck as changed again, so your next Update my decks re-offers that card. Nothing about the card itself is touched; a kept card you already have stays exactly as you left it. With nothing declined, the dialog just says "You haven't declined any cards."

Deck-source configuration lives here too, behind a button next to the "Source" line at the top: "Configure source" if nothing is set up yet, "Change source" once something is. It opens the same dialog either way: the three sources one under another, each with a line explaining what it is, and Cancel below them. The example deck is listed first and marked as the recommended start, since it's the one you can pick before you have any decks of your own.

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
| `export_deck` | The deck that Backup/Restore/Export intern pearls deck and the automatic pre-sync backup operate on (default: `Intern Pearls::Intern Custom`). |
| `auto_sync_decks`, `auto_sync_interval_minutes`, `notify_addon_updates`, `auto_update_addon` | Sync and update automation, see Settings below and `config.md` for details on each. |
| `dim_images_night_mode`, `dim_images_night_mode_percent` | Dim bright pictures while Anki itself is in Night Mode, and by how much (a percentage, 0-90). Applies to every deck in your collection, not just this add-on's. Also editable from Experimental > Night mode dimming, see below. |

### Experimental submenu

Features that are new or still settling, tucked away from the top level and from Advanced's own established groups: **Generate Cards (AI)**, **AI Backends**, and **Night mode dimming**.

#### Generate Cards (AI)

Drafts new cards from source material you paste in or attach, through an AI coding-assistant CLI you already have installed and signed into on your own machine. There is no API key field anywhere in the add-on, and no credential of any kind is ever read, sent, or stored by it: the add-on only shells out to a CLI you set up yourself, the same way you'd run it from a terminal.

Which backends are enabled, which one is preferred, where each CLI lives, and its default model and effort all live in one place: Experimental > AI Backends, also reachable from the wizard's own setup page (a "Configure AI Backends" button) and from a "Change" link on the input page once a backend's been picked. Each backend gets its own box: a "Use this assistant" checkbox, a "Preferred" radio, an executable path field (blank auto-detects; Browse picks one by hand), its Model/Effort controls, and a "Test connection" button, plus a status line naming its own three-state check (installed and working, found, but not responding, or not found). That status is a cheap, free `--version` call, so "installed and working" only means the binary runs; it is not proof you're actually signed in. A disabled backend, or one not found yet, has its "Test connection" button disabled entirely, since there's no binary to run a real prompt through. Test connection itself runs one trivial, real prompt and reports only whether it truly answers, "working" or "not working," with a readable message (never the CLI's raw error output) either way; that costs an actual (tiny) model call, so it only ever runs when you click it, never automatically. A Re-check button re-runs every enabled backend's status line, without touching one mid-test.

- **Claude Code** (Claude Pro or Max): tools fully restricted. The strongest of the three.
- **Codex CLI** (ChatGPT Plus or Pro): sandboxed read-only, no writes or network. Still worth knowing plainly: read-only means it can open files on your machine and fold what it reads into the cards it drafts, which is not the same guarantee as "can't touch your data."
- **Antigravity CLI** (a free, throttled Google account tier): no dedicated sandbox flag of its own yet, so this one relies on the assistant's own approval defaults rather than anything the add-on enforces.

These three are not equally sandboxed, and AI Backends says so in those words rather than averaging them into one reassurance. Pick the one whose restrictions you're comfortable running source material through. Each backend's box also says whether it can see attached images directly or only read an attached PDF as text.

Paste or type source material (lecture notes, an article excerpt, a topic outline), optionally attach images or PDFs, add free-text instructions, and choose a quality mode: **Thorough** (roughly 1 to 3 minutes) drafts, then self-reviews before handing cards back; **Quick draft** (roughly 15 to 30 seconds) is a single pass with no self-review. What that means for actually reaching the web is not the same claim on every backend, and the input page's mode hints spell out the real one for whichever backend you've picked, not a shared line:

- **Claude Code** is the one backend the add-on itself restricts by mode. Quick gets exactly one turn and no tools beyond reading the scratch copy of files you attach (so it can view them), still no web access at all. Thorough gets up to 15 turns and web-search tools, so it can genuinely check a fact against something it finds online.
- **Codex CLI** always runs sandboxed read-only, in both modes. There's no network either way, so Thorough's fact-check step can only recheck against files already on your machine, never anything online: only the prompt's stated workflow (draft, verify, self-review vs. one pass) differs by mode, and there's no turn cap in either.
- **Antigravity CLI** doesn't have its tools or turns restricted by mode at all here; both modes run under its own approval defaults, which may include web access even under Quick draft. Only the prompt's stated workflow differs by mode.

Each backend's box in AI Backends also carries a Model field and, for Claude Code, an Effort combo. Claude Code defaults to `sonnet` at `medium` effort in both quality modes: without an explicit model, it would run the signed-in account's own default, the top model for a Max subscriber, which burns credits fast across Thorough mode's up-to-15-turn loop, and `sonnet`/`medium` stays smart enough for drafting cards without that cost; a hand-edited effort value the CLI wouldn't recognize falls back to `medium` too, rather than reaching `claude --effort` and dying with an opaque CLI error. Both fields are editable and persist to config **per backend** as you change them, so a model set while Claude Code is active never pre-fills, or gets sent for, another backend. Codex CLI's Model field is honored too, passed as `--model` only when set and only when the installed CLI's help documents that flag (probed against both `codex --help` and `codex exec --help`, since a subcommand's own options often live only under its own help), so an older Codex CLI without the flag isn't hard-broken by receiving it anyway; this probe is not verified against a real Codex CLI binary, since none is installed in this add-on's own dev environment. Antigravity CLI's Model field is read-only text, since headless mode has no verified way to honor a model choice at all yet (its own default is already the cheap tier). The wizard's input page, once a backend's picked, only shows a one-line summary of all this ("Backend: Claude Code, sonnet, medium effort") and a Change link back into AI Backends: nothing about enabling, preferring, pathing, modeling, or testing a backend lives on the wizard itself anymore.

Pick which note types are in play (Study Deck - Basic, Study Deck - Cloze; Study Deck - Image ID isn't offered here at all, since its whole field is an image and a generated card has no way to fill that field directly) and a target card count, then Generate. A progress screen shows the current phase and elapsed time, with a Cancel that actually stops the run. Once you've run a mode a few times, elapsed time is joined by a learned estimate: the median of your last 10 runs in that mode, e.g. "Elapsed 48s · your recent Thorough runs averaged 1m 40s". That's specific to your own machine and material, so there's nothing to show on your first run in a mode; no estimate is ever invented or guessed.

Drafted cards come back into a review list: each one flags with a mechanical check (a likely duplicate against your existing collection, invalid cloze syntax, an overlong answer, an image that failed to resolve) before you decide anything, checked or unchecked by default accordingly. Edit any field by hand, leave a note on a card, and Revise all sends the whole set back for one more pass carrying your notes and any free-text feedback: cards you didn't note come back untouched ("keep verbatim"), and the list shows what actually changed. Import writes only the boxes you left checked.

**No card here ever carries an AI-generated raster image.** An image on a drafted card can only come from three places: a real web source the assistant found and cites, an image file you attached directly, or SVG markup the model draws itself (checked for script content before it's used). Nothing here calls an image-generation model or fabricates a picture of anything. An attached PDF's *text* is always extracted; its embedded images generally are not, since decoding them needs a library (Pillow) Anki's own bundled Python doesn't carry: if that's the case for an attached PDF, the wizard says so once, and the fix is to attach any wanted figures as their own image files instead.

A card carrying any image starts **unchecked**, whatever its mechanical checks say, because you haven't actually seen the picture yet. Every image is resolved right here in the review list, in the background (so the dialog never freezes waiting on a slow download): the row shows a real thumbnail once it's in, and for a web image, the source URL's host, so you can see where the model's suggestion actually points before it ever reaches your collection. A download that fails shows as a check on that card instead of a popup, same as any other mechanical check. Whatever review resolved is exactly what Import writes, so importing never re-downloads or re-reads anything.

That download is worth being explicit about: for a web image, it's this add-on itself opening a real HTTPS connection to the host the model named, not the backend CLI. It happens automatically, the moment a draft carrying a web image reaches this review list, in every mode, Quick draft included, despite Quick's "no web access" description above (that description is about what the assistant can reach, not about this add-on's own review-time fetch). The host is always shown on the card's row precisely so you can see what was contacted before you decide whether to keep the card.

Cards land in a `Generated` subdeck under your configured deck (`export_deck::Generated`) and carry a `Generated` tag under your scope tag. Every one gets a fresh local GUID that never resembles a synced card's GUID, so a deck source's own sync and reconcile machinery can never match, retire, or overwrite one: a shared-deck update can add or retire the maintainer's cards, but it will never touch what you generated yourself. Importing is one undoable step (Ctrl+Z reverts it), same as any other add to the collection.

What's stored between sessions is deliberately small: which backend and CLI path you last used, the deck-skill consent record described below (only ever written after you say yes), a rolling per-backend usage log of when a run happened and roughly how many tokens it used (kept for 7 days to show "Today via this add-on: N runs, ~Xk tokens"), and the last 10 run durations per backend and mode (used only for the learned time estimate on the progress screen above). Everything else, including the source material you pasted or attached, drafts, your revision notes and feedback, and the prompts and replies exchanged with the CLI, lives only in memory for that one dialog session and is discarded (including the temp folder holding extracted attachment images and every image downloaded for review) the moment it closes, whether by Import, Cancel, or closing the window. Closing mid-review with drafted cards on screen asks you to confirm first, since that's the only point anything unsaved would be lost.

A View skills link shows exactly what's sent to the assistant on top of your material: the bundled InternPearls authoring skill (card-craft rules that ship with the add-on), plus a deck-specific skill if your configured deck source offers one and you've consented to it. Nothing from a deck skill is ever sent without that explicit consent, and the same link lets you enable or disable one you've already consented to.

#### Night mode dimming

Softens the glare of a white-background image while Anki itself is in Night Mode; it never applies in Day mode. Applies to every deck in your collection, not just the ones this add-on manages, since the rule is appended to every card Anki renders rather than to a note type. Two controls:

- **Dim bright images in Night Mode**, off by default.
- **Dim by N%**, default 30, range 0-90 (higher dims more; 0 leaves images unchanged). Only takes effect while the toggle above is on.

The config is read each time a card is shown, so changing either control takes effect on the very next card, no restart needed.

### Advanced submenu

Occasional tools, tucked away from the two primary actions at the top, in four groups: acting on the deck source (including the two halves Update my decks normally runs together, for anyone who wants just one of them on its own), repairing the collection itself, the two backup/restore pairs (deck-scoped, then whole-collection), and the add-on's own update check.

**Sync decks** is the content-only half of Update my decks: it fetches `manifest.json`, compares each deck's version hash against what you last synced, and imports only the decks that changed, with the same backup, GUID matching, and field preservation described above. Its confirmation is a shorter one, though: it lists one row per deck with the manifest's own card count and a NEW or UPDATED chip, and no card list at all, because nothing has been downloaded at that point. Update my decks is where you read the individual cards before agreeing to them. It just doesn't also archive retired cards or relocate reorganized ones — use Update my decks for that in one pass, or Reconcile my decks below to run just that half.

**Reconcile my decks** is the archive/relocate half of Update my decks, runnable on its own. It does two kinds of housekeeping a plain content sync can't, both driven by ledgers the deck source ships in its manifest:

- *Retired cards.* When a deck splits one bulky card into several focused ones, or reword-replaces a card, the old version's identity changes — so a sync adds the new cards but never removes your copy of the old one, and it lingers as a duplicate in your reviews. Reconcile finds each retired card you still have, copies any personal text you've written in a protected field (like Notes) onto its replacement(s) — but only if the replacement's field is still blank, so it never overwrites something you've already written there — then moves the old card to an `…::Retired` subdeck, suspends it, and tags it `…::retired`. **It never deletes anything**: your review history is kept, and you can bring any card back by unsuspending it or moving it out of the Retired deck. Run on its own (not via Update my decks, which always syncs content first), it warns if some replacements aren't in your collection yet and suggests running Sync decks first.
- *Reorganized decks.* When the deck source moves a card into a different deck without changing its identity (a topic getting its own deck, say), a normal sync updates the card's content in place but never relocates it — only a brand-new card gets filed into the deck the source specifies. Reconcile finds any card still sitting exactly where the source last put it and moves it to match. If you've since filed that card into a deck of your own choosing, it's left alone — Reconcile only ever follows a card that's still where the source's last known location was, never overrides your own organization.

Both are schema-neutral (no forced full AnkiWeb sync) and trivially reversible by hand. A backup is taken automatically first, and re-running it is a no-op on anything already handled. If you've turned on background auto-sync (see Settings below), which only ever applies deck content on its own, a pending backlog of retired, reworded, and relocated cards shows up right on this menu item itself — "Reconcile my decks (3 pending)" — with a one-time tooltip when it first appears or grows, so a backlog auto-sync can't clear by itself never piles up unnoticed.

**Import single deck (manual)** picks one `.apkg` outside your configured source, for a deck someone sent you directly or a build you're testing before pushing it live. It runs the same personalization, automatic backup, and note restore as a sync, just for the one file you choose. A file that changes a card's format is offered the same yes or no a sync offers, in the same words and at the same point (after the backup), so the counts on the confirmation are counts it can keep; if your collection has no note type for the new format yet, it says so there rather than asking, since there would be nothing to move your cards onto.

**Clean up duplicate cards** finds notes that share a note type and front text but carry different GUIDs, most often left over from a deck reorg where a sync couldn't match an incoming card to your existing one and imported it fresh instead. For each such group it keeps the copy with the most reviews (ties prefer whichever copy already sits under the deck source's current canonical deck), and archives the rest using the same never-delete machinery as Reconcile my decks: any personal notes carry over to the kept copy first, then the losing copies are suspended, moved to the Retired deck, and tagged. Nothing is deleted, and a backup runs automatically before anything changes.

**Remove empty cards** clears out cards that have nothing left to show. When a deck source rewrites a fill-in-the-blank card to use fewer blanks, an import updates the card's text but never removes the cards that were generated for the blanks that are now gone, so those come up in review reading "No cloze 3 found on card". This is Anki's own Tools > Empty Cards narrowed to your deck: it asks Anki for the same report, keeps only the notes under your configured scope tag, and lists every card it proposes to remove, with the blank numbers that went missing, before anything happens. Other people's decks are left alone.

This is the one place the add-on deletes rather than archives, because there is nothing in an empty card to keep: the note holds every field, and the dead card renders as an error message. Three guards make that safe. A note is never left with zero cards, so no note can be deleted along with its cards; any note whose cards are *all* empty is reported and skipped rather than touched, since that means something is wrong with the card itself rather than there being a leftover to tidy; and the report is taken again after you confirm, so a card that got its content back while the list was open (a sync landing in the background, say) is left alone rather than removed for having been empty a moment earlier. A backup is taken automatically first, as with every other action here.

**Fix note types** scans the note types this add-on manages (Study Deck - Basic, Study Deck - Cloze, Study Deck - Image ID) and adds any fields they are missing. It never removes or renames fields, and it does not touch cards or scheduling. Every sync runs this before every import.

**Backup intern pearls deck** is the manual, on-demand version of the automatic pre-sync backup: a fresh `.apkg` of just the configured deck (`export_deck`), with scheduling included, saved internally, with the most recent 10 of them kept. Use it right before poking at cards yourself outside the add-on.

**Restore intern pearls deck** brings a previous deck backup or export back in. The file picker defaults to the internal backups folder, but you can browse to any matching `.apkg`. Since the file's own GUIDs already came from a real collection, this is a plain import with scheduling restored, matching cards update in place and anything missing is added as new; no personalization step is needed the way Sync and Import single deck need it for a spec-authored deck from someone else's collection. Whatever deck this rolls back is re-offered on your next Update my decks (see "How history is preserved").

**Export intern pearls deck** writes a standalone `.apkg` of just the configured deck, with your review history, deck options, and media all included, the same result as Anki's own File > Export > Anki Deck Package with every checkbox checked. This is the same export the automatic backup and Backup intern pearls deck use, just prompting you for where to save it, meant to be kept or shared on its own rather than used purely to undo a sync.

**Backup full collection** takes a full, whole-collection backup on demand, the same kind that used to run automatically before every sync. Use this for broader protection than the deck-scoped default covers. Retention for these is whatever Anki's own preferences specify, not this add-on's 10-backup limit, which only applies to the deck-scoped backups above.

**Restore full collection** opens Anki's own backup picker (the same one under File > Switch Profile > Open Backup) pointed at your backups folder, so you can revert a full collection backup if something looks wrong. This replaces your entire collection, every deck, not just the ones this add-on manages, since that's what a real collection backup contains. Anki asks you to confirm the specific backup file before doing anything. Every deck this add-on manages is re-offered on your next Update my decks (see "How history is preserved").

**Check for add-on updates** compares your installed version against the public repo's `version.json`. If a newer version exists, it offers to download and install the `.ankiaddon`. You still need to restart Anki afterward. This is the on-demand version of what the Settings toggles below do on their own, which is why most people never need it: it's here as a fallback, not the primary way to stay current.

### Settings

Sync automation and add-on update behavior, kept separate from Manage decks since those answer a different question ("which decks, which fields, from where" versus "how automatic, how often"):

- **Sync decks automatically when updates are available**, off by default. When on, the add-on checks the source in the background on the interval below and applies any changed decks without asking, backing up first the same as a manual sync. The one thing it never applies unattended is a change that would force a one-time full AnkiWeb sync without anyone consenting to it, which means a card-template change or a note-type format change (a question and answer becoming a fill-in-the-blank): a deck update that includes either is held back and stays pending, and a tooltip naming which kind it is points you at Sync decks to review it. That deck is then left alone for the rest of the session rather than re-downloaded and re-backed-up on every check, since only you can decide about it; a newer version of it from the source is picked up normally. It also never archives retired cards or relocates reorganized ones on its own — that stays a one-click confirm via Reconcile my decks, which the same check keeps nudged about (see the Advanced entry above) so a backlog can't pile up silently just because content sync is unattended.
- **Check every N minutes**, default 15, minimum 1, maximum a week (a value edited into `config.json` by hand is clamped to that range, since one big enough to overflow Anki's own timer used to fail on every launch). The check runs off the main thread when Anki supports it (essentially all current versions do), so it doesn't freeze Anki even at a short interval; if it can't reach the source, it fails within a few seconds and just tries again next time.
- **Notify me when a new add-on version is out**, on by default. A tooltip once per new release, no installation. The check runs once per launch rather than on a repeating timer, since a new add-on release isn't as time-sensitive as a new deck.
- **Install add-on updates automatically**, off by default. Downloads and installs a newer version as part of the same once-per-launch check, no confirmation. A restart is still needed to load it, same as installing by hand.

Night mode image dimming used to live here too; it now has its own dialog under Experimental (see above), since it's a display tweak rather than a sync-automation one.

### About

A short description of what the add-on does, a summary of your current settings (auto-sync, add-on updates, preserved fields), a reminder that no deck content ships with it, and a link to this repo.

## Updating decks

Run Intern Pearls > Update my decks, or turn on "Sync decks automatically when updates are available" in Settings so deck content applies on its own (retiring/relocating cards always stays a manual, one-click confirm — see Reconcile my decks above). Either way, only changed decks are imported, and the add-on backs up the deck automatically before touching anything, so there's no separate step to remember. For broader protection on top of that, Advanced > Backup full collection takes a whole-collection backup on demand.

## How history is preserved

Your collection ends up holding exactly the cards you said yes to. Every decline (Skip, Keep yours, Never) is checked before any import, interactive or the unattended background sync alike, and a declined card is filtered out of the downloaded package before it ever reaches your collection. A decline always wins over a match, so a card you turned away can't slip back in through a different code path: the counts on the confirmation leave it out because the import will, and a format change on a declined card is neither asked about nor applied, since converting a note whose new content is being dropped would leave you with a fill-in-the-blank card holding no blanks. Nothing here ever deletes a card already in your collection: choosing Never only stops that card from being offered again, and Keep yours is never a permanent pin. The card comes back the next time that deck changes, still defaulted to Keep yours, and Offer again under Manage decks brings it back whenever you want it, until the day you actively choose Apply instead.

Every sync and manual import starts with a fresh, timestamped backup, scoped to the decks that run is about to change (fast, self-contained, includes scheduling). In the normal case that is exactly the configured `export_deck`; a run that also changes a deck filed outside it backs up each top-level deck involved, so what gets backed up always covers what gets touched. "What gets touched" is read from where the affected cards actually sit right now rather than from where a deck source's ledger or manifest says they belong, so a card you have refiled yourself is still covered, and the archiving, relocating, and reworded-pair merging that Reconcile my decks and Update my decks do counts as touching just as an import does. Update my decks and an unattended background sync read that for their content updates too, by matching each downloaded deck against your collection before backing anything up; Sync decks on its own is the one exception, since it downloads each deck as it imports it and so scopes its backup from the deck names the source lists. Import single deck scopes it from the deck names inside the file you picked, and Clean up duplicates and Remove empty cards from where the cards they act on actually are. Backups are kept per deck, ten of each, so a run covering several decks can't push another deck's history out; each deck's own files are told apart by a short hash of its name, so two decks whose names differ only in characters a filename can't hold still keep separate backups rather than overwriting each other. A full, whole-collection backup is still one click away under Advanced > Backup full collection for broader protection; it's just no longer the automatic default, since most syncs only ever need to undo changes to these decks. If a backup can't be created for some reason, you're asked whether to proceed anyway rather than being blocked or silently continuing (an automatic background sync skips that round instead of asking, since there's no one there to answer). You're asked the same way if your cards are tagged but sit in no deck the add-on can export on its own, since a confirmation that promised a backup shouldn't quietly go without one. On someone's very first sync, before any of it is in your collection, there's nothing to back up and this step is skipped entirely.

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

As of v0.32.0, restoring a backup clears the relevant part of that record too, so a rollback is re-offered on your next check instead of the add-on reporting you're up to date over stale cards. Restore full collection clears it entirely, since every deck could have rolled back. Restore intern pearls deck only clears the decks actually in the file you're importing (falling back to clearing all of them if the file can't be read), so restoring one deck's backup doesn't force a recheck of every other deck too. Either way, the next Update my decks re-offers whatever came back, and the re-import still matches by GUID, so review history carries over as always.

A card you generated yourself (Generate cards with AI) sits outside all of this on purpose. It gets a fresh local GUID that a deck source's own GUIDs never use, so the matching ladder above can never pair it with anything a sync ships: it is never overwritten by an update, never archived by Reconcile my decks, and never counted as a duplicate of a maintainer's card. A deck source can add, retire, or reorganize its own cards freely and a card you generated stays exactly where you put it.

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
  persistent state under `user_files/`: `installed.json` (synced deck versions),
  `card_feedback.json` (unsent card notes), `shipped_fields.json` (last-shipped
  protected-field values), `state.json` (add-on update nag bookkeeping), and
  `declined.json`, the declined-card registry:
  `{guid: {state, front, deck, decided, hash}}`.
- `internpearls/ui.py` — the `_info`/`_warn`/`_ask`/`_prompt` dialog wrappers, the
  `_safe`/`_bg_safe` error decorators, and shared label/button styling helpers.
- `internpearls/net.py` — HTTP and GitHub contents-API fetches, the timeout policy,
  and `TransportError` for a host that was never reached, which is what lets a caller
  tell "couldn't reach the source" from "reached it and it can't be used".
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
- `internpearls/dialogs.py` — Manage decks, Settings, Night mode dimming, About, and source configuration.
- `internpearls/review.py`: card row rendering shared by the update screen and the
  end-of-run summary, and the feedback digest they produce. Kept out of `dialogs.py`
  because that module imports `sync.py`, and this is built from `sync.py`'s update
  flow, which would make the import circular.
- `internpearls/widgets.py`: the shared chip, section heading, single-line row, and
  `StreamingList` (a scroll area that builds its rows in batches instead of all at
  once). The update screen, the end-of-run summary, and `review.py`'s card rows all
  build from this module, so the two ends of a run look alike because they share the
  same components.

The two checks that run on their own (the add-on-update check and the deck auto-sync
poll) dispatch their network work through `_run_in_background()`, which uses Anki's
`QueryOp` to run off the main thread when it's available, falling back to running
inline if not. Only the part that actually touches `mw.col` (backing up and importing,
which only happens when something changed) runs on the main thread; that matches the
cost a manual Sync decks click already pays, so it isn't the part that needed fixing.

### Running tests

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -v
```

The dialogs also have a render suite that asserts on real painted pixels
(`qt_tests/`, run separately because real and mock Qt cannot share a process). See
`CONTRIBUTING.md`. `tools/render_dialog.py` renders the same scenes to a PNG when you
want to look at one rather than assert on it.

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
python3 -m pip install PyQt6
python3 tools/render_dialog.py --list
python3 tools/render_dialog.py confirm --expand 1 --feedback --out confirm.png
python3 tools/render_dialog.py confirm --dark          # see "Colors" below
```

It reuses `tests/mock_anki.py` for the whole fake Anki world and swaps only that
harness's fake Qt for real PyQt6, so the two are opposites rather than duplicates:
mock Qt for structure in CI, real Qt for pixels locally. It's a developer tool and
isn't packaged into the `.ankiaddon`. Its default card content is synthetic; `--apkg`
reads a real deck through the add-on's own `apkg_note_details`.

Reach for it whenever a change involves a stylesheet, a border, spacing, or a color.

### Colors

Colors live in `internpearls/palette.py`, in two sets, light and dark, chosen from
Anki's own `theme_manager.night_mode` when a dialog builds. There is no theme-neutral
value: measured against Anki's own window colors, no single mid-tone clears WCAG AA on
both a light and a dark background, so a single set always leaves one theme unreadable.
`tests/test_palette.py` checks every value arithmetically.

That still leaves one rule that's easy to miss: **if you hardcode a background,
hardcode the foreground with it, from the same set.** Text color otherwise comes from
the platform palette, which flips with the theme while your background doesn't, so a
light block ends up with white text on it in dark mode. A color-only style is safe; a
background-only style is not.

Better still, when a block just needs to look sunken rather than to carry a specific
brand color: reference the palette from the stylesheet itself, `background:
palette(base); color: palette(text); border: 1px solid palette(mid)`. Qt resolves those
per theme on its own, so there's no set to pick and no pair that can drift apart the way
a hardcoded background and a palette foreground did twice. Two guards back the
hardcoded path, and both were added only after each had already missed a real instance:
a source lint over every `setStyleSheet` call in the add-on (`tests/test_review.py`),
and the real-Qt contrast suite, which measures `QPlainTextEdit` as well as labels and
flat buttons (`qt_tests/test_contrast.py`).

`--dark` selects `palette.py`'s dark set, the same one a real dialog picks up under
Anki's night mode, and separately approximates Anki's own window colors through Qt's
color-scheme hint for anything that still reads from the platform palette. That second
part is an approximation, not a reproduction, so a `palette()`-based color can still
look slightly off from the real app; the `palette.py` colors themselves are exact,
since `--dark` selects the real dark set rather than testing whether one value survives
a dark background.

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
