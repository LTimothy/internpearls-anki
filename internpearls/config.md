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

An explicit path to the CLI binary for the backend named in `ai_backend`, used instead
of searching `PATH` and the usual install locations. Ignored unless `ai_backend` names
a specific backend. Defaults to `""`.

## ai_model

The model to request from whichever backend the wizard is currently using, e.g.
`sonnet` or `opus` for Claude Code, or a full model name for Codex CLI. Empty means
"use the backend's own default": for Claude Code that default is `sonnet` (set inside
the add-on, not this file, so an empty value here still sends an explicit, cheaper
model rather than whatever the account's own default happens to be); for Codex CLI an
empty value passes no `-m` flag at all, deferring entirely to Codex's own default.
Antigravity CLI has no way to honor this (see the wizard's Model field for that
backend), so it's ignored there regardless of what's set. Stored flat, not per backend:
switching backends doesn't clear it, but a value that doesn't apply to the backend now
in use is simply ignored, not carried into a request it wasn't meant for. Defaults to
`""`. Editable from the "Generate cards with AI" wizard's backend row.

## ai_effort

The reasoning-effort level to request, one of `low`, `medium`, `high`, `xhigh`, `max`.
Only Claude Code has a verified `--effort` flag, so this only ever affects that backend;
it's hidden from the wizard and ignored entirely for Codex CLI and Antigravity CLI.
Empty means "use the backend's own default", which for Claude Code is `medium`, chosen
to stay smart enough for card drafting without burning a Max subscription's credits the
way the account's own top-model default would across Thorough mode's up-to-15-turn
loop. Defaults to `""`. Editable from the "Generate cards with AI" wizard's backend row.

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
