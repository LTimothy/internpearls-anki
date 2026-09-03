## github_decks_repo

A GitHub repo to sync decks from, as `owner/name`. Leave empty to use `decks_dir`
instead. Easier to set via the Configure source / Change source button inside Intern
Pearls → Manage decks.

## github_token

A read-only, fine-grained GitHub personal access token scoped to `github_decks_repo`.
Only needed when that repo is private; leave empty for a public repo. Stored locally in
this config only; never shared or committed anywhere.

## github_ref

The branch or tag to pull the manifest and decks from. Defaults to `main`.

## decks_dir

A local folder containing `manifest.json` and the `.apkg` files, used instead of GitHub
when `github_decks_repo` is empty. (Configure source sets one and clears the other, so
they don't normally both exist.)

## scope_tag

The root tag identifying cards this add-on manages. Field snapshotting and GUID matching
are scoped to this tag (and its subtags); everything else in your collection is ignored.
Defaults to `InternPearls`.

## protected_fields

Field names to snapshot before an import and restore after, so personal annotations
survive a re-import even though Anki's importer overwrites every field on a matched
note. Defaults to `["Notes"]`; add any other field name you keep your own content in.
Editable in Intern Pearls → Manage decks (the "Preserved fields" box).

## excluded_decks

Full names of decks you've opted out of syncing, e.g.
`["Intern Pearls::Intern Custom::CA1 Handbook"]`. Decks listed here are skipped by Sync
decks and auto-sync. Easiest to manage via Intern Pearls → Manage decks (unchecking a
deck adds it here); an empty list syncs everything the source offers. Unchecking a deck
does not delete cards you already imported.

## export_deck

The deck that Export/Restore/Backup intern pearls deck (Advanced menu) and the automatic
pre-sync backup all operate on. Defaults to `Intern Pearls::Intern Custom`. Change this
if you've adapted this add-on for a different deck hierarchy.

## notify_addon_updates

Whether to check, once per Anki launch, if a newer version of this add-on is out and
show a brief tooltip if so. This alone never installs anything; "Check for add-on
updates" (or `auto_update_addon` below) stays the action that does that. Notifies at
most once per new release, so it won't repeat on every launch once you've seen the
notice. Defaults to `true`. Editable in Intern Pearls → Settings.

## auto_update_addon

When `true`, a newer add-on version installs itself as part of the same once-per-launch
check `notify_addon_updates` runs, instead of just notifying you. A restart is still
needed either way to load it. Defaults to `false`. Editable in Intern Pearls → Settings.

## auto_sync_decks

When `true`, decks sync automatically in the background: once shortly after Anki
starts, then again every `auto_sync_interval_minutes` while Anki stays open, without
asking each time. A backup is still taken first; if it fails, that round is skipped
instead of importing unprotected. Results show as a brief tooltip, never a blocking
dialog, since this can fire mid-review. The check itself runs off the main thread when
possible, so it stays quick and doesn't freeze Anki even at a short interval. Defaults
to `false`. Editable in Intern Pearls → Settings, which also restarts the running timer
immediately so a change here doesn't need an Anki restart to take effect.

## auto_sync_interval_minutes

How often the background poll checks the source when `auto_sync_decks` is on. Floored
at 1 minute regardless of what's set here, to keep a typo or a 0 from becoming a busy
loop. GitHub's request volume at that floor is still trivial: one small `manifest.json`
fetch a minute, well under even the unauthenticated 60-per-hour limit. Defaults to `15`.
Editable in Intern Pearls → Settings.

## ai_backend

Which AI backend the "Generate cards with AI" wizard should prefer when more than one
is detected and working (`claude`, `codex`, or `agy`). Empty uses whichever one is
found and working first. Defaults to `""`.

## ai_cli_path

An explicit path to each backend's own CLI binary, used instead of searching `PATH` and
the usual install locations. Stored per backend kind, as an object with `claude`,
`codex`, and `agy` keys (`{"claude": "", "codex": "", "agy": ""}`); an empty entry
searches normally for that backend. Editable from the AI Backends window's Executable
path field, per backend; that window opens from the Generate Cards (AI) wizard's own
Setup link, and has no menu item of its own. Defaults to `{"claude": "", "codex": "", "agy": ""}`.

A config written before this became per-backend stored `ai_cli_path` as a single
string, meaning "the path for the backend named in `ai_backend`"; that shape is read
once and folded into the map above (into whichever backend `ai_backend` named, or
dropped if it named none), so nobody loses a configured path on upgrade.

## ai_backend_enabled

Whether each backend is offered at all, as an object with `claude`, `codex`, and `agy`
keys (`{"claude": true, "codex": true, "agy": true}`). A backend set to `false` is
skipped entirely: not detected, not chosen, not shown as a candidate anywhere the
wizard or the AI Backends window looks. Editable from the AI Backends window's "Use
this assistant" checkbox, per backend. Defaults to `{"claude": true, "codex": true,
"agy": true}`.

## ai_model

The model to request from each backend, e.g. `sonnet` or `opus` for Claude Code, or a
full model name for Codex CLI. Stored per backend kind, as an object with `claude`,
`codex`, and `agy` keys (`{"claude": "", "codex": "", "agy": ""}`), so a value set while
one backend is active never pre-fills, or gets sent for, another: switching backends
shows and uses only that backend's own entry. Empty (the default for every key) means
"use the backend's own default": for Claude Code that default is `sonnet` (set inside
the add-on, not this file, so an empty value here still sends an explicit, cheaper
model rather than whatever the account's own default happens to be); for Codex CLI an
empty value passes no `--model` flag at all, deferring entirely to Codex's own default,
and a non-empty value is only passed when the installed Codex CLI's own help documents
the flag (see "Codex CLI's model flag" below). Antigravity CLI behaves the same way as
Codex CLI: an empty value sends no `--model` at all and lets `agy` pick its own default
(already a cheap Flash tier), and a non-empty value is passed only when `agy --help`
documents the flag. Run `agy models` to see the ids it accepts, such as
`gemini-3.8-flash-medium`. Defaults to `{"claude": "", "codex": "", "agy": ""}`.
Editable from the AI Backends window, per backend.

## ai_effort

The reasoning-effort level to request. Claude Code accepts `low`, `medium`, `high`,
`xhigh`, `max`; Antigravity CLI accepts `low`, `medium`, `high`. Codex CLI has no
verified effort flag, so the control is absent from its box in the AI Backends window
and its entry here is ignored. Stored the same per-backend shape as `ai_model` above,
for the same reason. Empty means "use the backend's own default": for Antigravity CLI
that sends no `--effort` flag at all, and a non-empty value is passed only when
`agy --help` documents the flag. For Claude Code, empty (or any value that isn't one of
its five levels, e.g. a hand-edited typo) means `medium`, chosen to stay smart enough
for card drafting without burning a Max subscription's credits the way the account's
own top-model default would across Thorough mode's up-to-15-turn loop; the AI Backends
window's Effort combo always shows this same effective value, never a typo it can't
find in its own list. Defaults to `{"claude": "", "codex": "", "agy": ""}`. Editable
from the AI Backends window, per backend.

### Codex CLI's model flag

Codex CLI documents `-m, --model` under its `exec` subcommand's own help
(`codex exec --help`), not under `codex --help`. The wizard probes for the long form,
matched as a whole flag token (not a substring another flag might contain, like
`--model-provider`), against both `codex --help` and `codex exec --help`; it's passed
only when the installed CLI's own help documents it, so an older Codex CLI without the
flag isn't hard-broken by receiving it anyway.

## ai_default_count

How many cards the "Generate cards with AI" wizard should ask for by default. `0` (the
default) means automatic: no number is sent at all, and the assistant makes one card per
point the source actually teaches, up to 40. A value from 1 to 40 pre-fills the wizard's
Advanced panel with that exact number instead. Anything else, including a negative or a
value past 40, reads as automatic. This only seeds the control; changing the number in
the wizard applies to that session and is never written back here.

## ai_default_depth

Which depth the wizard should start on: `thorough` (drafts, may verify online, then
self-reviews) or `quick` (exactly one turn, no web access). Defaults to `auto`, which
lets the material decide: thorough for a source over 1,500 characters or any attachment,
quick for a short paste. As with the count above, this only seeds the control; picking a
depth in the wizard applies to that session and is never written back here.

## dim_images_night_mode

When on, bright pictures are dimmed while Anki itself is in Night Mode, so a
white-background diagram doesn't glare out of a dark card. Never applies in Day mode.
Applies to every deck in your collection, not just this add-on's, since it works by
styling images themselves rather than one note type. Takes effect immediately, with no
Anki restart. Defaults to `false`. Editable in Intern Pearls → Experimental → Night
mode dimming.

## dim_images_night_mode_percent

How much dimmer, as a percentage, `dim_images_night_mode` makes those images: higher
dims more. Clamped to 0-90 (past that an image reads as blacked out rather than
dimmed). Defaults to `30`, the fixed dim level applied before this became
configurable, so an existing `dim_images_night_mode` setting keeps its exact look
until the percentage is changed. Editable in Intern Pearls → Experimental → Night mode
dimming.

## dim_night_mode_scope

What `dim_images_night_mode` dims. `"images"` dims bright images only, the original
behaviour. `"content"` dims everything Anki draws in a web view: cards, the deck
list, the overview, and the editor. Takes effect the next time a screen loads; never
the menu bar or dialogs. Defaults to `"images"`; an unrecognized value falls back to
it. Editable in Intern Pearls → Experimental → Night mode dimming.

## Not a config.json key: user_files/user_skill.md

Your own standing instructions for the AI wizard ("My rules"), plain text, live in
`user_files/user_skill.md`, not in this config file, so they survive an add-on update.
Editable from the wizard's input page (Edit my rules link). An empty save removes the
file entirely rather than leaving an empty one behind.
