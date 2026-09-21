"""Keeps the GitHub Pages live demo generated from the add-on, never drifting.

The demo (docs/index.html + docs/demo_harness.py) doesn't re-implement the
add-on: it executes the real modules under Pyodide against the same mock-Anki
harness pytest uses. Menu structure, dialog layout and wording, version, and
behavior therefore all come from the code itself at runtime. The only thing
that CAN drift is the mirrored copy of the source that the static site serves —
build.sh refreshes docs/addon/ on every build, and this test fails if a copy
is stale, so shipping a code change without re-mirroring is impossible.

Card content isn't duplicated either: the demo downloads the example deck
repo's real manifest and .apkg files at load.
"""
import os
import json
import subprocess
import sys

import mock_anki

HERE = os.path.dirname(__file__)
ADDON = os.path.join(HERE, "..", "internpearls")
DOCS_ADDON = os.path.join(HERE, "..", "docs", "addon")


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_docs_addon_mirror_is_current():
    expected = {f: _read(os.path.join(ADDON, f))
                for f in os.listdir(ADDON) if f.endswith(".py")}
    expected["mock_anki.py"] = _read(os.path.join(HERE, "mock_anki.py"))
    expected["demo_replay.py"] = _read(os.path.join(
        HERE, "..", "demo", "replay.py"))
    stale = []
    for name, want in sorted(expected.items()):
        mirrored = os.path.join(DOCS_ADDON, name)
        if not os.path.exists(mirrored):
            stale.append(f"missing: docs/addon/{name}")
        elif _read(mirrored) != want:
            stale.append(f"stale: docs/addon/{name}")
    extras = [f for f in os.listdir(DOCS_ADDON) if f.endswith(".py")
              and f not in expected]
    stale += [f"orphaned: docs/addon/{f}" for f in extras]
    assert not stale, ("the live demo's mirrored source is out of date — run "
                       "./build.sh to refresh docs/addon/:\n  " +
                       "\n  ".join(stale))


def test_demo_contract_generated_files_are_current():
    result = subprocess.run(
        [sys.executable, "tools/generate_demo_contract.py", "--check"],
        cwd=os.path.join(HERE, ".."),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_demo_fixture_operations_use_real_packages_and_fixed_transport(tmp_path):
    source = tmp_path / "source"
    decks = source / "decks"
    decks.mkdir(parents=True)
    package = decks / "sample.apkg"
    fields = lambda front, back: [front, back, "", "", "", "", ""]
    mock_anki.make_apkg(
        str(package),
        [(f"sample-{index}", fields(f"Sample card {index}", f"Answer {index}"),
          "SampleScope") for index in range(1, 5)],
        deck="Example Decks::Sample",
    )
    (source / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "decks": [{
            "name": "Example Decks::Sample", "apkg": "decks/sample.apkg",
            "version": "fixture", "cards": 4,
        }],
        "scope_tag": "SampleScope", "export_deck": "Example Decks",
        "front_aliases": {},
    }), encoding="utf8")
    environment = dict(os.environ)
    environment["DEMO_SOURCE"] = str(source)
    environment["PYTHONPATH"] = os.pathsep.join([
        os.path.abspath("docs"), os.path.abspath("tests"), os.path.abspath("."),
        environment.get("PYTHONPATH", ""),
    ])
    result = subprocess.run(
        [sys.executable, "-c", """
import json
import os
import sqlite3
import tempfile
import zipfile
import demo_harness as harness

for operation in ("history", "bulk", "auto"):
    harness.validate_worker_message({
        "type": "maintainer", "payload": {"operation": operation},
    })

harness.maintainer("history")
manifest = json.load(open(os.path.join(harness.SOURCE, "manifest.json"), encoding="utf8"))
deck = manifest["decks"][0]
assert manifest["schema"] == 2
assert len(manifest["change_notes"]) == 2
assert len(manifest["retired"][deck["name"]]) == 1
assert len(manifest["deck_moves"]) == 1

harness.maintainer("bulk")
with tempfile.TemporaryDirectory() as folder:
    with zipfile.ZipFile(os.path.join(harness.SOURCE, deck["apkg"])) as archive:
        archive.extractall(folder)
    con = sqlite3.connect(os.path.join(folder, "collection.anki2"))
    fronts = [row[0].split(harness.mock_anki.FS)[0]
              for row in con.execute("select flds from notes")]
    con.close()
assert "Bulk card 001" in fronts and "Bulk card 180" in fronts

harness.maintainer("auto")
harness._install_demo_net()
url = ("https://api.github.com/repos/" + harness.config.ANKI_REPO
       + "/contents/version.json?ref=main")
version = json.loads(harness.net._http_get(url))
assert version["version"] == harness.config.ADDON_VERSION
"""],
        cwd=os.path.abspath("."), env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_demo_mock_normalizes_package_model_ids_like_anki():
    collection = mock_anki.MockCollection()
    model = mock_anki.make_model(name="Imported package model")
    model["id"] = str(model["id"])

    collection._register_models({int(model["id"]): model})

    imported = collection.models.by_name("Imported package model")
    assert isinstance(imported["id"], int)
