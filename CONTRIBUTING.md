# Contributing

Bug reports, fixes, and small features are welcome. Open an issue first for
anything bigger than a bug fix so we can agree on the approach before you
write code.

## Setup

No install step beyond Python 3 and pytest:

```bash
pip install pytest
cd addon && pytest tests/ -v
```

Tests run against `internpearls/logic.py` only and need no Anki install. To
try your change in Anki itself, run `./build.sh` and install the resulting
`internpearls.ankiaddon` via Tools > Add-ons > Install from file (restart
Anki after).

## Where code goes

The one structural rule: code that could run without Anki goes in
`internpearls/logic.py` (no `aqt`/`anki` imports — apkg handling, GUID
matching, version comparison, formatting). If your new function can be tested
with plain Python, it belongs in `logic.py`, with a test in
`tests/test_logic.py`. Code that touches `mw`, `col`, or Qt goes in the module
matching its concern — `collection.py` (collection reads/writes), `sync.py`
(sync flows), `dialogs.py` (panels), `net.py` (fetches), `ui.py` (dialog
wrappers and styling), `updates.py` (self-update), `background.py` (unattended
checks), `config.py` (constants and config) — with `__init__.py` holding only
the menu and startup wiring. See "Code layout" in the README.

## Conventions

- **Dialogs** go through the `_info` / `_warn` / `_ask` / `_prompt` wrappers
  in `internpearls/ui.py`, never raw `aqt.utils` calls.
- **Menu items** are sentence case ("Import single deck", not "Import Single
  Deck") with no trailing ellipses.
- **Persistent state** (anything that must survive an add-on update) lives
  under `internpearls/user_files/`, nowhere else in the add-on folder.
- **Imports stay `merge_notetypes=False`.** Flipping it to `True` forces
  AnkiWeb full syncs on every import; note types are reconciled by the
  Fix-note-types step instead. See "How history is preserved" in the README.
- **Fetches from this repo's GitHub** (version.json, the .ankiaddon) go
  through the contents API (`_gh_public_raw`), not raw.githubusercontent.com,
  which can serve stale files for minutes after a push.
- **No deck content.** This repo ships tooling only; card content, deck
  names beyond the configurable defaults, and anything tied to a specific
  private deck source don't belong here.

## Pull requests

- Keep changes surgical: touch only what the fix or feature needs, and match
  the existing style even where you'd do it differently.
- Add or update a test in `tests/test_logic.py` for any `logic.py` change.
- Run `pytest tests/ -v` and `./build.sh` before opening the PR.
- Changed a stylesheet, border, spacing, or color? Render it and look:
  `python3 tools/render_dialog.py --list`. The test suite uses mock Qt and cannot
  tell you whether Qt painted anything. See "Seeing a dialog actually render" and
  "Colors" in `README.md`.
- Don't bump the version, edit `CHANGELOG.md`, or rebuild
  `internpearls.ankiaddon` in your PR — releases (semver bump in
  `internpearls/config.py` + `version.json`, tag, changelog entry, repackage) are done
  by the maintainer, as described under "Versioning" in the README.
