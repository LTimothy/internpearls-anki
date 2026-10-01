"""The pure layer under "Later": registry migration, returning-row status, the digest
count line and the startup nudge predicate."""
import json

from internpearls import logic


def _entry(state="held", **extra):
    e = {"state": state, "front": "f", "deck": "IP::A", "decided": "2026-09-01",
         "hash": "h1"}
    e.update(extra)
    return e


# ------------------------------------------------------------------ migrate_declined
def test_migrate_turns_skip_into_held_and_marks_it_migrated():
    reg = {"g": _entry("skip")}
    assert logic.migrate_declined(reg) is True
    assert reg["g"] == _entry("held", migrated=True)


def test_migrate_leaves_other_states_and_non_dicts_alone():
    reg = {"n": _entry("never"), "k": _entry("keep"), "f": _entry("frozen"),
           "h": _entry("held"), "bad": "garbage", "none": None, "lst": ["skip"]}
    before = json.loads(json.dumps(reg))
    assert logic.migrate_declined(reg) is False
    assert reg == before


def test_migrate_is_idempotent_and_keeps_every_other_key():
    reg = {"g": _entry("skip", note="x", extra=1), "n": _entry("never")}
    assert logic.migrate_declined(reg) is True
    once = json.loads(json.dumps(reg))
    assert once["g"]["extra"] == 1 and once["g"]["note"] == "x"
    assert logic.migrate_declined(reg) is False
    assert reg == once


def test_migrate_tolerates_a_non_dict_registry():
    assert logic.migrate_declined(None) is False
    assert logic.migrate_declined([]) is False


def test_load_declined_migrates_and_saves(anki):
    from internpearls import config
    path = config.DECLINED
    with open(path, "w", encoding="utf8") as fh:
        json.dump({"g": _entry("skip"), "n": _entry("never")}, fh)
    reg = config.load_declined()
    assert reg["g"]["state"] == "held" and reg["g"]["migrated"] is True
    on_disk = json.load(open(path, encoding="utf8"))
    assert on_disk["g"] == _entry("held", migrated=True)
    assert on_disk["n"]["state"] == "never"


def test_load_declined_does_not_rewrite_a_clean_registry(anki, monkeypatch):
    from internpearls import config
    config.save_declined({"n": _entry("never")})
    saved = []
    monkeypatch.setattr(config, "save_declined", lambda r: saved.append(r))
    assert config.load_declined() == {"n": _entry("never")}
    assert saved == []


# -------------------------------------------------------------------- later_status
def test_later_status_waits_for_a_note_on_unchanged_content():
    entry = _entry(note="fix the wording")
    assert logic.later_status(entry, "h1") == {
        "later_wait": True, "later_note": "fix the wording",
        "later_note_status": "not_updated"}


def test_later_status_reports_changed_content_without_waiting():
    entry = _entry(note="fix the wording")
    assert logic.later_status(entry, "h2") == {
        "later_note": "fix the wording", "later_note_status": "updated"}


def test_later_status_without_a_note_never_waits():
    assert logic.later_status(_entry(), "h1") == {}
    assert logic.later_status(_entry(note=""), "h1") == {}


def test_later_status_migrated_waits_whatever_the_hash():
    assert logic.later_status(_entry(migrated=True), "other") == {
        "later_wait": True, "later_migrated": True}


def test_later_status_migrated_with_a_note_carries_both():
    out = logic.later_status(_entry(migrated=True, note="n"), "h2")
    assert out == {"later_wait": True, "later_migrated": True, "later_note": "n",
                   "later_note_status": "updated"}


def test_later_status_a_note_without_a_stored_hash_is_updated_and_does_not_wait():
    for entry in (_entry(note="n", hash=""), {"state": "held", "note": "n"}):
        assert logic.later_status(entry, "h1") == {
            "later_note": "n", "later_note_status": "updated"}
        assert logic.later_status(entry, None) == {
            "later_note": "n", "later_note_status": "updated"}


def test_later_status_ignores_non_held_and_non_dict_entries():
    assert logic.later_status(_entry("never", note="n"), "h1") == {}
    assert logic.later_status(_entry("keep", migrated=True), "h1") == {}
    assert logic.later_status("garbage", "h1") == {}
    assert logic.later_status(None, "h1") == {}


# ------------------------------------------------------------- rows and filter tabs
def test_a_new_row_carries_only_a_never_decision():
    assert logic.carries_decision(_entry("never"), "new")
    assert not logic.carries_decision(_entry("held"), "new")
    assert not logic.carries_decision(_entry("skip"), "new")
    assert logic.carries_decision(_entry("keep"), "changed")
    assert logic.carries_decision(_entry("frozen"), "changed")
    assert logic.carries_decision(_entry("never"), "changed") is False


def test_the_held_filter_tab_reads_later():
    assert dict(logic.FILTER_MODES)["held"] == "Later"


# ------------------------------------------------------------------------- digest
def _digest(registry):
    return logic.build_feedback_digest(
        [{"deck": "IP::A", "front": "flagged", "guid": "g0", "note": "n"}],
        standing_declines=registry)


def test_digest_snapshot_omits_later_cards_and_prints_a_count_line():
    text = _digest({"g1": _entry("held", front="Held card one"),
                    "g2": _entry("held", front="Held card two"),
                    "g3": _entry("never", front="Never card")})
    assert "Held card" not in text and "Held for later" not in text
    assert "Skipped for now" not in text
    assert "2 cards left for later" in text
    assert "Never imported (1)" in text and "Never card" in text


def test_digest_count_line_agrees_for_one_card():
    text = _digest({"g1": _entry("held")})
    assert "1 card left for later" in text and "1 cards" not in text


def test_digest_has_no_count_line_without_later_cards():
    text = _digest({"g1": _entry("never")})
    assert "left for later" not in text


def test_digest_with_only_later_cards_prints_no_empty_header():
    text = _digest({"g1": _entry("held"), "g2": _entry("held")})
    assert "Current standing declines" not in text
    assert "2 cards left for later" in text


def test_digest_header_counts_only_the_listed_entries():
    text = _digest({"g1": _entry("held"), "g3": _entry("never")})
    assert "Current standing declines (1)" in text
    assert "1 card left for later" in text


def test_digest_count_line_leaves_out_later_cards_in_excluded_decks():
    reg = {"g1": _entry("held"), "g2": _entry("held", deck="IP::Off"),
           "g3": _entry("held", deck="IP::Off::Sub")}
    text = logic.build_feedback_digest(
        [{"deck": "IP::A", "front": "flagged", "guid": "g0", "note": "n"}],
        standing_declines=reg, excluded=["IP::Off"])
    assert "2 cards left for later" in text   # same exact-name rule as later_count
    assert logic.later_count(reg, ["IP::Off"]) == 2
    text = logic.build_feedback_digest(
        [{"deck": "IP::A", "front": "flagged", "guid": "g0", "note": "n"}],
        standing_declines={"g2": _entry("held", deck="IP::Off")}, excluded=["IP::Off"])
    assert "left for later" not in text


# ------------------------------------------------------------------------- nudge
def test_later_nudge_is_due_only_when_the_count_grew():
    assert logic.later_nudge_due(3, None) is True
    assert logic.later_nudge_due(3, 0) is True
    assert logic.later_nudge_due(3, 2) is True
    assert logic.later_nudge_due(3, 3) is False
    assert logic.later_nudge_due(2, 3) is False
    assert logic.later_nudge_due(0, None) is False
    assert logic.later_nudge_due(0, 0) is False


# --------------------------------------------------------------------- later_seen
def test_later_seen_round_trips(anki):
    from internpearls import config
    assert config.load_later_seen() == 0
    config.save_later_seen(4)
    assert config.load_later_seen() == 4


def test_later_seen_tolerates_a_corrupt_or_odd_file(anki):
    from internpearls import config
    for body in ("not json{", '"text"', "[1]", '{"n": 1}', "-3", "true", "2.5"):
        with open(config.LATER_SEEN, "w", encoding="utf8") as fh:
            fh.write(body)
        assert config.load_later_seen() == 0, body


# -------------------------------------------------------------------- later_count
def test_later_count_counts_held_entries_outside_excluded_decks():
    reg = {"a": _entry("held"), "b": _entry("held", deck="IP::Gone"),
           "c": _entry("never"), "d": "garbage", "e": _entry("held", migrated=True)}
    assert logic.later_count(reg, ()) == 3
    assert logic.later_count(reg, ["IP::Gone"]) == 2
    assert logic.later_count({}, ()) == 0
    assert logic.later_count(None, ()) == 0
