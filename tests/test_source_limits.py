"""Limits on what a deck source can hand the add-on: the size and shape of a package
before anything is unpacked, and the paths a manifest may name.

A deck source is someone else's repo or folder, so a package is checked from its zip
directory alone (no member is decompressed to find out) and a path must stay inside
the source it came from.
"""
import os
import zipfile

import pytest

from internpearls import logic


def _zip(path, members, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression) as z:
        for name, data in members:
            z.writestr(name, data)
    return str(path)


def test_a_deck_the_size_of_the_largest_real_one_passes(tmp_path):
    """About 40 MB of stored pictures across some 80 members, the shape of the biggest
    deck a real source ships."""
    blob = os.urandom(512 * 1024)
    path = _zip(tmp_path / "big.apkg",
                [("collection.anki2", blob)] + [(str(i), blob) for i in range(80)])
    assert os.path.getsize(path) > 40 * 1024 * 1024
    logic.check_apkg_limits(path)


def test_the_limits_leave_wide_headroom_over_real_packages():
    """A whole-collection export with media runs to about 200 MB and a thousand files;
    the limits sit well above that, so only something no deck needs trips them."""
    assert logic.APKG_MAX_BYTES >= 500 * 1024 * 1024
    assert logic.APKG_MAX_EXPANDED >= 2 * logic.APKG_MAX_BYTES
    assert logic.APKG_MAX_MEMBERS >= 20000


def test_a_zip_bomb_is_refused_without_unpacking_it(tmp_path, monkeypatch):
    """A small file that would unpack to far more than any deck: refused from the
    sizes in its directory, before a byte is decompressed."""
    monkeypatch.setattr(logic, "APKG_MAX_EXPANDED", 8 * 1024 * 1024)
    path = _zip(tmp_path / "bomb.apkg", [("collection.anki2", b"\0" * (16 * 1024 * 1024))],
                zipfile.ZIP_DEFLATED)
    assert os.path.getsize(path) < 1024 * 1024
    opened = []
    monkeypatch.setattr(zipfile.ZipFile, "open",
                        lambda *a, **k: opened.append(a) or pytest.fail("decompressed"))
    with pytest.raises(logic.PackageLimitError, match="unpack to more than 8 MB"):
        logic.check_apkg_limits(path)
    assert not opened


def test_one_enormous_member_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(logic, "APKG_MAX_MEMBER", 1024 * 1024)
    path = _zip(tmp_path / "one.apkg", [("collection.anki2", b"\0" * (2 * 1024 * 1024))],
                zipfile.ZIP_DEFLATED)
    with pytest.raises(logic.PackageLimitError, match="more than 1 MB"):
        logic.check_apkg_limits(path)


def test_a_package_over_the_file_size_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(logic, "APKG_MAX_BYTES", 1024 * 1024)
    path = _zip(tmp_path / "large.apkg", [("collection.anki2", os.urandom(2 * 1024 * 1024))])
    with pytest.raises(logic.PackageLimitError, match="larger than 1 MB"):
        logic.check_apkg_limits(path)


def test_too_many_members_is_refused_before_the_directory_is_parsed(tmp_path, monkeypatch):
    """The member count comes from the end-of-archive record, so a package listing
    millions of entries is refused without building millions of entries in memory."""
    monkeypatch.setattr(logic, "APKG_MAX_MEMBERS", 50)
    path = _zip(tmp_path / "many.apkg", [(str(i), b"") for i in range(60)])

    def no_parse(*a, **k):
        raise AssertionError("the zip directory was parsed")
    monkeypatch.setattr(logic.zipfile, "ZipFile", no_parse)
    with pytest.raises(logic.PackageLimitError, match="more than 50 files"):
        logic.check_apkg_limits(path)


def test_the_member_count_is_read_from_a_zip64_archive_too(tmp_path):
    """Past 65,535 entries the count moves to the zip64 record."""
    path = _zip(tmp_path / "z64.apkg", [(str(i), b"") for i in range(70000)])
    assert logic._zip_directory_size(path)[0] == 70000


def test_a_file_that_is_not_a_zip_still_reads_as_a_bad_package(tmp_path):
    path = tmp_path / "junk.apkg"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(zipfile.BadZipFile):
        logic.check_apkg_limits(str(path))


def test_every_package_reader_checks_the_limits_first(tmp_path, monkeypatch):
    """The readers are where a package is first unpacked, on the main thread, so each
    one refuses an oversized package rather than extracting it."""
    from mock_anki import make_apkg
    path = str(tmp_path / "deck.apkg")
    make_apkg(path, [("g1", ["front", "back"], "tag")], deck="Deck")
    monkeypatch.setattr(logic, "APKG_MAX_EXPANDED", 10)
    for reader in (logic.apkg_notes, logic.apkg_deck_names):
        with pytest.raises(logic.PackageLimitError):
            reader(path)
    with pytest.raises(logic.PackageLimitError):
        logic.write_personalized(path, {}, str(tmp_path / "out.apkg"))
    assert logic.apkg_media_index(path) == {}
    assert logic.extract_apkg_media(path, {"a.png": "0"}, ["a.png"],
                                    str(tmp_path / "media")) == {}


@pytest.mark.parametrize("path,expected", [
    ("decks/Pharm.apkg", "decks/Pharm.apkg"),
    ("Pharm.apkg", "Pharm.apkg"),
    ("./decks//Pharm.apkg", "decks/Pharm.apkg"),
    ("skills/deck skill.md", "skills/deck skill.md"),
])
def test_a_plain_relative_path_is_accepted(path, expected):
    assert logic.safe_source_path(path) == expected


@pytest.mark.parametrize("path", [
    "../outside.apkg",
    "decks/../../outside.apkg",
    "decks/..",
    "/etc/passwd",
    "\\\\server\\share\\x.apkg",
    "C:\\Users\\x.apkg",
    "c:x.apkg",
    "decks\\..\\..\\x.apkg",
    "decks/\0.apkg",
    "",
    ".",
    None,
    42,
])
def test_a_path_that_could_leave_the_source_is_refused(path):
    with pytest.raises(RuntimeError, match="inside the deck source"):
        logic.safe_source_path(path)


def test_the_import_itself_refuses_an_oversized_package(anki, tmp_path, monkeypatch):
    """Anki's importer unpacks every member, so the import checks too, whatever path
    led to it."""
    from mock_anki import make_apkg
    from internpearls import collection
    path = str(tmp_path / "deck.apkg")
    make_apkg(path, [("g1", ["front", "back"], "tag")], deck="Deck")
    monkeypatch.setattr(logic, "APKG_MAX_EXPANDED", 10)
    with pytest.raises(logic.PackageLimitError):
        collection._import_apkg(path)
    assert not anki.col.imports


def test_a_compressed_collection_is_decoded_no_further_than_the_member_cap(
        tmp_path, monkeypatch):
    """A newer package's collection is zstd inside the zip, so its decoded size isn't in
    the directory: the decode itself stops at the cap."""
    import sys
    written = []

    class Bomb:
        class ZstdDecompressor:
            def copy_stream(self, src, dst):
                for _ in range(100):
                    dst.write(b"\0" * 1024)
                    written.append(1024)

    monkeypatch.setitem(sys.modules, "zstandard", Bomb)
    monkeypatch.setattr(logic, "APKG_MAX_MEMBER", 4 * 1024)
    path = _zip(tmp_path / "newer.apkg", [("collection.anki21b", b"tiny")])
    with pytest.raises(logic.PackageLimitError, match="collection would unpack"):
        logic.apkg_notes(path)
    assert sum(written) <= 5 * 1024
