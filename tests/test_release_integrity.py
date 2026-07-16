"""Guards the two release steps that are manual, and therefore forgettable.

`internpearls.ankiaddon` is committed, and self-update serves it straight from the
repo. So a release built from stale source doesn't fail loudly: people download old
code while version.json advertises the new version, and the in-app update check then
compares that same advertised version against itself and reports everyone up to date,
permanently. The same reasoning already guards the demo's mirrored copy in
test_demo_parity.py; the package that people actually install deserves it at least as
much.

Both failures here are fixed the same way: run ./build.sh.
"""
import json
import os
import zipfile

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
ADDON = os.path.join(ROOT, "internpearls")
PACKAGE = os.path.join(ROOT, "internpearls.ankiaddon")


def _packaged_names():
    """What build.sh puts in the zip: every top-level module, plus the three metadata
    files Anki reads. Derived from the source tree rather than hardcoded, so a new
    module can't be left out of the package and out of this test at the same time.
    """
    names = [f for f in os.listdir(ADDON) if f.endswith(".py")]
    return sorted(names + ["manifest.json", "config.json", "config.md"])


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_packaged_addon_matches_source():
    assert os.path.exists(PACKAGE), "internpearls.ankiaddon is missing: run ./build.sh"
    with zipfile.ZipFile(PACKAGE) as z:
        shipped = {n: z.read(n) for n in z.namelist()}

    expected = _packaged_names()
    stale = []
    for name in expected:
        if name not in shipped:
            stale.append(f"missing from the package: {name}")
        elif shipped[name] != _read(os.path.join(ADDON, name)):
            stale.append(f"stale in the package: {name}")
    stale += [f"orphaned in the package: {n}" for n in shipped if n not in expected]
    assert not stale, (
        "the packaged .ankiaddon is out of date with internpearls/. Run ./build.sh and "
        "commit the rebuilt package:\n  " + "\n  ".join(stale))


def test_advertised_version_matches_the_code():
    """version.json is what the update check compares against, and the package is what
    it then installs. If those two disagree, the add-on either never offers an update
    that exists, or offers one forever.
    """
    from internpearls.config import ADDON_VERSION
    with open(os.path.join(ROOT, "version.json")) as fh:
        advertised = json.load(fh)["version"]
    assert advertised == ADDON_VERSION, (
        f"version.json says {advertised!r} but ADDON_VERSION is {ADDON_VERSION!r}. "
        'Bump both in lockstep (see README, "Versioning").')


def test_packaged_version_matches_the_source_version():
    """Catches the specific ordering mistake: bumping the version and tagging, but
    packaging before the bump, or not at all.
    """
    from internpearls.config import ADDON_VERSION
    with zipfile.ZipFile(PACKAGE) as z:
        packaged_config = z.read("config.py").decode()
    assert f'ADDON_VERSION = "{ADDON_VERSION}"' in packaged_config, (
        f"the packaged config.py doesn't carry {ADDON_VERSION}: run ./build.sh")
