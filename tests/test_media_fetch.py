"""Deck images listed in the manifest are fetched one by one, cached by hash, and put
back into the downloaded package before anything reads it."""
import hashlib
import json
from pathlib import Path

import pytest

from mock_anki import make_apkg
from test_sync_flows import _configure, _fields, _sync, _write_source, DECK, TAGS

X, Y = b"picture-x", b"picture-y"


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _entries(**files):
    return {f"{k}.png": {"path": f"media/{k}.png", "sha256": _sha(v)}
            for k, v in files.items()}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from internpearls import sync
    path = tmp_path / "media_cache"
    monkeypatch.setattr(sync, "MEDIA_CACHE", str(path))
    return path


def _github(tmp_path, monkeypatch, media, files):
    """A GitHub source whose only deck lists `media`; `files` maps repo path to bytes
    (or an Exception to raise). Returns (deck entry, fetch, requested paths)."""
    from internpearls import sync
    apkg = tmp_path / "deck.apkg"
    make_apkg(str(apkg), [("g1", _fields("Q"), TAGS)])
    deck = {"name": DECK, "version": "v1", "apkg": "decks/deck.apkg", "media": media}
    manifest = {"schema": 4, "decks": [deck]}
    requested = []

    def raw(repo, path, *args, **kwargs):
        requested.append(path)
        if path == "manifest.json":
            return json.dumps(manifest).encode()
        if path == "decks/deck.apkg":
            return apkg.read_bytes()
        got = files[path]
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(sync, "_gh_raw", raw)
    cfg = dict(sync._cfg(), gh_repo="example/decks")
    _, fetch, _ = sync._fetch_manifest(cfg)
    requested.clear()
    return deck, fetch, requested


def _index(path):
    from internpearls import logic
    return logic.apkg_media_index(path)


def test_media_is_downloaded_verified_and_injected(anki, tmp_path, monkeypatch, cache):
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X),
                                     {"media/x.png": X})
    path = fetch(deck)
    assert set(_index(path)) == {"x.png"}
    assert (cache / _sha(X)).read_bytes() == X
    assert requested == ["decks/deck.apkg", "media/x.png"]


def test_media_cache_hit_makes_no_request(anki, tmp_path, monkeypatch, cache):
    cache.mkdir()
    (cache / _sha(X)).write_bytes(X)
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X), {})
    path = fetch(deck)
    assert requested == ["decks/deck.apkg"]
    assert set(_index(path)) == {"x.png"}


def test_media_hash_mismatch_refuses_deck(anki, tmp_path, monkeypatch, cache):
    deck, fetch, _ = _github(tmp_path, monkeypatch, _entries(x=X),
                             {"media/x.png": b"tampered"})
    with pytest.raises(RuntimeError, match="checksum"):
        fetch(deck)
    assert not (cache / _sha(X)).exists()
    assert not (cache / _sha(b"tampered")).exists()


def test_a_corrupted_cache_file_is_fetched_again(anki, tmp_path, monkeypatch, cache):
    cache.mkdir()
    (cache / _sha(X)).write_bytes(b"truncated")
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X),
                                     {"media/x.png": X})
    fetch(deck)
    assert requested == ["decks/deck.apkg", "media/x.png"]
    assert (cache / _sha(X)).read_bytes() == X


def test_media_fetch_failure_leaves_deck_unfetched_and_keeps_cache(
        anki, tmp_path, monkeypatch, cache):
    files = {"media/x.png": X, "media/y.png": RuntimeError("network down")}
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X, y=Y), files)
    with pytest.raises(RuntimeError, match="network down"):
        fetch(deck)
    assert (cache / _sha(X)).read_bytes() == X
    files["media/y.png"] = Y
    requested.clear()
    path = fetch(deck)
    assert requested == ["decks/deck.apkg", "media/y.png"]
    assert set(_index(path)) == {"x.png", "y.png"}


def test_hostile_media_entry_refuses_before_any_image_request(
        anki, tmp_path, monkeypatch, cache):
    bad = {"../x.png": {"path": "media/x.png", "sha256": _sha(X)}}
    deck, fetch, requested = _github(tmp_path, monkeypatch, bad, {"media/x.png": X})
    with pytest.raises(RuntimeError):
        fetch(deck)
    assert "media/x.png" not in requested


def test_schema3_deck_fetch_unchanged(anki, tmp_path, monkeypatch, cache):
    deck, fetch, requested = _github(tmp_path, monkeypatch, None, {})
    del deck["media"]
    path = fetch(deck)
    assert Path(path).read_bytes() == (tmp_path / "deck.apkg").read_bytes()
    assert requested == ["decks/deck.apkg"]
    assert not cache.exists()


def _local_source(tmp_path, media):
    folder = tmp_path / "source"
    (folder / "media").mkdir(parents=True)
    make_apkg(str(folder / "deck.apkg"), [("g1", _fields("Q"), TAGS)], deck=DECK)
    for name, data in media.items():
        (folder / "media" / name).write_bytes(data)
    entries = {n: {"path": f"media/{n}", "sha256": _sha(b)} for n, b in media.items()}
    manifest = {"schema": 4, "decks": [{"name": DECK, "version": "v1",
                                        "apkg": "deck.apkg", "cards": 1,
                                        "media": entries}]}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return folder


def test_local_folder_injects_without_touching_source(anki, tmp_path, cache):
    from internpearls import sync
    folder = _local_source(tmp_path, {"x.png": X})
    before = (folder / "deck.apkg").read_bytes()
    _configure(anki, str(folder))
    manifest, fetch, _ = sync._fetch_manifest(sync._cfg())
    path = fetch(manifest["decks"][0])
    assert (folder / "deck.apkg").read_bytes() == before
    assert Path(path).parent != folder
    assert set(_index(path)) == {"x.png"}


def test_sync_imports_the_package_with_its_images(anki, tmp_path, cache, monkeypatch):
    from internpearls import collection
    seen, real = [], collection._import_apkg

    def spy(path, *args, **kwargs):
        seen.append(set(_index(path)))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(collection, "_import_apkg", spy)
    folder = Path(_write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    (folder / "media").mkdir()
    (folder / "media" / "x.png").write_bytes(X)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["schema"] = 4
    manifest["decks"][0]["media"] = {"x.png": {"path": "media/x.png", "sha256": _sha(X)}}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    _configure(anki, str(folder))
    _sync(anki)
    assert anki.col.note_by_guid("g1") is not None
    assert seen and all(s == {"x.png"} for s in seen)


def test_prune_media_cache_keeps_listed_hashes(anki, cache):
    from internpearls import sync
    cache.mkdir()
    for b in (X, Y):
        (cache / _sha(b)).write_bytes(b)
    (cache / "stray.part").write_bytes(b"")
    sync._prune_media_cache({"schema": 4, "decks": [{"media": _entries(x=X)}]})
    assert sorted(p.name for p in cache.iterdir()) == sorted([_sha(X), "stray.part"])


def test_readers_can_skip_image_downloads(anki, tmp_path, monkeypatch, cache):
    from internpearls import sync
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X),
                                     {"media/x.png": X})
    path = sync._cached_fetch(fetch, deck, with_media=False)
    assert requested == ["decks/deck.apkg"]
    assert _index(path) == {}
    assert not cache.exists()


def test_prune_ignores_a_malformed_hash(anki, cache):
    from internpearls import sync
    cache.mkdir()
    (cache / _sha(X)).write_bytes(X)
    sync._prune_media_cache({"decks": [{"media": {"x.png": {"sha256": []}}},
                                       {"media": _entries(x=X)}]})
    assert (cache / _sha(X)).exists()


def test_a_manifest_this_addon_cannot_read_leaves_the_cache_alone(
        anki, tmp_path, monkeypatch, cache):
    from internpearls import sync
    cache.mkdir()
    (cache / _sha(X)).write_bytes(X)
    manifest = {"schema": 99, "decks": []}
    monkeypatch.setattr(sync, "_gh_raw", lambda *a, **k: json.dumps(manifest).encode())
    sync._fetch_manifest(dict(sync._cfg(), gh_repo="example/decks"))
    assert (cache / _sha(X)).exists()


def test_a_full_fetch_reuses_the_bare_download(anki, tmp_path, monkeypatch, cache):
    from internpearls import sync
    deck, fetch, requested = _github(tmp_path, monkeypatch, _entries(x=X),
                                     {"media/x.png": X})
    sync._cached_fetch(fetch, deck, with_media=False)
    path = sync._cached_fetch(fetch, deck)
    assert requested == ["decks/deck.apkg", "media/x.png"]
    assert set(_index(path)) == {"x.png"}


def test_image_progress_ticks_once_per_image(anki, tmp_path, monkeypatch, cache):
    deck, fetch, _ = _github(tmp_path, monkeypatch, _entries(x=X, y=Y),
                             {"media/x.png": X, "media/y.png": Y})
    ticks = []

    def pump(_n=None):
        return True
    pump.part = lambda done, of, label=None: ticks.append((done, of, label)) or True
    fetch(deck, on_chunk=pump)
    assert [(d, o) for d, o, _ in ticks] == [(1, 2), (2, 2)]
    assert ticks[-1][2] == "Pharm images (2 of 2)"


def test_cancel_during_images_stops_the_fetch(anki, tmp_path, monkeypatch, cache):
    from internpearls.net import DownloadCancelled
    deck, fetch, _ = _github(tmp_path, monkeypatch, _entries(x=X, y=Y),
                             {"media/x.png": X, "media/y.png": Y})

    def pump(_n=None):
        return True
    pump.part = lambda done, of, label=None: False
    with pytest.raises(DownloadCancelled):
        fetch(deck, on_chunk=pump)


def test_each_deck_owns_its_cards_share_of_the_bar():
    from internpearls import sync
    assert [sync._share([0, 1, 5], 6, i) for i in (1, 2, 3)] == [1, 4, 1]
