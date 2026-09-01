# Vendored dependencies

- `pypdf` 6.16.2 (BSD-3-Clause, see `LICENSE`), downloaded with
  `pip3 download pypdf --no-deps` and extracted from the wheel's `pypdf/`
  package only (no tests, no optional extras). Used to extract text and
  embedded images from attached PDFs, entirely locally.

Imported lazily by `internpearls/ai_logic.py`, which adds this directory to
`sys.path` only inside the function that needs it, so a normal Anki launch
never pays the import cost.
