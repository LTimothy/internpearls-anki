# Intern Pearls Deck Tools

Anki add-on for history-safe deck syncing: matches cards by GUID, snapshots and
restores user-configured protected fields around each import, and backs up the
deck before touching anything. The README's "Menu reference", "How history is
preserved", and "For developers" sections are the behavior spec — keep them
accurate when behavior changes.

This repo is public. It must never contain card content, the name of any
private deck-content repo, or tokens. The add-on syncs whatever source the
user configures; nothing in here may assume a specific one.

## Layout

- `internpearls/logic.py` — pure Python, no `aqt`/`anki` imports: apkg
  reading/rewriting, GUID matching, version comparison, HTML formatting.
- Everything that touches `mw`, `col`, or Qt is split by concern:
  `config.py` (constants, config access, `user_files/` state), `ui.py`
  (dialog wrappers, styling helpers), `net.py` (HTTP/GitHub fetches,
  timeouts), `collection.py` (collection reads/writes and Advanced actions),
  `sync.py` (sync flows), `updates.py` (self-update), `background.py`
  (background dispatch, unattended checks), `dialogs.py` (settings/config
  dialogs), `__init__.py` (menu and startup wiring only).
- `tests/test_logic.py` — pytest over `logic.py`; no Anki install needed.
- `tests/mock_anki.py` + `tests/conftest.py` — a mock Anki deep enough to run
  the real code without an Anki install. `tests/test_sync_flows.py` drives
  sync/collection/background end to end; `tests/test_dialogs.py` drives the
  real dialogs and menu the same way.
- `docs/` — a GitHub Pages live demo that runs the add-on's actual code
  (via Pyodide) rather than reimplementing it; `docs/addon/` is a byte-exact
  mirror of `internpearls/` refreshed by `./build.sh` and enforced by
  `tests/test_demo_parity.py`.

New code goes in `logic.py` if it's testable with plain Python, otherwise in
the module matching its concern above. Add a test alongside any change:
`logic.py` changes get a unit test, flow changes extend the flow tests,
dialog changes extend the dialog tests.

## Verify before committing

```bash
pytest tests/ -v      # must pass
./build.sh             # repackages internpearls.ankiaddon and refreshes docs/addon/
```

## Hard constraints

Each of these traces back to a real bug or a deliberate trade-off — don't
relax them without understanding why they're there.

- **`merge_notetypes=False` on every import.** `True` bumps the collection
  schema on every import and forces a full AnkiWeb sync. Note types are
  reconciled idempotently instead (`_ensure_notetypes`, only touches schema
  when it genuinely adds a missing field). Template/CSS changes are detected
  separately and applied only with explicit user consent; an unattended
  auto-sync defers a template change to a manual sync rather than ever
  applying one on its own. In `update_decks` that consent is the checkbox on
  the single confirmation (detected up front from the preview's own download,
  unticked by default) rather than a question mid-import; `sync_decks` still
  asks via `_offer_template_changes`. Either way the consent is explicit and
  names the one-time full AnkiWeb sync it costs.
- **Card matching is GUID first, then front text, then a rename map**
  (`remap_cards`). A GUID match must never be overridden by a text match.
  Stable-GUID deck sources rely on this to reword card fronts without any
  alias bookkeeping.
- **Retirement/reconciliation only ever archives or relocates, never
  deletes.** A split or reworded card leaves the old version orphaned in a
  learner's collection since sync is additive; reconciliation suspends +
  moves it to a "Retired" subdeck and tags it, or relocates a card whose deck
  moved in a pure reorg — never removes anything outright, and never bumps
  the collection schema (so it never forces an unattended full sync either).
  The single carve-out is `collection.remove_empty_cards`, which deletes
  cards Anki's own empty-cards report says render nothing at all. It is a
  deliberate exception, not a precedent: an empty card holds no content to
  preserve (its note keeps every field, and the card itself shows only an
  error), and archiving one would leave a dead card in the Retired deck
  forever. It stays safe by never leaving a note with zero cards, so nothing
  it does can delete a note, and by scoping to the configured tag so other
  people's decks are untouched. Anything that isn't provably contentless
  still archives.
- **Persistent state lives under `internpearls/user_files/`.** Everything
  else in the add-on folder is replaced on update.
- **Card feedback is written to disk as it is typed, and cleared only once
  the digest has actually been shown.** It is the one thing in a run that
  clicking Update again cannot reproduce, and it used to exist only in memory
  until the digest at the very end, several dialogs and one fallible import
  later. `review.save_feedback` is debounced, not deferred to close;
  `update_decks` folds anything left on disk back into the current run rather
  than prompting about it. Do not move the clear earlier than the digest: an
  exit with nothing flagged deliberately leaves the file alone, because
  treating "no flags this run" as "safe to forget" is how a recovered note
  gets thrown away a second time.
- **Self-update fetches use an API that returns fresh data**, not a CDN path
  that can serve a stale cached response right after a release.
- **Dialogs go through shared wrappers**, not ad hoc calls, so every dialog
  carries consistent styling and title.
- **Background work** (update checks, auto-sync polling) never touches the
  collection directly from a background thread; only the main thread writes.
- **`installed_matching_collection` matches by deck-name prefix, not exact
  equality.** A deck spec's `deck_name` is routinely just the parent path —
  cards land in `deck_name::<subdeck>` for any spec with a `subdecks` list,
  which is the normal case. An exact-match version silently treated every
  subdeck-based deck as never-installed on every check, forever (shipped in
  v0.25.2, fixed in v0.26.1) — caught via the live demo constantly
  re-offering an update with nothing changed. `pytest` alone won't catch a
  regression here, since none of the mock fixtures use subdecks; before
  touching this function again, also exercise it against a real deck source
  (`docs/demo_harness.py`'s `DEMO_SOURCE` env override lets `boot()` run
  outside Pyodide against a local clone — see `docs/demo_harness.py`'s
  docstring) or add a subdeck-nested fixture to the flow tests.
- **`find_deck_moves_needed` matches a stuck card by front, not only GUID.**
  A card whose deck source changed its `id_seed` has a different GUID in a
  long-time learner's collection than the move ledger is keyed by, so a
  GUID-only match never relocates it: it stays stuck at `from` and its new
  deck is re-offered forever by `installed_matching_collection` (the same
  "self-correcting redundant re-sync" that function's docstring describes, but
  which never actually self-corrects for these cards). The deck-repo manifest
  ships each move's current front (`build_all.py`'s `_deck_moves` +
  `_spec_fronts`); the add-on falls back to front when the ledger GUID isn't in
  the collection, and emits *her* GUID so `apply_deck_moves` can act (shipped
  v0.29.1). A manifest without `front` keeps GUID-only behavior. Only bites
  seed-changed + reorg'd cards, which no mock fixture had until the two
  `..._by_front...` / `..._without_front` flow tests were added.
- **`docs/index.html`'s busy indicator yields via `setTimeout`, never
  `requestAnimationFrame`.** Every `H.start()`/`H.feed()` call blocks
  Pyodide's single JS thread end to end (it's real synchronous Python,
  including any network fetch inside it), so the page has to force a paint of
  its "Working…" state *before* making that call. `requestAnimationFrame`
  looks like the right primitive but silently never fires while the tab is
  backgrounded or otherwise not visible, which hangs the whole flow forever
  with no error — found by testing this exact page in an automated,
  non-foregrounded browser tab. `setTimeout(fn, 0)` yields a real turn of the
  event loop regardless of tab visibility and doesn't have this failure mode.
- **Review renders a card's media only from an already-downloaded `.apkg`, on
  expand.** `field_preview_html` names an image unless it is handed a
  resolver, and the resolver extracts on first expand into a per-dialog temp
  dir. Rendering eagerly would extract every picture in a deck (a deck can
  carry a couple of hundred images) to show a list most of which is never
  opened, and rendering from a path that was never extracted paints broken
  images, which is the failure the naming behavior exists to avoid.
- **A row marker is a background and foreground pair, never a bare colour.**
  Measured against the render suite's own window colours (`#efefef` light,
  `#2f2f31` dark), no single colour clears WCAG AA 4.5:1 on both themes, so a
  foreground-only marker would be unreadable on one of them. Guarded by two
  direct tests, not the general contrast suite: `qt_tests/test_contrast.py`
  measures one dominant foreground/background pair per widget, and a row's
  own body text always outcompetes a small inline pill, so it cannot see this
  case at all. `tests/test_review.py` computes WCAG directly over
  `review._MARKERS`, and `qt_tests/test_paint.py` asserts each pill's
  background colour actually appears in the render.
- **`remap_cards` owns what "matched" and "new" mean.** `find_changed_notes`
  takes its pair list rather than re-deciding, for the same reason
  `new_notes` is returned from there: a second implementation of that ladder
  eventually disagrees, and the visible symptom is a preview that lies about
  what is going to change.
- **`_run_sync`'s `on_progress` callback must return truthy to continue.** A
  False return (Cancel clicked in `cancellable_progress`) stops the loop
  *before* that deck's fetch/import, never partway through one, so whatever
  already completed is still fully persisted (installed.json, restored
  fields). Any new caller of `_run_sync` that passes `on_progress` must treat
  its 5th return value (`cancelled`) as a real branch, not an afterthought:
  `update_decks()` skips archiving/relocating entirely on cancel, since that
  step assumes every content update already landed. In `tests/mock_anki.py`,
  the mock `QProgressDialog.cancel_after` hook counts `setLabelText()` calls,
  not `setValue()` calls, on purpose, since `cancellable_progress()`'s own
  setup/teardown calls `setValue()` outside the per-step loop and would
  otherwise throw off a test's "cancel after N steps" count.

## Releases

Semver `MAJOR.MINOR.PATCH`, all three parts, bumped in lockstep: the
version constant, `version.json`, a git tag `vX.Y.Z`, and a `CHANGELOG.md`
entry — then `./build.sh` and push. The in-app update check compares these
numerically, so all must match exactly. Users need an Anki restart to load a
new version.

## Working style

- Surface assumptions and trade-offs before implementing; if two readings of
  a request exist, say so rather than picking one silently.
- Minimum code that solves the problem — no speculative config,
  abstractions, or error handling for cases that can't happen.
- Surgical diffs: don't reformat, rename, or restyle code a change doesn't
  require; match existing conventions.
- Turn tasks into verifiable goals (a failing test made to pass, a build
  that succeeds) and loop until verified.
