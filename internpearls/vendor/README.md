# Vendored dependencies

- `pypdf` 6.18.0 (BSD-3-Clause, see `LICENSE`): the `pypdf/` package from the
  wheel that `pip3 download pypdf --no-deps` fetches, without tests or extras.
  It reads text and images from PDFs attached to the AI card wizard, on your
  own machine.

`internpearls/ai_logic.py` adds this folder to `sys.path` only when it first
needs pypdf, so a normal Anki launch never imports it.
