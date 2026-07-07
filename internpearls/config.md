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

## export_deck

The deck that Export/Import/Backup intern pearls deck (Advanced menu) and the automatic
pre-sync backup all operate on. Defaults to `Intern Pearls::Intern Custom`. Change this
if you've adapted this add-on for a different deck hierarchy.
