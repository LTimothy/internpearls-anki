"""Saved feedback text and the dialog that reopens it."""
import datetime
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from internpearls import config, review
from test_dialogs import find, walk


ENTRIES = [{"deck": "Example", "front": "Front", "guid": "g1", "note": "check dose",
            "decision": "never"}]


def _folder():
    return Path(config._collection_state_path(review.FEEDBACK)).parent / "feedback_digests"


def _freeze(monkeypatch, hour=15, minute=20):
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 7, hour, minute, 3)
    monkeypatch.setattr(review.datetime, "datetime", Frozen)


def test_digest_is_archived_before_the_dialog_opens(anki, monkeypatch):
    def interact(payload):
        files = list(_folder().glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf8") == anki.gui.clipboard[-1]
        close = find(payload["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    assert review.offer_feedback_digest(None, ENTRIES) is True


def test_same_second_digests_keep_both_texts(anki, monkeypatch):
    _freeze(monkeypatch)
    review.save_feedback_digest("first\n", ENTRIES)
    review.save_feedback_digest("second\n", ENTRIES)
    files = sorted(_folder().glob("*.txt"))
    assert len(files) == 2
    assert {p.read_text(encoding="utf8") for p in files} == {"first\n", "second\n"}
    assert [int(p.name.split("-", 1)[0]) for p in files] == [1, 2]
    assert all(p.name.split("-", 1)[1] == "2026-10-07T15-20-03.txt" for p in files)


def test_clock_rollback_keeps_the_last_saved_digest_first(anki, monkeypatch):
    _freeze(monkeypatch, hour=1, minute=59)
    for i in range(20):
        review.save_feedback_digest(str(i), ENTRIES)
    _freeze(monkeypatch, hour=1, minute=0)
    review.save_feedback_digest("last saved", ENTRIES)
    saved = review.load_feedback_digests()
    assert [d["text"] for d in saved] == ["last saved"] + [str(i) for i in range(19, 0, -1)]
    assert saved[0]["when"] == datetime.datetime(2026, 10, 7, 1, 0, 3)
    assert len(list(_folder().glob("*.txt"))) == 20
    assert len(list(_folder().glob("*.json"))) == 20
    assert all(d["notes"] == 1 and d["decisions"] == 1 for d in saved)


def test_digest_list_uses_numeric_sequence_and_skips_unparseable_names(anki):
    folder = _folder()
    folder.mkdir(parents=True)
    for name, text in [
            ("999999-2026-10-07T01-59-03.txt", "older"),
            ("1000000-2026-10-07T01-00-03.txt", "newest"),
            ("not-a-digest.txt", "invalid"),
            ("1000001-not-a-timestamp.txt", "invalid date")]:
        (folder / name).write_text(text, encoding="utf8")
    assert [d["text"] for d in review.load_feedback_digests()] == ["newest", "older"]


def test_digest_write_failure_returns_false(anki, monkeypatch):
    def fail(*args):
        raise OSError("cannot write digest")
    monkeypatch.setattr(review, "_write_feedback_digest", fail)
    assert review.save_feedback_digest("feedback", ENTRIES) is False


def test_digest_without_an_open_collection_returns_false(anki):
    anki.mw.col = None
    assert review.save_feedback_digest("feedback", ENTRIES) is False


def test_no_feedback_returns_true_without_showing_a_digest(anki):
    assert review.offer_feedback_digest(None, []) is True
    assert review.show_result_with_feedback(None, (), []) is True
    assert not anki.gui.clipboard


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
    assert review.save_feedback_digest("café\n", ENTRIES) is True
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
    assert review.offer_feedback_digest(None, ENTRIES) is True
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


def _save_from_view(anki, monkeypatch, text):
    rounds = []
    def interact(payload):
        rounds.append(payload)
        fields = [n for n in payload["contract_tree"]["nodes"]
                  if n["kind"] == "textarea"]
        assert fields[0]["value"] == text
        label = "Save as file" if len(rounds) == 1 else "Close"
        button = find(payload["tree"], t="button", label=label)
        assert button is not None, f"Missing {label} button"
        return {"events": [{"id": button["id"], "click": True}]}
    monkeypatch.setattr(anki.gui, "next_interaction", interact)
    review.show_feedback_digest(None, text)
    assert len(rounds) == 2


def test_save_as_file_writes_displayed_text_atomically(anki, monkeypatch, tmp_path):
    text = "Intern Pearls card feedback (2026-10-06)\n\n  > café <b>dose</b>\n"
    target = tmp_path / "feedback <dose> & notes.txt"
    target.write_bytes(b"previous feedback")
    anki.gui.file_picks.append(str(target))
    replaced = []
    original = config.replace_file
    def replace(src, dst):
        assert Path(src).parent == target.parent
        assert Path(dst) == target
        assert target.read_bytes() == b"previous feedback"
        assert Path(src).read_bytes() == text.encode("utf8")
        replaced.append(src)
        return original(src, dst)
    monkeypatch.setattr(config, "replace_file", replace)
    _save_from_view(anki, monkeypatch, text)
    assert target.read_bytes() == text.encode("utf8")
    assert len(replaced) == 1
    assert not Path(replaced[0]).exists()
    assert anki.gui.tooltips == ["Saved to feedback &lt;dose&gt; &amp; notes.txt"]
    assert not anki.gui.warnings


def test_save_as_file_cancel_writes_nothing(anki, monkeypatch, tmp_path):
    before = set(tmp_path.rglob("*"))
    anki.gui.file_picks.append(None)
    _save_from_view(anki, monkeypatch, "feedback\n")
    assert set(tmp_path.rglob("*")) == before
    assert not anki.gui.tooltips
    assert not anki.gui.warnings


def test_save_as_file_picker_error_shows_safe_warning_and_stays_open(
        anki, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("Cannot open <b>file picker</b>")
    monkeypatch.setattr(review, "getSaveFile", fail)
    _save_from_view(anki, monkeypatch, "feedback\n")
    assert len(anki.gui.warnings) == 1
    assert "Something went wrong: Cannot open &lt;b&gt;file picker&lt;/b&gt;" in anki.gui.warnings[0]
    assert "Traceback" not in anki.gui.warnings[0]
    assert "RuntimeError: Cannot open <b>file picker</b>" in capsys.readouterr().out
    assert anki.gui.clipboard == ["feedback\n"]
    assert not anki.gui.tooltips


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_save_as_file_failure_warns_in_plain_text_and_stays_open(
        anki, monkeypatch, tmp_path, failure):
    target = tmp_path / "feedback.txt"
    target.write_bytes(b"previous feedback")
    anki.gui.file_picks.append(str(target))
    warnings = []
    from internpearls import ui
    original = ui.showWarning
    def warn(text, **kw):
        warnings.append((text, kw))
        return original(text, **kw)
    monkeypatch.setattr(ui, "showWarning", warn)
    def fail(*args, **kwargs):
        raise OSError("cannot write <feedback> & notes")
    if failure == "write":
        fdopen = review.os.fdopen
        @contextmanager
        def fail_write(*args, **kwargs):
            with fdopen(*args, **kwargs):
                yield SimpleNamespace(write=fail)
        monkeypatch.setattr(review.os, "fdopen", fail_write)
    else:
        monkeypatch.setattr(config, "replace_file", fail)
    _save_from_view(anki, monkeypatch, "feedback\n")
    assert len(warnings) == 1
    assert "cannot write <feedback> & notes" in warnings[0][0]
    assert warnings[0][1]["textFormat"] == "plain"
    assert target.read_bytes() == b"previous feedback"
    assert list(target.parent.glob("*.tmp")) == []
    assert not anki.gui.tooltips


@pytest.mark.parametrize("text, date", [
    ("Intern Pearls card feedback (2026-10-06)\n", "2026-10-06"),
    ("Intern Pearls card feedback\n  > appointment (2025-01-02)\n", "2026-10-07"),
])
def test_save_as_file_picker_uses_digest_date_or_today(anki, monkeypatch, text, date):
    class Today(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 7)
    monkeypatch.setattr(review.datetime, "date", Today)
    from aqt.utils import getSaveFile
    picks = []
    def pick(parent, title, key, name, ext, fname=""):
        picks.append((parent, name, ext, fname))
        return getSaveFile(parent, title, key, name, ext, fname=fname)
    monkeypatch.setattr(review, "getSaveFile", pick, raising=False)
    anki.gui.file_picks.append(None)
    _save_from_view(anki, monkeypatch, text)
    assert picks == [(anki.mw, "Text file", ".txt", f"Intern Pearls feedback {date}.txt")]
