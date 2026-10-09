# Contributing

Bug reports, fixes and small features are welcome. For anything bigger than a
bug fix, open an issue first so we can agree on the approach.

## Setup

You need Python 3 and pytest. No Anki install is needed.

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -q
```

That suite runs against a mock Qt and checks structure: which widgets exist,
how they nest, and what a click calls.

A second suite renders the real dialogs with real PyQt6 and checks what they
paint. Run it as its own command, because the two cannot share a process (see
`qt_tests/README.md`):

```bash
python3 -m venv .venv-qt
.venv-qt/bin/pip install PyQt6 pytest
QT_QPA_PLATFORM=offscreen .venv-qt/bin/python -m pytest qt_tests/ -q
```

CI runs both on every release tag. To try a change in Anki, run `./build.sh`,
install `internpearls.ankiaddon` with Tools > Add-ons > Install from file, and
restart Anki.

## Where code goes

Code that plain Python can test goes in a module with no `aqt` or `anki`
imports: `internpearls/logic.py`, `ai_logic.py` or `dupes.py`, with a test in
`tests/`. Code that touches `mw`, `col` or Qt goes in the module for its
concern. "For developers" in the README lists them. `__init__.py` holds only
the menu and startup wiring.

## Conventions

- Dialogs go through the `_info`, `_warn`, `_ask` and `_prompt` wrappers in
  `internpearls/ui.py`, never raw `aqt.utils` calls.
- Menu items use sentence case ("Import single deck") with no trailing
  ellipsis.
- State that must survive an add-on update lives in
  `internpearls/user_files/` and nowhere else.
- Imports keep `merge_notetypes=False`. `True` forces a full AnkiWeb sync on
  every import. See "How history is preserved" in the README.
- Fetches from this repo (`version.json`, the `.ankiaddon`) go through the
  GitHub contents API (`_gh_public_raw`). raw.githubusercontent.com can serve
  stale files for minutes after a push.
- Colours live in `internpearls/palette.py`. See "Colors" in the README.
- The repo holds tooling only: no card content, and nothing tied to a
  particular deck source.

## Pull requests

- Commit only source, tests, user docs and release assets. A new root file or
  Pages file needs an explicit `.gitignore` allowlist entry.
- Keep the diff to what the fix or feature needs, and match the existing
  style.
- Every change comes with a test in the matching suite.
- Run `python3 -m pytest tests/ -q` and `./build.sh` before opening the pull
  request. After any edit under `internpearls/`, the tests fail until
  `./build.sh` refreshes the package and the demo's copy of the code.
- If you change a stylesheet, border, spacing or colour, render the dialog
  and look at it (`python3 tools/render_dialog.py --list`). The mock suite
  cannot tell whether Qt painted anything.
- Leave the version, `CHANGELOG.md` and `internpearls.ankiaddon` alone. The
  maintainer does releases, as described under "Versioning" in the README.

### Browser demo

The demo at `docs/` runs the non-AI flows that its parity tests cover. A
change to one of those flows needs:

- every new widget node kind or action kind in the shared schema, with
  boundary tests
- a real-Qt test when it depends on signals, focus, keys or layout
- a new or updated browser parity case

Run the browser suite with `python3 tools/demo_npm.py run test:demo-contract`.
Matching registries only show that names line up, not that a person can
finish the flow. Anything the browser cannot run is listed as a limitation.

## Commit style

Conventional Commits:

```
type(scope): subject
```

- Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`,
  `chore`. A release commit is `chore(release): vX.Y.Z`.
- Scope is optional. Common ones are `sync`, `review`, `later`, `feedback`,
  `backup`, `collection`, `net`, `update`, `ai`, `dupes`, `nightmode`,
  `platform`, `ui`, `config` and `demo`.
- The subject is lowercase and imperative, with no trailing period, and at
  most 72 characters.
- Add a body only when the reason is not obvious from the subject. No
  trailers.
- Describe what the change does now.
