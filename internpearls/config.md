Where a setting has a place in the Intern Pearls menu, it is named below.

## github_decks_repo

The GitHub repo to sync decks from, as `owner/name`. Leave empty to use `decks_dir`.
Set it with Manage decks > Configure source.

## github_token

A read-only, fine-grained GitHub token for `github_decks_repo`, needed only if that repo
is private. It stays in this config and is sent only to GitHub.

## github_ref

The branch or tag to sync from. Defaults to `main`.

## decks_dir

A local folder with `manifest.json` and the `.apkg` files, used when
`github_decks_repo` is empty. Configure source sets one and clears the other.

## scope_tag

The root tag of the cards this add-on manages. Field preservation and card matching
look only at this tag and its subtags. Defaults to `InternPearls`.

## protected_fields

Fields kept through every import, so your own notes survive. Defaults to `["Notes"]`.
Edit in Manage decks (Preserved fields).

## excluded_decks

Full names of decks you have opted out of, such as
`["Intern Pearls::Intern Custom::Example Deck"]`. Sync and auto-sync skip them; cards
already imported stay. Unchecking a deck in Manage decks adds it here.

## export_deck

The deck that Advanced > Backup, Restore and Export intern pearls deck use, and that is
backed up before each sync. Defaults to `Intern Pearls::Intern Custom`.

## notify_addon_updates

Check once per Anki launch for a newer add-on version and show a short notice, once per
release. Installs nothing. Defaults to `true`. Edit in Settings.

## auto_update_addon

Install a newer add-on version during that same launch check instead of only notifying.
Restart Anki to load it. Defaults to `false`. Edit in Settings.

## auto_sync_decks

Sync decks in the background shortly after a profile opens and then every
`auto_sync_interval_minutes`. Each round backs up first and is skipped if the backup
fails. Results show as a short notice, never a dialog. Defaults to `false`. Edit in
Settings; a change there applies without a restart.

## auto_sync_interval_minutes

Minutes between background checks. Values outside 1 to 10080 (one week) are clamped,
and a value that is not a number reads as `15`, the default. Each check of a GitHub source is one API request:
without a token, GitHub allows 60 an hour, so a 1-minute interval uses all of them;
with a token, 5,000. Edit in Settings.

## ai_backend

The AI tool Generate cards (AI) prefers when more than one works: `claude`, `codex` or
`agy`. Empty uses the first one that works. Defaults to `""`.

## ai_cli_path

The path to each tool's program, keyed `claude`, `codex` and `agy`. Empty searches
`PATH` and the usual install folders. Edit in the AI Backends window (the wizard's
Setup link). An older single-string value is read as the entry for `ai_backend`.

## ai_backend_enabled

Whether each tool is offered at all, keyed `claude`, `codex` and `agy`. `false` hides
it everywhere. Edit with the ignore and use again links in the AI Backends window.

## ai_model

The model to request from each tool, keyed `claude`, `codex` and `agy`. Empty uses the
tool's default, which is `sonnet` for Claude Code. For Codex and `agy`, empty sends no
`--model` flag, and a value is sent only if that tool's help lists the flag (Codex is
checked under `codex exec --help` too). `agy models` lists `agy`'s ids. Edit in the AI
Backends window.

## ai_effort

The reasoning effort for each tool, keyed the same way. Claude Code takes `low`,
`medium`, `high`, `xhigh` or `max`, and empty or unknown means `medium`. `agy` takes
`low`, `medium` or `high`, and empty sends no flag. Codex has no effort setting. Edit in
the AI Backends window.

## ai_default_count

How many cards the wizard asks for. `0`, the default, lets the assistant decide, up to
40. A value from 1 to 40 fills in that number; anything else reads as `0`. The wizard
never writes back here.

## ai_default_depth

The wizard's starting depth: `thorough` (drafts, may check facts online, then reviews
its own cards) or `quick` (one pass, no fact checks). The default, `auto`, picks
thorough for 1,500 characters or more or any attachment, quick otherwise. The wizard
never writes back here.

## dim_images_night_mode

Dim bright pictures while Anki is in Night Mode, in every deck. Applies at once.
Defaults to `false`. Edit in Experimental > Night mode dimming.

## dim_images_night_mode_percent

How much to dim, from 0 to 90 percent. Defaults to `30`. Edit in Experimental > Night
mode dimming.

## dim_night_mode_scope

`"images"` dims bright pictures only. `"content"` dims everything Anki draws in a web
view (cards, deck list, overview, editor) but not menus or dialogs. Applies when a
screen next loads. Defaults to `"images"`. Edit in Experimental > Night mode dimming.

## dupes_threshold

Scan for duplicates sensitivity: `0.6` (Strict), `0.5` (Normal) or `0.4` (Loose). Any
other value reads as `0.5`. Set by the scan's Sensitivity choice.

## dupes_excluded_decks

Deck names, or parts of names, the duplicate scan leaves out, such as
`["Archive", "Shared"]`. Defaults to `[]`. Set by the scan's Exclude decks box.

## dupes_ignored

Note pairs marked Ignore pair in the duplicate scan, so a rescan skips them. Remove an
entry to see that pair again. Defaults to `[]`.

## My rules (not in this file)

Your standing instructions for the AI wizard live in `user_files/user_skill.md`, so they
survive add-on updates. Edit them with the wizard's Add my rules or Edit my rules link.
Saving an empty box deletes the file.
