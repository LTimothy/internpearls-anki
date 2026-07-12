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
  applying one on its own.
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
- **Persistent state lives under `internpearls/user_files/`.** Everything
  else in the add-on folder is replaced on update.
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
