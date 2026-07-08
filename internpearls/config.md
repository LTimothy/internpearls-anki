## github_decks_repo

A GitHub repo to sync decks from, as `owner/name`. Leave empty to use `decks_dir`
instead. Easier to set via Intern Pearls → Configure deck source in Anki itself.

## github_token

A read-only, fine-grained GitHub personal access token scoped to `github_decks_repo`.
Stored locally in this config only; never shared or committed anywhere.

## github_ref

The branch or tag to pull the manifest and decks from. Defaults to `main`.

## decks_dir

A local folder containing `manifest.json` and the `.apkg` files, used instead of GitHub
when `github_token` is empty.

## scope_tag

The root tag identifying cards this add-on manages. Notes snapshotting and GUID matching
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

The deck that Export/Import/Backup intern pearls deck (Advanced menu) and the automatic
pre-sync backup all operate on. Defaults to `Intern Pearls::Intern Custom`. Change this
if you've adapted this add-on for a different deck hierarchy.

## notify_addon_updates

Whether to check, once per Anki launch, if a newer version of this add-on is out and
show a brief tooltip if so. Never auto-installs anything — that's still the explicit
"Check for add-on updates" action. Notifies at most once per new release (won't repeat
on every launch once you've seen the notice). Defaults to `true`.

## auto_sync_decks

When `true`, decks sync automatically in the background — once shortly after Anki
starts, then again roughly every `auto_sync_interval_minutes` while Anki stays open —
without asking each time. A backup is still taken first; if it fails, that round is
skipped rather than importing unprotected. Results show as a brief tooltip, never a
blocking dialog, since this can fire mid-review. Defaults to `false` (off); toggle it
from Intern Pearls → Manage decks rather than editing this directly, so the running
timer picks up the change immediately.

## auto_sync_interval_minutes

How often the background poll checks the source when `auto_sync_decks` is on. Floored
at 15 minutes regardless of what's set here — GitHub's request volume at this cadence is
trivial (one small `manifest.json` fetch per interval), so there's no reason to go
lower. Defaults to `60`.
