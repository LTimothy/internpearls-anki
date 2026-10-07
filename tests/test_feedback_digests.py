"""Saved feedback text and the dialog that reopens it."""
import datetime
from pathlib import Path

import pytest

from internpearls import config, review
from test_dialogs import find, walk


ENTRIES = [{"deck": "Example", "front": "Front", "guid": "g1", "note": "check dose",
            "decision": "never"}]


def _folder():
    return Path(config._collection_state_path(review.FEEDBACK)).parent / "feedback_digests"


def _freeze(monkeypatch):
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 7, 15, 20, 3)
    monkeypatch.setattr(review.datetime, "datetime", Frozen)


def test_digest_is_archived_before_the_dialog_opens(anki, monkeypatch):
    def interact(payload):
        files = list(_folder().glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf8") == anki.gui.clipboard[-1]
        close = find(payload["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.offer_feedback_digest(None, ENTRIES)


def test_same_second_digests_keep_both_texts(anki, monkeypatch):
    _freeze(monkeypatch)
    review.save_feedback_digest("first\n", ENTRIES)
    review.save_feedback_digest("second\n", ENTRIES)
    files = sorted(_folder().glob("*.txt"))
    assert len(files) == 2
    assert {p.read_text(encoding="utf8") for p in files} == {"first\n", "second\n"}
    assert all(p.name.startswith("2026-10-07T15-20-03") for p in files)


def test_prune_keeps_the_twenty_newest_digests_and_their_counts(anki, monkeypatch):
    _freeze(monkeypatch)
    for i in range(25):
        review.save_feedback_digest(str(i), ENTRIES)
    saved = review.load_feedback_digests()
    assert [d["text"] for d in saved] == [str(i) for i in range(24, 4, -1)]
    assert len(list(_folder().glob("*.txt"))) == 20
    assert len(list(_folder().glob("*.json"))) == 20
    assert all(d["notes"] == 1 and d["decisions"] == 1 for d in saved)


def test_digest_write_is_atomic_and_utf8(anki, monkeypatch):
    seen = []
    original = config.replace_file
    def replace(src, dst):
        if str(dst).endswith(".txt"):
            assert not Path(dst).exists()
            assert Path(src).parent == Path(dst).parent
            seen.append(Path(src).read_text(encoding="utf8"))
        return original(src, dst)
    monkeypatch.setattr(config, "replace_file", replace)
    review.save_feedback_digest("café\n", ENTRIES)
    assert seen == ["café\n"]
    assert not list(_folder().glob("*.tmp"))


def test_digest_archives_follow_collection_and_source(anki, tmp_path):
    anki.mw.col.path = str(tmp_path / "one" / "collection.anki2")
    review.save_feedback_digest("one", ENTRIES)
    first = _folder()
    anki.mw.col.path = str(tmp_path / "two" / "collection.anki2")
    assert review.load_feedback_digests() == []
    review.save_feedback_digest("two", ENTRIES)
    assert _folder() != first
    anki.mw._config = {"github_decks_repo": "example/another"}
    assert review.load_feedback_digests() == []
    anki.mw.col.path = str(tmp_path / "one" / "collection.anki2")
    anki.mw._config = {}
    assert [d["text"] for d in review.load_feedback_digests()] == ["one"]


def test_prune_failure_does_not_stop_the_digest_view(anki, monkeypatch, capsys):
    _freeze(monkeypatch)
    for i in range(20):
        review.save_feedback_digest(str(i), ENTRIES)
    def fail(*args):
        raise OSError("cannot prune")
    monkeypatch.setattr(review.os, "remove", fail)
    def interact(payload):
        close = find(payload["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.offer_feedback_digest(None, ENTRIES)
    assert anki.gui.clipboard
    assert "cannot prune" in capsys.readouterr().out
    assert len(list(_folder().glob("*.txt"))) == 21


def test_recent_feedback_rows_are_newest_first_and_show_unchanged_text(anki, monkeypatch):
    _freeze(monkeypatch)
    older = "older text\n"
    newest = "newest <b>text</b>\n"
    review.save_feedback_digest(older, ENTRIES)
    review.save_feedback_digest(newest, ENTRIES + [dict(ENTRIES[0], note="second")])
    pending = {"pending": {"note": "still typing"}}
    review.save_feedback(pending)
    rounds = []
    def interact(payload):
        rounds.append(payload)
        tree = payload["tree"]
        if "recent" in payload["title"].lower() and len(rounds) == 1:
            labels = [n["text"] for n in walk(tree) if n.get("t") == "label"]
            summaries = [s for s in labels if "Oct 2026" in s]
            assert summaries == ["7 Oct 2026, 15:20 · 2 notes, 2 decisions",
                                 "7 Oct 2026, 15:20 · 1 note, 1 decision"]
            show = find(tree, t="button", label="Show")
            return {"events": [{"id": show["id"], "click": True}]}
        if "recent" not in payload["title"].lower():
            fields = [n for n in payload["contract_tree"]["nodes"]
                      if n["kind"] == "textarea"]
            assert len(fields) == 1
            assert fields[0]["value"] == newest
            assert fields[0]["readonly"]
        close = find(tree, t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.open_recent_feedback()
    assert anki.gui.clipboard[-1] == newest
    assert review.load_saved_feedback() == pending
    assert len(list(_folder().glob("*.txt"))) == 2


def test_recent_feedback_empty_state(anki, monkeypatch):
    def interact(payload):
        labels = [n["text"] for n in walk(payload["tree"]) if n.get("t") == "label"]
        assert labels == ["No card feedback has been saved yet."]
        close = find(payload["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.open_recent_feedback()


@pytest.mark.parametrize("sidecar", [
    "corrupt", "missing", "unreadable", "[]", "null", '{"notes": "x"}',
    '{"notes": 1, "decisions": 1.5}', '{"notes": true, "decisions": 1}', "{}",
])
def test_recent_feedback_with_unknown_counts_keeps_and_shows_text(anki, monkeypatch, sidecar):
    _freeze(monkeypatch)
    text = "saved <b>feedback</b>\n"
    review.save_feedback_digest(text, ENTRIES)
    path = next(_folder().glob("*.json"))
    if sidecar in ("missing", "unreadable"):
        path.unlink()
        if sidecar == "unreadable":
            path.mkdir()
    else:
        path.write_text("{" if sidecar == "corrupt" else sidecar, encoding="utf8")
    saved = review.load_feedback_digests()
    assert [d["text"] for d in saved] == [text]
    assert saved[0]["notes"] is None and saved[0]["decisions"] is None
    rounds = []
    def interact(payload):
        rounds.append(payload)
        tree = payload["tree"]
        if "recent" in payload["title"].lower() and len(rounds) == 1:
            labels = [n["text"] for n in walk(tree) if n.get("t") == "label"]
            assert [s for s in labels if "Oct 2026" in s] == [
                "7 Oct 2026, 15:20 · counts unavailable"]
            show = find(tree, t="button", label="Show")
            return {"events": [{"id": show["id"], "click": True}]}
        if "recent" not in payload["title"].lower():
            fields = [n for n in payload["contract_tree"]["nodes"]
                      if n["kind"] == "textarea"]
            assert len(fields) == 1
            assert fields[0]["value"] == text
            assert fields[0]["readonly"]
        close = find(tree, t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.open_recent_feedback()
    assert anki.gui.clipboard == [text]
    assert not anki.gui.warnings
    assert len(rounds) == 3


def test_recent_feedback_error_shows_safe_warning(anki, monkeypatch, capsys):
    def fail():
        raise RuntimeError("Cannot load <b>feedback</b>")
    monkeypatch.setattr(review, "load_feedback_digests", fail)
    review.open_recent_feedback()
    assert len(anki.gui.warnings) == 1
    assert "Something went wrong: Cannot load &lt;b&gt;feedback&lt;/b&gt;" in anki.gui.warnings[0]
    assert "Traceback" not in anki.gui.warnings[0]
    assert "RuntimeError: Cannot load <b>feedback</b>" in capsys.readouterr().out
