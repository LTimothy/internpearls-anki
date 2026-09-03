# tests/test_ai_cli.py
import json
import os
import sys
import threading
import time
import pytest

from internpearls import ai_cli, ai_logic

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_cli.py")]


def _run(mode="ok", **kw):
    return ai_cli._run_argv(FAKE + [mode], "claude", "PROMPT", **kw)


class _CallbackBoom(Exception):
    pass


def test_run_happy_path_parses_result_and_phase():
    events = []
    res = _run(on_event=events.append)
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15
    assert any(e.get("phase") == "Verify online" for e in events)


def test_run_failure_raises_generation_error_with_stderr():
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("fail")
    assert "boom" in str(e.value)


def test_run_garbage_output_raises():
    with pytest.raises(ai_cli.GenerationError):
        _run("garbage")


def test_run_no_usable_reply_error_names_event_count_and_last_line():
    # A stream that never produces a parseable result must not just say "no
    # usable reply": it names how many stream lines it saw, echoes the last
    # non-empty one (the owner's only clue without opening the log file), and
    # points at ai_last_run.log for the full stream.
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("garbage")
    msg = str(e.value)
    assert "1 events seen" in msg
    assert "not json" in msg
    assert "ai_last_run.log" in msg
    assert "user_files" in msg


def test_run_log_written_with_stdout_and_exit_code(tmp_path):
    log_path = tmp_path / "ai_last_run.log"
    _run("ok", log_path=str(log_path))
    text = log_path.read_text(encoding="utf8")
    assert "--- stdout ---" in text
    assert "Front" in text and "WebSearch" in text  # a raw stdout line made it in verbatim
    assert "exit code: 0" in text


def test_write_run_log_elides_a_prompt_carried_in_argv(tmp_path):
    # agy carries the prompt in argv (see build_argv); the run's own
    # material, and the bundled/deck/user skill text riding along in it,
    # must never land in a file whose whole purpose is to be shared for
    # debugging (see _elided_argv).
    log_path = tmp_path / "ai_last_run.log"
    prompt = "the learner's actual source material"
    argv = ["/usr/bin/agy", "-p", prompt]
    ai_cli._write_run_log(str(log_path), argv, prompt,
                          ['{"event": "result"}\n'], "", 0, 1.2)
    text = log_path.read_text(encoding="utf8")
    assert prompt not in text
    assert f"<prompt, {len(prompt)} chars>" in text
    assert "exit code: 0" in text
    assert "elapsed: 1.2s" in text


def test_run_log_elides_a_stdout_line_that_echoes_the_prompt(tmp_path):
    # A CLI that echoes the prompt into a stream event (a transcript-style
    # "user_input" event, say) must not leak it into the run log via an
    # unscrubbed stdout line, even though the argv itself is already elided.
    # Only that one line is replaced; the rest of the stream survives.
    log_path = tmp_path / "ai_last_run.log"
    prompt = ("This is the learner's actual pasted source material for the "
             "card batch, which must never reach the log verbatim.")
    ai_cli._run_argv(FAKE + ["echo_prompt"], "claude", prompt,
                     log_path=str(log_path))
    text = log_path.read_text(encoding="utf8")
    assert prompt not in text
    assert "<line containing the prompt elided," in text
    assert "WebSearch" in text            # the other stdout lines survive
    assert "Front" in text and "Study Deck - Basic" in text


def test_no_usable_reply_excerpt_elided_when_last_line_echoes_prompt():
    prompt = ("This is the learner's actual pasted source material for the "
             "card batch, which must never reach the error dialog verbatim.")
    with pytest.raises(ai_cli.GenerationError) as e:
        ai_cli._run_argv(FAKE + ["echo_prompt_garbage"], "claude", prompt)
    msg = str(e.value)
    assert prompt not in msg
    assert "<line containing the prompt elided," in msg


def test_run_log_unchanged_when_lines_only_share_a_short_common_word(tmp_path):
    # "Basic" turns up in both the prompt and the fake CLI's own output (its
    # note type is "Study Deck - Basic"), but that overlap is a single short
    # word, not an actual echo of the prompt, so it must not trigger elision.
    log_path = tmp_path / "ai_last_run.log"
    prompt = ("Basic pharmacology review\n"
             "the rest of the learner's real source material, unrelated "
             "to anything the fake CLI ever prints back.")
    ai_cli._run_argv(FAKE + ["ok"], "claude", prompt, log_path=str(log_path))
    text = log_path.read_text(encoding="utf8")
    assert "<line containing the prompt elided" not in text
    assert "Basic" in text
    assert "WebSearch" in text


# === redact_texts: the production prompt (ai_logic.build_prompt) buries the
# learner's source material, focus text, and saved rules well past the first
# three lines under bundled skill and schema boilerplate, so a CLI that
# echoes back only that material (not the prompt's front) would otherwise
# match no needle at all and leak the learner's own words into the run log.
# These tests build a real prompt through build_prompt, with the bundled
# skill, a source text, a focus text, and a user skill all present, so the
# needles under test are exercised against the actual shape a run sees.
_SOURCE = (
    "Digoxin toxicity classically presents with GI upset, visual halos, and "
    "cardiac dysrhythmias. Renal clearance means dose adjustment is required "
    "in elderly patients and anyone with reduced creatinine clearance, since "
    "levels accumulate quietly over days before symptoms appear. Hypokalemia "
    "and hypomagnesemia both potentiate toxicity even at a therapeutic level, "
    "so electrolytes are checked alongside any digoxin level. Treatment for "
    "severe toxicity is digoxin-specific antibody fragments, not simply "
    "holding the dose and waiting for it to clear on its own.")
_FOCUS = "Focus: emphasize the electrolyte interactions and elderly dosing."
_USER_SKILL = "Always cite Barash for any drug dose stated on a card."
assert len(_SOURCE) > 240   # exercises the middle/end window needles


def _real_prompt():
    return ai_logic.build_prompt(
        skills=ai_logic.active_skills(None, _USER_SKILL),
        source=_SOURCE, note_types=["Basic"],
        field_map={"Basic": ["Front", "Back"]}, count=3,
        instructions=_FOCUS, mode="quick")


def _run_echo(monkeypatch, tmp_path, fake_mode, echo_text):
    monkeypatch.setenv("FAKE_CLI_ECHO_TEXT", echo_text)
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + [fake_mode], True))
    log_path = tmp_path / "ai_last_run.log"
    res = ai_cli.run_generation(
        "claude", "/usr/bin/claude", _real_prompt(), "quick", str(tmp_path),
        log_path=str(log_path), redact_texts=(_SOURCE, _FOCUS, _USER_SKILL))
    return res, log_path.read_text(encoding="utf8")


def test_redact_texts_elides_an_echoed_source_material_section(
        monkeypatch, tmp_path):
    _, text = _run_echo(monkeypatch, tmp_path, "echo_env",
                        "## Source material\n" + _SOURCE)
    assert _SOURCE not in text
    assert "<line containing the prompt elided," in text
    assert "Front" in text   # the normal result line still survives untouched


def test_redact_texts_elides_an_echoed_focus_text(monkeypatch, tmp_path):
    _, text = _run_echo(monkeypatch, tmp_path, "echo_env",
                        "## User instructions\n" + _FOCUS)
    assert _FOCUS not in text
    assert "<line containing the prompt elided," in text


def test_redact_texts_elides_an_echoed_middle_of_a_long_source(
        monkeypatch, tmp_path):
    # A chunk taken from around the source's midpoint, wide enough (200
    # chars, centered) to be guaranteed to contain the narrower 80-char
    # middle-window needle _body_needles derives for a text this long.
    center = len(_SOURCE) // 2
    middle_quote = _SOURCE[max(0, center - 100):center + 100]
    _, text = _run_echo(monkeypatch, tmp_path, "echo_env", middle_quote)
    assert middle_quote not in text
    assert "<line containing the prompt elided," in text


def test_redact_texts_does_not_elide_a_short_legitimate_quote(
        monkeypatch, tmp_path):
    # A card reply is allowed to legitimately repeat a short run of the
    # source (e.g. a drug name and a few surrounding words); anything under
    # the 20-char needle floor must not trip elision.
    short_quote = _SOURCE[:15]
    assert len(short_quote) < 20
    res, text = _run_echo(monkeypatch, tmp_path, "echo_env_in_card",
                          short_quote)
    assert short_quote in text
    assert "<line containing the prompt elided," not in text
    assert "Quote:" in res["text"]   # sanity: the run produced the card


def test_redact_texts_elides_a_full_80_char_quote_even_in_a_normal_reply(
        monkeypatch, tmp_path):
    # Accepted trade-off, deliberate: a card that legitimately quotes a full
    # 80-character run of the source verbatim also gets its log line elided.
    # The run log is a debugging aid, not the card content itself, and a
    # line that repeats 80 straight characters of the learner's material is
    # safe to hide from it even when the reply that produced it was fine.
    full_quote = _SOURCE[:80]
    assert len(full_quote) == 80
    _, text = _run_echo(monkeypatch, tmp_path, "echo_env_in_card", full_quote)
    assert full_quote not in text
    assert "<line containing the prompt elided," in text


def test_run_log_captures_stderr_and_nonzero_exit_on_failure(tmp_path):
    log_path = tmp_path / "ai_last_run.log"
    with pytest.raises(ai_cli.GenerationError):
        _run("fail", log_path=str(log_path))
    text = log_path.read_text(encoding="utf8")
    assert "--- stderr ---" in text
    assert "boom" in text
    assert "exit code: 2" in text


def test_run_log_is_overwritten_not_appended(tmp_path):
    log_path = tmp_path / "ai_last_run.log"
    _run("ok", log_path=str(log_path))
    first_size = log_path.stat().st_size
    _run("ok", log_path=str(log_path))
    second_size = log_path.stat().st_size
    assert second_size == first_size   # identical run, identical log; not doubled


def test_run_log_over_cap_is_truncated_to_its_tail(tmp_path):
    padding = "x" * 80
    lines = [f"line {i} {padding}\n" for i in range(30000)]  # well over 2 MB
    assert len("".join(lines).encode("utf8")) > ai_cli._RUN_LOG_CAP
    log_path = tmp_path / "ai_last_run.log"
    ai_cli._write_run_log(str(log_path), ["cmd"], "PROMPT", lines, "", 0, 1.0)
    data = log_path.read_bytes()
    assert len(data) <= ai_cli._RUN_LOG_CAP
    text = data.decode("utf8", errors="ignore")
    assert "line 29999" in text     # the tail survived
    assert "line 0 " not in text    # the head was cut


def test_connection_writes_no_log(monkeypatch):
    calls = []
    real_write = ai_cli._write_run_log

    def spying_write(log_path, *a, **kw):
        calls.append(log_path)
        return real_write(log_path, *a, **kw)

    monkeypatch.setattr(ai_cli, "_write_run_log", spying_write)
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["ok"], True))
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "working"
    assert calls == [None]   # _write_run_log was called, but asked to log nothing


def test_run_error_result_raises_with_the_clis_own_message_not_exit_code():
    # Real shape from a v2.1.251 claude with an expired login: subtype
    # "success", is_error true, empty stderr, exit 1. The raised message must
    # be the CLI's own explanation, not the generic "assistant exited 1".
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("error_result")
    msg = str(e.value)
    assert "OAuth session expired" in msg
    assert "exited" not in msg


def test_run_error_result_text_never_reaches_parse_cards_json():
    # The is_error message must never be treated as the model's reply: it
    # has to raise, not come back as a successful result carrying that text.
    events = []
    try:
        _run("error_result", on_event=events.append)
        pytest.fail("expected GenerationError")
    except ai_cli.GenerationError:
        pass
    assert not any(e["type"] == "result" for e in events)
    cards, errors = ai_logic.parse_cards_json(
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        {"Study Deck - Basic"}, {"Study Deck - Basic": ["Front", "Back"]})
    assert cards == [] and errors   # confirms this text was never valid card JSON


def test_run_agy_uses_accumulated_deltas_when_result_response_is_empty():
    # The owner's real bug: agy's own stream narrated the whole reply as
    # agent_response text_deltas but its terminal result carried an empty
    # "response". _run_argv must fall back to the accumulated deltas rather
    # than treating this as "no usable reply".
    res = ai_cli._run_argv(FAKE + ["agy_delta_fallback"], "agy", "PROMPT",
                           prompt_via_stdin=False)
    cards, errors = ai_logic.parse_cards_json(
        res["text"], {"Study Deck - Basic"},
        {"Study Deck - Basic": ["Front", "Back"]})
    assert not errors
    assert cards[0]["fields"]["Front"] == "q"


def test_agy_ndjson_fixture_deltas_match_the_final_result_text():
    # tests/agy_stream_samples.ndjson is a captured real agy stream where the
    # terminal result already carries the text; the accumulated deltas must
    # agree with it (a sanity check on the accumulation itself, independent
    # of the fallback path above).
    path = os.path.join(os.path.dirname(__file__), "agy_stream_samples.ndjson")
    deltas, final_text = [], None
    with open(path, encoding="utf8") as fh:
        for line in fh:
            evt = ai_logic.parse_stream_event("agy", line)
            if not evt:
                continue
            if evt.get("delta"):
                deltas.append(evt["delta"])
            if evt["type"] == "result":
                final_text = evt["text"]
    assert final_text == "ok\n"
    assert "".join(deltas) == "ok\n"


def test_run_agy_error_result_raises_the_readable_antigravity_sentence():
    # The captured ERROR result line (tests/agy_stream_samples.ndjson) must
    # reach the caller as the same plain sentence Test connection would give
    # for the identical failure, not the raw agy error text.
    with pytest.raises(ai_cli.GenerationError) as e:
        ai_cli._run_argv(FAKE + ["agy_error_result"], "agy", "PROMPT",
                         prompt_via_stdin=False)
    assert str(e.value) == ("Antigravity received an empty prompt, so it had "
                            "nothing to work from.")


def test_run_agy_permission_denied_stderr_raises_the_readable_sentence():
    with pytest.raises(ai_cli.GenerationError) as e:
        ai_cli._run_argv(FAKE + ["agy_permission_denied"], "agy", "PROMPT",
                         prompt_via_stdin=False)
    assert str(e.value) == ("Antigravity refused a file write; the add-on "
                            "never enables writes, so this run used a tool "
                            "it should not have.")


def test_run_timeout_kills_process():
    with pytest.raises(ai_cli.GenerationError) as e:
        _run("slow", timeout=2)
    assert "time" in str(e.value).lower()


def test_run_cancel_kills_process():
    flag = threading.Event()
    flag.set()
    with pytest.raises(ai_cli.GenerationCancelled):
        _run("slow", cancel=flag.is_set, timeout=30)


def test_run_other_exception_kills_process_and_is_not_masked(monkeypatch):
    """A callback exception (or any non-cancel/timeout failure) mid-run must
    still kill the child and propagate the real exception, not a
    subprocess.TimeoutExpired from an un-killed process.wait()."""
    procs = []
    real_popen = ai_cli.subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(ai_cli.subprocess, "Popen", spying_popen)

    def bad_event(evt):
        raise _CallbackBoom("callback exploded")

    start = time.monotonic()
    with pytest.raises(_CallbackBoom):
        _run("event_then_slow", on_event=bad_event, timeout=30)
    elapsed = time.monotonic() - start

    # The fake CLI sleeps 30s after its one event; a correct kill path exits
    # in well under a second. Bounding this proves proc.wait() didn't block
    # for its full 5s timeout (which is what masks the exception).
    assert elapsed < 5

    assert len(procs) == 1
    proc = procs[0]
    for _ in range(30):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, "child process is still running"
    # poll() only reflects our own wait(); confirm the pid itself is gone.
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


# === I9: end-to-end evidence for all three backends' result parsing, not just
# claude. Neither codex nor agy is installed on this machine, so these drive
# the fake CLI through run_generation()'s real path (build_argv included):
# they prove the parsing/plumbing works for a given event shape, not that the
# shape matches a real vendor binary, which remains unverified.
def test_run_generation_parses_codex_top_level_text_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["codex_top"], True))
    res = ai_cli.run_generation("codex", "/usr/bin/codex", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_parses_codex_nested_item_text_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["codex_nested"], True))
    res = ai_cli.run_generation("codex", "/usr/bin/codex", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_reports_codex_usage_on_a_short_run(monkeypatch, tmp_path):
    # Verified against a live short run: a token_count event carries the real
    # usage, and the terminal turn.completed carries none. _run_argv must
    # remember the last usage it saw and report that instead of 0.
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["codex_short"], True))
    res = ai_cli.run_generation("codex", "/usr/bin/codex", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_parses_antigravity_result_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli, "build_argv",
                        lambda kind, path, mode, scratch, imgs, **kw:
                            (FAKE + ["agy_ok"], True))
    res = ai_cli.run_generation("agy", "/usr/bin/agy", "PROMPT", "quick",
                                str(tmp_path))
    assert '"Front": "q"' in res["text"]
    assert res["tokens"] == 15


def test_run_generation_agy_sends_empty_stdin_and_prompt_last_in_argv(
        monkeypatch, tmp_path):
    """End-to-end through the real build_argv (not stubbed): agy takes the
    prompt in argv rather than on stdin, and -p/the prompt must be the last
    two elements so nothing after them is mistaken for argv noise. Verified
    by what the fake CLI actually received, not by inspecting the argv this
    process constructed."""
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLI_RECORD", str(record))
    fake_agy = os.path.join(os.path.dirname(__file__), "fake_cli.py")
    scratch = str(tmp_path)
    prompt = "PROMPT TEXT FOR AGY"

    res = ai_cli.run_generation("agy", fake_agy, prompt, "quick", scratch)

    assert '"Front": "q"' in res["text"]
    seen = json.loads(record.read_text())
    assert seen["stdin"] == ""
    assert seen["argv"][-2:] == ["-p", prompt]


def test_find_cli_prefers_override(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert ai_cli.find_cli("claude", override=str(exe)) == str(exe)
    assert ai_cli.find_cli("claude", override=str(tmp_path / "nope")) is None


def test_build_argv_claude_thorough_allows_web_only():
    argv, stdin = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough",
                                    "/tmp/scratch", [])
    joined = " ".join(argv)
    assert stdin is True
    assert "--output-format" in joined and "stream-json" in joined
    assert "WebSearch" in joined and "Bash" not in joined
    assert "--max-turns" in joined


def test_build_argv_claude_quick_denies_all_tools():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                "/tmp/scratch", [])
    assert "WebSearch" not in " ".join(argv)


def test_build_argv_claude_images_adds_read_and_dir():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                "/tmp/s", ["/tmp/s/a.png"])
    joined = " ".join(argv)
    assert "Read" in joined and "--add-dir" in joined


def test_build_argv_codex_images_flag():
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick",
                                "/tmp/s", ["/tmp/s/a.png"])
    joined = " ".join(argv)
    assert "exec" in argv and "--image" in joined and "a.png" in joined


def test_build_argv_claude_defaults_to_sonnet_medium():
    # The owner-chosen default: without any config override, claude must never
    # fall through to the account's own default model (the top model for a Max
    # subscriber), which burns credits fast across Thorough's up-to-15-turn loop.
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [])
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "medium"


def test_build_argv_claude_thorough_also_defaults_to_sonnet_medium():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough", "/tmp/s", [])
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "medium"


def test_build_argv_claude_honours_config_overrides():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [],
                                model="opus", effort="high")
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"


def test_build_argv_codex_omits_model_flag_when_unset(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [])
    assert "--model" not in argv


def test_build_argv_codex_emits_model_flag_when_set_and_supported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3")
    assert "--model" in argv and argv[argv.index("--model") + 1] == "o3"


def test_build_argv_codex_omits_model_flag_when_unsupported(monkeypatch):
    # An older codex without a documented --model flag must not be hard-broken
    # by passing it anyway: same guard mechanism as agy's --sandbox.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3")
    assert "--model" not in argv


def test_build_argv_codex_probes_the_exec_subcommand_for_model_support(monkeypatch):
    # The original probe never named a subcommand, so it only ever read the
    # top-level `codex --help`, not `codex exec --help` where exec's own flags
    # (including -m/--model) are actually documented.
    seen = []

    def fake_supports_flag(path, flag, subcommand=None):
        seen.append((flag, subcommand))
        return True
    monkeypatch.setattr(ai_cli, "supports_flag", fake_supports_flag)
    ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                      model="o3")
    assert ("--model", "exec") in seen


def test_build_argv_codex_never_gets_an_effort_flag(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick", "/tmp/s", [],
                                model="o3", effort="high")
    assert "--effort" not in argv


def test_build_argv_agy_sends_model_and_effort_only_when_set_and_supported(
        monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [],
                                model="gemini-3.8-flash-medium", effort="high")
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert argv[argv.index("--effort") + 1] == "high"
    # Nothing set: agy's own default, no flag at all.
    bare, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--model" not in bare and "--effort" not in bare
    # Set, but this binary does not document them: passing either would hard
    # fail every run, so neither is sent.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    unsupported, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s",
                                       [], model="gemini-3.8-flash-medium",
                                       effort="high")
    assert "--model" not in unsupported and "--effort" not in unsupported


def test_build_argv_agy_puts_the_prompt_in_argv_after_dash_p_and_not_on_stdin(
        monkeypatch):
    # agy never reads stdin: --print takes the prompt as its VALUE. Anything
    # after it would be read as the prompt instead, so -p goes last.
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, via_stdin = ai_cli.build_argv("agy", "/usr/bin/agy", "quick",
                                        "/tmp/s", [], prompt="DRAFT CARDS")
    assert via_stdin is False
    assert argv[-2:] == ["-p", "DRAFT CARDS"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--add-dir") + 1] == "/tmp/s"
    assert "--disable-slash-commands" in argv


def test_agy_hint_names_a_fast_model():
    assert "gemini-3.8-flash-low" in ai_cli.BACKENDS["agy"]["model_hint"]


def test_build_argv_agy_omits_disable_slash_commands_when_unsupported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag",
                        lambda path, flag, **kw: flag != "--disable-slash-commands")
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--disable-slash-commands" not in argv


def test_build_argv_agy_refuses_an_oversized_prompt(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    with pytest.raises(ai_cli.GenerationError) as e:
        ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [],
                          prompt="x" * (ai_cli._MAX_ARG_PROMPT + 1))
    assert "too long" in str(e.value)


def test_backends_carry_model_hint_for_the_ui():
    for kind, info in ai_cli.BACKENDS.items():
        assert "model_hint" in info and info["model_hint"]


def test_image_capable_table():
    # All three read an attached image: agy does it with view_file against the
    # scratch dir build_argv hands it as --add-dir.
    for kind in ("claude", "codex", "agy"):
        assert ai_cli.image_capable(kind)


def test_build_argv_agy_adds_sandbox_when_supported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" in argv


def test_build_argv_agy_omits_sandbox_when_unsupported(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: False)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--sandbox" not in argv


def test_build_argv_agy_never_skips_permissions(monkeypatch):
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert "--dangerously-skip-permissions" not in argv


def test_supports_flag_missing_binary_returns_false():
    assert ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox") is False


def test_supports_flag_caches_per_path_and_flag(monkeypatch):
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    calls = []
    real_run = ai_cli.subprocess.run

    def spying_run(argv, *a, **kw):
        calls.append(tuple(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(ai_cli.subprocess, "run", spying_run)
    ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox")
    ai_cli.supports_flag("/no/such/binary-xyz", "--sandbox")
    assert len(calls) == 1


FAKE_HELP = os.path.join(os.path.dirname(__file__), "fake_help_cli.py")


def test_supports_flag_rejects_substring_match(monkeypatch):
    # The original bug: a literal "-m" substring check is satisfied by
    # "--max-turns", so it was effectively always True and never protected
    # anything. Help text that documents an unrelated flag containing this
    # one as a substring must not count as support.
    monkeypatch.setenv("FAKE_HELP_TOP", "--max-turns <N>  cap the turn count")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "-m") is False


def test_supports_flag_rejects_longer_flag_that_contains_this_one(monkeypatch):
    # "--model-provider" documented, but not "--model" itself.
    monkeypatch.setenv("FAKE_HELP_TOP", "--model-provider <name>  set the provider")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is False


def test_supports_flag_accepts_word_boundary_match(monkeypatch):
    monkeypatch.setenv("FAKE_HELP_TOP", "-m, --model <MODEL>  the model to use")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is True


def test_supports_flag_probes_named_subcommands_help(monkeypatch):
    # The top-level help documents nothing; only `exec --help` does, which is
    # where a subcommand's own options actually live for a real codex-style CLI.
    monkeypatch.setenv("FAKE_HELP_TOP", "a generic top-level help screen")
    monkeypatch.setenv("FAKE_HELP_SUBCOMMAND", "exec")
    monkeypatch.setenv("FAKE_HELP_SUB", "-m, --model <MODEL>  the model to use")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is False
    assert ai_cli.supports_flag(FAKE_HELP, "--model", subcommand="exec") is True


def test_supports_flag_still_detects_agy_sandbox_with_tightened_matching(monkeypatch):
    monkeypatch.setenv("FAKE_HELP_TOP", "--sandbox  run in a restricted sandbox")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    assert ai_cli.supports_flag(FAKE_HELP, "--sandbox") is True


def test_supports_flag_shares_one_help_call_across_different_flags(monkeypatch):
    # agy's build_argv probes four separate flags (--sandbox, --model,
    # --effort, --disable-slash-commands) on the same binary; before the help
    # text was cached per (path, subcommand), each was its own `--help` shell-
    # out with its own 10s timeout, all before the run clock even starts.
    monkeypatch.setenv("FAKE_HELP_TOP",
                       "--sandbox  run sandboxed\n-m, --model <M>  the model")
    ai_cli._flag_support_cache.clear()
    ai_cli._help_text_cache.clear()
    calls = []
    real_run = ai_cli.subprocess.run

    def spying_run(argv, *a, **kw):
        calls.append(tuple(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(ai_cli.subprocess, "run", spying_run)
    assert ai_cli.supports_flag(FAKE_HELP, "--sandbox") is True
    assert ai_cli.supports_flag(FAKE_HELP, "--model") is True
    assert len(calls) == 1


def test_resolve_claude_effort_passes_through_known_level():
    assert ai_cli.resolve_claude_effort("high") == "high"


def test_resolve_claude_effort_falls_back_on_typo():
    # A hand-edited config typo must resolve to something valid, not reach
    # `claude --effort <typo>` and die with an opaque CLI error.
    assert (ai_cli.resolve_claude_effort("hihg")
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_resolve_claude_effort_falls_back_on_empty():
    assert (ai_cli.resolve_claude_effort("")
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_build_argv_claude_falls_back_to_default_effort_on_typo():
    argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick", "/tmp/s", [],
                                effort="hihg")
    assert (argv[argv.index("--effort") + 1]
            == ai_cli.BACKENDS["claude"]["default_effort"])


def test_backends_all_have_safety_posture():
    for kind, info in ai_cli.BACKENDS.items():
        assert info.get("safety"), f"{kind} is missing a safety posture string"


# === C1: per-backend mode text must be truthful, not one claim copy-pasted for
# all three backends regardless of what build_argv actually enforces for them.
def test_backends_all_have_mode_text_for_both_modes():
    for kind, info in ai_cli.BACKENDS.items():
        modes = info.get("modes")
        assert modes and modes.get("thorough") and modes.get("quick"), (
            f"{kind} is missing per-mode text")


def test_claude_mode_text_matches_what_build_argv_actually_restricts():
    thorough_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                      "/tmp/s", [])
    modes = ai_cli.BACKENDS["claude"]["modes"]
    # Thorough really does get web tools here, so claiming it may verify online
    # is true; quick really does get none, so claiming no web access is true.
    assert "WebSearch" in " ".join(thorough_argv)
    assert "web" in modes["thorough"].lower()
    assert "WebSearch" not in " ".join(quick_argv)
    assert "no web access" in modes["quick"].lower()


def test_codex_argv_is_mode_invariant_so_its_text_must_not_claim_mode_enforcement():
    # codex's build_argv never branches on mode (always --sandbox read-only, no
    # turn cap set), so neither mode's text may claim online verification
    # actually happens, or that a turn limit is enforced.
    thorough_argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("codex", "/usr/bin/codex", "quick",
                                      "/tmp/s", [])
    assert thorough_argv == quick_argv
    assert "read-only" in thorough_argv
    modes = ai_cli.BACKENDS["codex"]["modes"]
    for text in modes.values():
        assert "verifies facts online" not in text.lower()
    # Thorough is the only one that even mentions verification, so it's the
    # one that must disclaim actually doing it online.
    assert "cannot" in modes["thorough"].lower() and "online" in modes["thorough"].lower()


def test_agy_argv_is_mode_invariant_so_quick_must_not_claim_no_web_access():
    # agy's build_argv never branches on mode either (no --tools/--max-turns
    # equivalent at all), so "Quick draft: ... no web access" (the label
    # every mode used to share) would be false here.
    thorough_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "thorough",
                                         "/tmp/s", [])
    quick_argv, _ = ai_cli.build_argv("agy", "/usr/bin/agy", "quick", "/tmp/s", [])
    assert thorough_argv == quick_argv
    quick_text = ai_cli.BACKENDS["agy"]["modes"]["quick"].lower()
    assert "no web access" not in quick_text
    assert "no tool" not in quick_text


def test_claude_quick_mode_text_matches_build_argv_with_attachments():
    # Finding 2: build_argv grants Read (scoped to the scratch dir) whenever
    # images are attached, in EVERY mode: so quick's text must not claim
    # "no tools at all" once an image is in play, only that it still has no
    # web access. Pinned against the WITH-attachments call specifically,
    # since the no-attachment case above can't catch this drift.
    quick_argv, _ = ai_cli.build_argv("claude", "/usr/bin/claude", "quick",
                                      "/tmp/s", ["/tmp/s/photo.png"])
    tools = quick_argv[quick_argv.index("--tools") + 1]
    assert "Read" in tools
    assert "WebFetch" not in tools and "WebSearch" not in tools
    quick_text = ai_cli.BACKENDS["claude"]["modes"]["quick"].lower()
    assert "no tools at all" not in quick_text
    assert "no web access" in quick_text


# === I7: Test connection ===================================================

def _stub_build_argv(monkeypatch, mode_arg):
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs, **kw: (FAKE + [mode_arg], True))


def test_connection_working_for_a_real_reply(monkeypatch):
    # "badjson" is just a fake CLI that exits 0 with a non-empty result text:
    # test_connection only needs proof the backend answered, not that the reply
    # happens to be card JSON (the trivial test prompt never asks for that).
    _stub_build_argv(monkeypatch, "badjson")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "working"


def test_connection_not_signed_in_gives_a_readable_message_not_raw_stderr(monkeypatch):
    _stub_build_argv(monkeypatch, "not_signed_in")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert "sign in" in res["detail"].lower()
    assert "Run `claude login`" not in res["detail"]   # not the raw stderr


def test_connection_expired_login_reports_the_clis_own_message(monkeypatch):
    # The real defect this guards: an expired login used to come back as
    # "assistant exited 1", defeating the whole point of Test connection.
    _stub_build_argv(monkeypatch, "error_result")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert "OAuth session expired" in res["detail"]
    assert "exited" not in res["detail"]


def test_connection_other_failure_falls_back_to_first_stderr_line(monkeypatch):
    _stub_build_argv(monkeypatch, "fail")
    res = ai_cli.test_connection("claude", "/usr/bin/claude")
    assert res["state"] == "not_working"
    assert res["detail"] == "boom"


def test_connection_cleans_up_its_scratch_dir(monkeypatch, tmp_path):
    seen = {}
    real_mkdtemp = ai_cli.tempfile.mkdtemp

    def spy_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        seen["path"] = p
        return p

    monkeypatch.setattr(ai_cli.tempfile, "mkdtemp", spy_mkdtemp)
    _stub_build_argv(monkeypatch, "badjson")
    ai_cli.test_connection("claude", "/usr/bin/claude")
    assert seen["path"] and not os.path.exists(seen["path"])


def test_readable_cli_error_recognizes_common_auth_phrasing():
    assert "sign in" in ai_cli._readable_cli_error(
        "claude", "Error: not authenticated, please log in").lower()


def test_readable_cli_error_falls_back_to_trimmed_first_line():
    assert ai_cli._readable_cli_error(
        "claude", "boom\nsome traceback\nmore junk") == "boom"


def test_readable_cli_error_explains_an_antigravity_auto_denied_tool():
    raw = ('jetski: no output produced ... a tool required the "write_file" '
           "permission that headless mode cannot prompt for, so it was "
           "auto-denied ...")
    assert ai_cli._readable_cli_error("agy", raw) == (
        "Antigravity refused a file write; the add-on never enables writes, "
        "so this run used a tool it should not have.")


def test_readable_cli_error_explains_an_antigravity_empty_prompt():
    raw = 'Error: empty prompt. Usage: agy --print "your prompt here"'
    assert ai_cli._readable_cli_error("agy", raw) == (
        "Antigravity received an empty prompt, so it had nothing to work from.")


def test_readable_cli_error_handles_empty_input():
    assert ai_cli._readable_cli_error("claude", "") == "no output from the assistant"


def test_readable_cli_error_does_not_blame_antigravity_for_other_backends():
    # "auto-denied" is generic wording a non-agy backend's stderr could contain
    # on its own; only kind == "agy" gets the Antigravity-specific sentence.
    raw = ('the sandboxed tool call was auto-denied by the local policy '
          "before it ran")
    detail = ai_cli._readable_cli_error("claude", raw)
    assert "Antigravity" not in detail
    assert "auto-denied" in detail


def test_detect_backends_skips_disabled_and_honours_preference(monkeypatch):
    from internpearls import ai_cli
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": f"/bin/{kind}")
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "1.0"})
    cfg = {"ai_backend": "codex",
           "ai_backend_enabled": {"claude": True, "codex": True, "agy": False},
           "ai_cli_path": {"claude": "", "codex": "", "agy": ""}}
    res = ai_cli.detect_backends(cfg)
    assert res["chosen"] == "codex"
    assert res["backends"]["agy"]["path"] is None and res["backends"]["agy"]["enabled"] is False
    cfg["ai_backend_enabled"]["codex"] = False
    assert ai_cli.detect_backends(cfg)["chosen"] == "claude"


def test_detect_backends_uses_per_backend_override(monkeypatch):
    from internpearls import ai_cli
    seen = {}
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": seen.setdefault(kind, override) or None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "x"})
    cfg = {"ai_backend": "", "ai_backend_enabled": {"claude": True, "codex": True, "agy": True},
           "ai_cli_path": {"claude": "/opt/claude", "codex": "", "agy": ""}}
    ai_cli.detect_backends(cfg)
    assert seen["claude"] == "/opt/claude" and seen["codex"] == ""
