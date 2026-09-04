# internpearls/ai_cli.py
"""Agent-CLI backends: detection, probes, and streaming subprocess runs.

No aqt imports: callers hand in paths and callbacks, so this stays testable
with a fake CLI script. The security posture lives in build_argv: no backend
ever gets shell access; thorough mode allowlists a small set of file and web
tools, confined to the scratch folder (claude: --restricted plus an
allowlist; codex: an OS sandbox around the scratch cwd); quick stays
tool-free except claude's Read, scoped to the scratch dir, when images are
attached (that is how it views them).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import warnings

from .ai_logic import parse_stream_event, format_duration

# "install_url" is where the AI Backends window's install-guide link sends a
# reader who does not have this CLI yet: the tool's own documentation, never a
# download the add-on fetches or runs itself.
BACKENDS = {
    "claude": {"label": "Claude Code", "exe": "claude",
               "subscription": "Claude Pro or Max subscription",
               "free_tier": "",
               "install_url": "https://docs.anthropic.com/en/docs/claude-code",
               "safety": "File tools confined to the scratch folder, web "
                        "read-only, no shell",
               # Cheaper-but-smart default: without a model flag, claude runs the
               # account default, the top model for a Max subscriber, which burns
               # credits fast across Thorough's up-to-15-turn loop. sonnet/medium
               # is the owner-chosen default for both quality modes; an explicit
               # config value (ai_model/ai_effort) always overrides it. See
               # build_argv.
               "default_model": "sonnet",
               "default_effort": "medium",
               "model_aliases": ["sonnet", "opus", "haiku"],
               "model_hint": "sonnet is the add-on default; haiku is faster, "
                             "opus is stronger; or a full model name",
               "effort_levels": ["low", "medium", "high", "xhigh", "max"],
               # Truthful per-backend: only claude's build_argv actually caps turns
               # and gates web tools by mode (--max-turns, --tools). See build_argv.
               "modes": {
                   "thorough": "Thorough: drafts, may search the web to verify "
                               "facts and read or write files in the scratch "
                               "folder, then self-reviews (up to 15 turns, "
                               "1 to 3 min)",
                   "quick": "Quick draft: exactly one turn, still no web access. "
                           "But if you attach files, it can read the scratch copy "
                           "of exactly those files, to view them (15 to 30 s)"}},
    "codex": {"label": "Codex CLI", "exe": "codex",
              "subscription": "ChatGPT account (free tier: about 50 coding "
                              "messages a day; more on Go, Plus, or Pro)",
              "free_tier": "capped",
              "install_url": "https://github.com/openai/codex",
              "safety": "OS sandbox: commands and writes confined to the "
                       "scratch folder, no network unless Codex is "
                       "configured for it",
              # No forced default here: --model is only passed when the user sets
              # one (see build_argv), and only when supports_flag confirms this
              # codex actually documents it (probed as `codex exec --help`, where
              # the exec subcommand's own options live), so an older codex
              # without the flag isn't hard-broken. No verified effort flag, so
              # no effort control at all.
              "default_model": "",
              "default_effort": "",
              "model_aliases": [],
              "model_hint": "e.g. gpt-5.1-codex, o3, or a full model name; leave "
                            "blank to use the model set in codex's own "
                            "config file",
              "modes": {
                  "thorough": "Thorough: asked to draft, verify, then self-review; "
                              "sandboxed to the scratch folder (writes allowed "
                              "there), no network unless Codex is configured "
                              "for it, so it may not be able to verify anything "
                              "online (1 to 3 min)",
                  "quick": "Quick draft: asked for a single pass with no "
                          "verification; always sandboxed read-only with no "
                          "network either way (15 to 30 s)"}},
    "agy": {"label": "Antigravity CLI", "exe": "agy",
            "subscription": "Google account (free tier, throttled)",
            "free_tier": "throttled",
            "install_url": "https://github.com/google-antigravity/antigravity-cli",
            "safety": "Reads the scratch folder only; cannot write files in "
                     "headless mode; relies on its own approval defaults "
                     "for the rest",
            # agy 1.1.24 documents both --model and --effort in its own --help,
            # and `agy models` lists the ids it accepts. There is no short alias
            # list to close over, so Model stays free text and build_argv sends
            # either flag (the learner's override, or this default) only when
            # supports_flag finds it in this binary's help, so an older agy is
            # not hard-broken. gemini-3.8-flash-low/low is the owner-chosen
            # default: fastest of the catalogue, and the one that does not burn
            # its turn thinking. See build_argv.
            "default_model": "gemini-3.8-flash-low",
            "default_effort": "low",
            "model_aliases": [],
            "model_hint": ("gemini-3.8-flash-low is the add-on default; "
                           "gemini-3.8-flash-medium or gemini-3.1-pro-low are "
                           "stronger, or any id from \"agy models\""),
            "effort_levels": ["low", "medium", "high"],
            # agy's build_argv never restricts tools or turns by mode, so nothing
            # here enforces "no web access" even in Quick; only the prompt's
            # stated workflow differs by mode.
            "modes": {
                "thorough": "Thorough: asked to draft, verify, then self-review, "
                            "but nothing here restricts its tools or turns; it "
                            "runs under Antigravity's own approval defaults, which "
                            "may include web access (1 to 3 min)",
                "quick": "Quick draft: asked for a single pass with no "
                        "verification, but nothing here restricts its tools or "
                        "turns either, so it may still use the web and may still "
                        "take a while (15 to 30 s)"}},
}
_COMMON_DIRS = ("/opt/homebrew/bin", "/usr/local/bin",
                os.path.expanduser("~/.local/bin"),
                os.path.expanduser("~/.npm-global/bin"))
# Idle: how long stdout can go silent before a run is considered hung. Cap: a
# ceiling far above any healthy run, so it only ever catches an idle rule that
# never fires (a backend with no per-line output at all).
_IDLE_S = {"quick": 120, "thorough": 180}
_CAP_S = {"quick": 900, "thorough": 1800}
# Turn budget per generation call, not per card: an automatic (count=None)
# draft still fits inside one call, it just returns more cards in the same
# reply, so this ceiling stays put regardless of how many cards get drafted.
_MAX_TURNS = {"quick": 1, "thorough": 15}
# Image-input support per backend. All three read an attached image: agy does
# it headlessly with view_file against the scratch dir build_argv passes as
# --add-dir, verified against agy 1.1.24.
_IMAGE_CAPABLE = {"claude": True, "codex": True, "agy": True}


class GenerationError(RuntimeError):
    pass


class EmptyReply(GenerationError):
    """A run that finished cleanly but never wrote a usable reply: no crash,
    no error result, just nothing to parse. `events` is the raw stream line
    count; `turns`/`refused_tools` are agy-only diagnostics (None/0 for any
    other backend, since only agy's stream reports a turn count and refused
    sandbox tool calls in a shape worth counting)."""

    def __init__(self, message, events, turns, refused_tools, duration_s):
        super().__init__(message)
        self.events = events
        self.turns = turns
        self.refused_tools = refused_tools
        self.duration_s = duration_s


class GenerationCancelled(RuntimeError):
    pass


def image_capable(kind):
    return _IMAGE_CAPABLE.get(kind, False)


def find_cli(kind, override=""):
    if override:
        return override if (os.path.isfile(override)
                            and os.access(override, os.X_OK)) else None
    exe = BACKENDS[kind]["exe"]
    found = shutil.which(exe)
    if found:
        return found
    for d in _COMMON_DIRS:
        p = os.path.join(d, exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_flag_support_cache = {}
# Word-boundary safe: a bare substring check would count "--max-turns" as proof
# of "-m", and would count "--model-provider" as proof of "--model". `-`/`_`/
# alnum on either side of the match keeps it from firing on a longer flag that
# merely contains this one as a prefix.
_FLAG_RE_CACHE = {}
# agy's build_argv probes four separate flags on the same binary, each a fresh
# `supports_flag` call; without this, that was four `agy --help` shell-outs
# (10s timeout apiece) before the run clock even starts. Cached per
# (path, subcommand) so the help text is fetched once per binary per session,
# independent of which flag is being asked about.
_help_text_cache = {}


def _help_text(path, subcommand):
    key = (path, subcommand)
    if key in _help_text_cache:
        return _help_text_cache[key]
    text = ""
    try:
        r = subprocess.run([path, "--help"], capture_output=True,
                           text=True, timeout=10)
        text += (r.stdout or "") + (r.stderr or "")
    except Exception:
        pass
    if subcommand:
        try:
            r = subprocess.run([path, subcommand, "--help"], capture_output=True,
                               text=True, timeout=10)
            text += (r.stdout or "") + (r.stderr or "")
        except Exception:
            pass
    _help_text_cache[key] = text
    return text


def _flag_regex(flag):
    r = _FLAG_RE_CACHE.get(flag)
    if r is None:
        r = re.compile(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])")
        _FLAG_RE_CACHE[flag] = r
    return r


def supports_flag(path, flag, subcommand=None):
    """True iff `path --help` (and, when given, `path <subcommand> --help`)
    documents `flag`, matched as a whole flag token, not merely a substring
    (see _flag_regex). A subcommand's own options are often documented only
    under `<cli> <subcommand> --help`, not the top-level help (this is why the
    original codex probe, which never passed one, could never actually detect
    -m/--model there); passing one probes that output too, on top of the
    top-level help, and either mentioning the flag counts as support. The
    flags this probes for (`--sandbox`, `--model`, `--effort`,
    `--disable-slash-commands`, among others) are not guaranteed to exist in
    every agy version, so we probe rather than assume: passing an unsupported
    flag would hard-fail every run, which is worse than the safety gap it
    would close. Never raise: a missing/broken binary just means "flag not
    detected", the safe default. Do not replace this with an unconditional
    flag append."""
    key = (path, flag, subcommand)
    if key in _flag_support_cache:
        return _flag_support_cache[key]
    text = _help_text(path, subcommand)
    result = bool(_flag_regex(flag).search(text))
    _flag_support_cache[key] = result
    return result


def probe(kind, path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return {"ok": r.returncode == 0,
            "detail": out[0] if out else f"exit {r.returncode}"}


def detect_backends(cfg):
    """Everything both dialogs need to know about the three CLIs in one pass:
    per backend, whether it is enabled, where it was found (honouring that
    backend's own path override), and whether --version runs. "chosen" is the
    preferred backend when it is enabled and working, else the first enabled
    working one in BACKENDS order, else None. A cheap, free check: never a
    model call (that is test_connection, on demand only)."""
    enabled = cfg.get("ai_backend_enabled") or {}
    overrides = cfg.get("ai_cli_path") or {}
    preferred = cfg.get("ai_backend", "")
    out, chosen = {}, None
    for kind in BACKENDS:
        on = bool(enabled.get(kind, True))
        if not on:
            out[kind] = {"path": None, "ok": False, "detail": "disabled", "enabled": False}
            continue
        override = overrides.get(kind, "") if isinstance(overrides, dict) else ""
        path = find_cli(kind, override)
        if path:
            res = probe(kind, path)
            out[kind] = {"path": path, "ok": res["ok"], "detail": res["detail"], "enabled": True}
        else:
            out[kind] = {"path": None, "ok": False, "detail": "not found", "enabled": True}
        if out[kind]["ok"] and (chosen is None or preferred == kind):
            chosen = kind
    return {"backends": out, "chosen": chosen}


def resolve_claude_effort(effort):
    """Falls back to claude's own default_effort when `effort` isn't one of its
    recognized effort_levels: a hand-edited config typo (or empty string, "no
    override set") must not reach `claude --effort <typo>` and die with an
    opaque CLI error. Model stays free text and is not validated here: the CLI
    itself validates model aliases, and the wizard's Model row already says so."""
    levels = BACKENDS["claude"]["effort_levels"]
    return effort if effort in levels else BACKENDS["claude"]["default_effort"]


_CODEX_CONFIG_PATH = os.path.expanduser("~/.codex/config.toml")
# Double- or single-quoted value, an optional trailing "# comment" after the
# closing quote, both stripped by the two capture groups below (whichever
# quote style matched carries the value; the other group is None).
_TOML_KV_RE = re.compile(
    r'^(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')\s*(?:#.*)?$')


def configured_default(path=None):
    """codex's own default model and reasoning effort, read from its config
    file (`path`, or ~/.codex/config.toml). codex's catalogue isn't listable
    non-interactively (`codex models` needs a terminal), so this is the only
    way the add-on can name what codex would actually use when nothing is
    configured here. A tiny hand parser, not a toml library: only the
    top-level `model` and `model_reasoning_effort` keys matter, as plain
    `key = "value"` (or `key = 'value'`, optionally trailing a `# comment`)
    lines, stopping at the first `[section]` header so a profile-scoped
    override is never mistaken for the CLI's own default.
    Returns (model, effort); either is "" when absent, and both are "" when
    the file is missing, unreadable, not valid text, or carries neither key
    in that shape."""
    path = path or _CODEX_CONFIG_PATH
    try:
        with open(path, encoding="utf8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return "", ""
    model, effort = "", ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            break
        m = _TOML_KV_RE.match(s)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        if key == "model":
            model = val
        elif key == "model_reasoning_effort":
            effort = val
    return model, effort


def model_effort_line(kind, cfg_model="", cfg_effort="", path=None):
    """'Model: <id>, effort: <level> (add-on default)' or '(your setting)', the
    line every backend row shows (AI Backends and the wizard's backend row).
    `cfg_model`/`cfg_effort` are the learner's config values for `kind`, ""
    meaning unset. codex has no add-on-forced default (see build_argv): with
    nothing configured here, its pair comes from its own config.toml
    (configured_default) instead of BACKENDS' default_model/default_effort,
    tagged "(Codex's own setting)" so it isn't mistaken for something this
    add-on chose; a codex with no model there just says so rather than naming
    a blank. `path` is the resolved CLI path, used only for agy: build_argv
    only actually sends --model/--effort when supports_flag confirms this
    binary understands them, so the "(add-on default)" tag here reads the
    same supports_flag results rather than assuming they'd be sent."""
    meta = BACKENDS[kind]
    if cfg_model or cfg_effort:
        model = cfg_model or meta["default_model"]
        effort = cfg_effort or meta["default_effort"]
        tag = "your setting"
    elif kind == "codex":
        model, effort = configured_default()
        if not model:
            return "Model: Codex's own default"
        tag = "Codex's own setting"
    elif kind == "agy":
        model, effort = meta["default_model"], meta["default_effort"]
        if path is not None:
            if not supports_flag(path, "--model"):
                model = ""
            if not supports_flag(path, "--effort"):
                effort = ""
        if not model and not effort:
            return "Model: Antigravity's own default"
        tag = "add-on default"
    else:
        model, effort = meta["default_model"], meta["default_effort"]
        tag = "add-on default"
    bits = [f"Model: {model}" if model else "Model: default"]
    if effort:
        bits.append(f"effort: {effort}")
    return ", ".join(bits) + f" ({tag})"


# macOS caps a process's whole argument block (ARG_MAX, 1 MB in practice), and
# agy takes the prompt as an argument rather than on stdin. A prompt anywhere
# near this size is a pasted book, not a lecture excerpt, so refuse it with a
# sentence rather than letting exec fail with a bare OSError.
_MAX_ARG_PROMPT = 200000


def build_argv(kind, path, mode, scratch, image_paths, model="", effort="",
               prompt=""):
    """Returns (argv, prompt_via_stdin). claude and codex read the prompt on
    stdin: codex exec freezes reading stdin when the prompt is an argument.
    agy never reads stdin at all, so its prompt is the value of -p and rides
    in argv, last, where nothing after it can be mistaken for the prompt.

    `model`/`effort` are whatever the caller resolved from config (empty string
    means "no override set"). claude falls back to its own default_model/
    default_effort (sonnet/medium) when unset, and an unrecognized effort value
    also falls back rather than reaching the CLI (see resolve_claude_effort), so
    it always gets an explicit, cheaper-than-account-default model; codex and
    agy only get --model when a model was actually set AND supports_flag
    confirms this binary understands it, and agy's --effort is gated the same
    way, so an older build of either is never handed a flag it would die on."""
    if kind == "claude":
        argv = [path, "-p", "--output-format", "stream-json", "--verbose",
                "--max-turns", str(_MAX_TURNS[mode]),
                "--setting-sources", ""]
        eff_model = model or BACKENDS["claude"]["default_model"]
        eff_effort = resolve_claude_effort(effort)
        if eff_model:
            argv += ["--model", eff_model]
        if eff_effort:
            argv += ["--effort", eff_effort]
        # Streams the reply as it's generated (stream_event/content_block_delta),
        # which is where the activity feed's delta text comes from.
        if supports_flag(path, "--include-partial-messages"):
            argv += ["--include-partial-messages"]
        tools = []
        if mode == "thorough":
            # --restricted removes Bash and confines the file tools below to
            # the working directories named by --add-dir; without it a named
            # file tool can still reach outside the scratch folder.
            if supports_flag(path, "--restricted"):
                argv += ["--restricted"]
            argv += ["--add-dir", scratch]
            tools += ["Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch"]
        elif image_paths:
            tools.append("Read")
            argv += ["--add-dir", scratch]
        # --tools is an allowlist of what's even available to the model, not just
        # what's auto-approved: naming this small a set here (never Bash/Edit/
        # NotebookEdit/Task) is what makes "worst case: bad card text" true
        # regardless of anything the model tries to do.
        tool_list = ",".join(tools)
        argv += ["--tools", tool_list, "--allowedTools", tool_list]
        if mode == "thorough" and supports_flag(path, "--permission-mode"):
            # Write is on the allowlist above; without this, Claude Code still
            # stops to ask for edit approval it can never receive headlessly.
            argv += ["--permission-mode", "acceptEdits"]
        return argv, True
    if kind == "codex":
        # Thorough gets a real OS sandbox around scratch (reads and writes
        # there, nothing outside it); quick stays read-only, tool-free.
        sandbox = "workspace-write" if mode == "thorough" else "read-only"
        argv = [path, "exec", "--json", "--sandbox", sandbox,
                "--skip-git-repo-check", "-C", scratch]
        if model and supports_flag(path, "--model", subcommand="exec"):
            argv += ["--model", model]
        for p in image_paths:
            argv += ["--image", p]
        return argv, True
    if kind == "agy":
        if len(prompt) > _MAX_ARG_PROMPT:
            raise GenerationError(
                "that source material is too long to send to Antigravity "
                f"({len(prompt):,} characters, the limit is "
                f"{_MAX_ARG_PROMPT:,}); shorten it or split it into two runs")
        # --add-dir makes the scratch dir readable, which is how agy views an
        # attached image (view_file); writes stay off, headlessly auto-denied.
        argv = [path, "--output-format", "stream-json", "--add-dir", scratch]
        if supports_flag(path, "--sandbox"):
            argv += ["--sandbox"]
        # Without this, a prompt containing a /word (a slash command's name)
        # can be expanded by agy instead of read as text.
        if supports_flag(path, "--disable-slash-commands"):
            argv += ["--disable-slash-commands"]
        # Mirrors claude's eff_model/eff_effort: an explicit config value always
        # wins, else the add-on's own default, so agy never runs on whatever
        # its own account default happens to be.
        eff_model = model or BACKENDS["agy"]["default_model"]
        eff_effort = effort or BACKENDS["agy"]["default_effort"]
        if eff_model and supports_flag(path, "--model"):
            argv += ["--model", eff_model]
        if eff_effort and supports_flag(path, "--effort"):
            argv += ["--effort", eff_effort]
        # agy's own idle-agnostic ceiling defaults to 5 minutes, well under our
        # hard cap; raise it to match so agy doesn't cut a healthy long run
        # before our own cap would. Go duration syntax (e.g. "15m").
        if supports_flag(path, "--print-timeout"):
            argv += ["--print-timeout", f"{_CAP_S[mode] // 60}m"]
        # -p and its value go last, on purpose: agy takes the prompt as this
        # flag's value, so anything appended after it would be read as argv
        # noise rather than as an option.
        argv += ["-p", prompt]
        return argv, False
    raise ValueError(kind)


_RUN_LOG_CAP = 2 * 1024 * 1024  # 2 MB; the log keeps the tail, not the head


def _elided_argv(argv, prompt):
    """argv with the literal prompt value replaced by a length-only stand-in,
    for the run log: the prompt carries the learner's own source material
    (and, via active_skills, the bundled/deck/user rules), which must never
    land in a file whose whole purpose is to be pasted somewhere for
    debugging."""
    return [f"<prompt, {len(a)} chars>" if a == prompt else a for a in argv]


_MIN_NEEDLE_LEN = 20


def _body_needles(text, include_windows=False):
    """Short excerpts of `text` to scan stdout/stderr lines against: the
    first 80 characters of the whole (stripped) text, plus the first 80
    characters of each of its first three non-empty lines. When
    `include_windows` is set and the text runs longer than 240 characters,
    two more needles are added, one 80-character window from the middle and
    one from the end, so a partial echo of the body (not just its opening)
    is still caught. Needles under _MIN_NEEDLE_LEN characters are dropped: a
    short needle (a stray word, a JSON key the text happens to share) would
    elide lines that merely echo common text, not lines that actually echo
    the text."""
    needles = []
    stripped = text.strip()
    whole = stripped[:80].strip()
    if len(whole) >= _MIN_NEEDLE_LEN:
        needles.append(whole)
    non_empty = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in non_empty[:3]:
        needle = ln[:80].strip()
        if len(needle) >= _MIN_NEEDLE_LEN and needle not in needles:
            needles.append(needle)
    if include_windows and len(stripped) > 240:
        mid = (len(stripped) - 80) // 2
        window = stripped[mid:mid + 80].strip()
        if len(window) >= _MIN_NEEDLE_LEN and window not in needles:
            needles.append(window)
        tail = stripped[-80:].strip()
        if len(tail) >= _MIN_NEEDLE_LEN and tail not in needles:
            needles.append(tail)
    return needles


def _prompt_needles(prompt):
    """Short excerpts of the whole `prompt` (see _body_needles) to scan
    stdout/stderr lines against. A CLI that echoes the prompt back into a
    stream event (a transcript-style `user_input` event, say) would
    otherwise put the learner's pasted material, and the active skills,
    straight into ai_last_run.log even though the argv itself is elided.
    Kept as the whole-prompt case alongside `_redact_needles`, which also
    covers a CLI that echoes only a piece of the prompt (the source text
    buried mid-prompt, say) rather than the prompt as a whole."""
    return _body_needles(prompt)


def _redact_needles(prompt, redact_texts=()):
    """The needles for one run: `_prompt_needles(prompt)` plus, for each
    non-empty text in `redact_texts` (the learner's own source material,
    focus text, and saved rules, as opposed to the full prompt those sit
    inside), the wider needle set from `_body_needles(..., include_windows=
    True)`. The production prompt (ai_logic.build_prompt) buries the
    learner's material well past its first three lines under bundled skill
    and schema boilerplate, so a CLI that echoes only that material back (a
    transcript-style event carrying just the task content, the realistic
    shape) would match none of the whole-prompt needles alone."""
    needles = list(_prompt_needles(prompt))
    for text in redact_texts:
        if not text or not text.strip():
            continue
        for n in _body_needles(text, include_windows=True):
            if n not in needles:
                needles.append(n)
    return needles


def _contains_needle(text, needles):
    return any(n in text for n in needles)


def _redact_line(line, needles):
    """`line` with any trailing newline stripped, or an elision marker in its
    place when it contains one of `needles`."""
    stripped = line.rstrip("\n")
    if _contains_needle(stripped, needles):
        return f"<line containing the prompt elided, {len(stripped)} chars>"
    return stripped


def _write_run_log(log_path, argv, prompt, lines, err_text, returncode,
                   elapsed_s, needles=()):
    """Best-effort evidence file for one run: the argv (prompt elided), every
    raw stdout line, stderr, the exit code, and the elapsed time. Overwritten
    each run, never appended, and capped at _RUN_LOG_CAP (kept from the tail,
    since the failure is usually near the end of the stream). `log_path` of
    None means the caller (test_connection) wants no log at all. Never
    raises: a failure to write this side-channel file must not turn a real
    generation result (success or failure) into a different error.

    `needles`, from _prompt_needles, redacts any stdout/stderr line that
    echoes the prompt (see _redact_line); the argv itself is elided
    separately by _elided_argv since it holds the whole prompt as one
    argument rather than a line that might merely contain it.

    A single `_GenerateDialog` never runs two generations at once, so this
    one log path is never written by two runs concurrently; nothing here
    needs to guard against that race."""
    if not log_path:
        return
    try:
        parts = ["argv: " + " ".join(_elided_argv(argv, prompt)), "",
                 "--- stdout ---"]
        parts.extend(_redact_line(line, needles) for line in lines)
        parts.extend(["", "--- stderr ---"])
        if err_text:
            parts.extend(_redact_line(ln, needles)
                        for ln in err_text.splitlines())
        parts.extend(["", f"exit code: {returncode}",
                      f"elapsed: {elapsed_s:.1f}s"])
        data = "\n".join(parts).encode("utf8")
        if len(data) > _RUN_LOG_CAP:
            data = data[-_RUN_LOG_CAP:]
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "wb") as fh:
            fh.write(data)
    except Exception:
        pass


def _agy_empty_reply_diagnostics(lines):
    """Scan raw agy stream-json `lines` for what an EmptyReply needs to
    report: the backend's own `num_turns` from its terminal result (None if
    it never sent one), and how many tool steps the sandbox refused (a
    step_update in state ERROR whose message mentions permission). Reads the
    raw lines directly rather than through parse_stream_event, which drops
    a tool step_update's state/message once it has picked a phase."""
    turns = None
    refused = 0
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        event = d.get("event")
        if event == "result":
            r = d.get("result")
            n = r.get("num_turns") if isinstance(r, dict) else None
            if isinstance(n, int) and not isinstance(n, bool):
                turns = n
        elif event == "step_update":
            u = d.get("step_update")
            if not isinstance(u, dict):
                continue
            if u.get("step_type") == "tool" and u.get("state") == "ERROR":
                msg = u.get("message") or u.get("error") or ""
                if isinstance(msg, str) and "permission" in msg.lower():
                    refused += 1
    return turns, refused


def _run_argv(argv, kind, prompt, on_event=None, cancel=None, timeout=120,
              cwd=None, prompt_via_stdin=True, log_path=None,
              redact_texts=(), idle=None):
    # Built once per run, from the prompt (and any extra redact_texts) at
    # hand, and reused for both the run log and the no-usable-reply excerpt
    # below, rather than each re-deriving it separately.
    needles = _redact_needles(prompt, redact_texts)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd,
                                start_new_session=True)
    except OSError as e:
        raise GenerationError(f"could not start the assistant: {e}") from e
    try:
        # stdin is closed either way: a backend that takes the prompt in argv
        # (agy) must not be left holding an open pipe it will never read.
        if prompt_via_stdin:
            proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    result, tokens, rate_limits, error_msg = None, 0, None, None
    # codex can report usage on a non-terminal event (token_count) and carry
    # none at all on the terminal result of a short run; remember whatever
    # usage was last seen on any event so the run still reports it. claude
    # and agy always carry usage on their own terminal result and never hit
    # this fallback in practice.
    last_usage = 0
    # agy only: the running text of the reply, one chunk per agent_response
    # step_update. A stream that ends with a SUCCESS result but an empty
    # "response" (the owner's real failure: the CLI's own final assembly
    # step dropped it) still has this to fall back to.
    agy_deltas = []

    lines = []
    done = threading.Event()

    def _reader():
        for line in proc.stdout:
            lines.append(line)
        done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    seen = 0
    err_text = ""
    last_line_at = start
    # The last phase actually emitted, so a delta arriving after a "Verify
    # online" tool step can put the chip back to "Working": a reply chunk is
    # not itself a verify step, and agy no longer resets phase on every
    # response chunk the way it used to.
    last_phase = None
    try:
        while True:
            while seen < len(lines):
                evt = parse_stream_event(kind, lines[seen])
                seen += 1
                last_line_at = time.monotonic()
                if not evt:
                    continue
                activity = evt.get("activity")
                if activity and on_event:
                    on_event({"type": "activity", "text": activity})
                if evt["type"] == "phase":
                    last_phase = evt["phase"]
                elif evt["type"] == "delta":
                    if last_phase not in (None, "Working") and on_event:
                        on_event({"type": "phase", "phase": "Working"})
                    last_phase = "Working"
                elif evt["type"] == "result":
                    result = evt["text"]
                    if kind == "agy" and not result and agy_deltas:
                        result = "".join(agy_deltas)
                    tokens = max(tokens, evt.get("tokens", 0))
                elif evt["type"] == "error":
                    # An is_error result: never assigned to `result`, so it
                    # can't flow onward as if it were the model's reply.
                    error_msg = evt["text"] or error_msg
                elif evt["type"] == "usage":
                    tokens = max(tokens, evt["tokens"])
                elif evt["type"] == "rate_limits":
                    rate_limits = evt
                if kind == "agy" and evt["type"] == "delta":
                    agy_deltas.append(evt["text"])
                if evt.get("tokens"):
                    last_usage = evt["tokens"]
                if on_event:
                    on_event(evt)
            if done.is_set() and proc.poll() is not None:
                break
            if cancel and cancel():
                raise GenerationCancelled("cancelled")
            now = time.monotonic()
            if idle is not None and now - last_line_at > idle:
                raise GenerationError(
                    f"the assistant went quiet for {format_duration(idle)}; "
                    f"the run log shows what it last sent")
            if now - start > timeout:
                raise GenerationError(
                    f"the assistant ran for over {format_duration(timeout)} "
                    f"and was stopped")
            time.sleep(0.05)
        # stderr is read here, before the finally block below closes it, so
        # a failure message survives the cleanup.
        err_text = (proc.stderr.read() or "").strip()
    except BaseException:
        # Cancel, timeout, a bad event shape, or a broken on_event callback
        # can all land here with the child still running. Kill it before the
        # finally block's proc.wait() below, or that wait blocks for up to 5s
        # on a live process and can raise TimeoutExpired in its place, which
        # would both mask the real exception and skip the rest of cleanup.
        if proc.poll() is None:
            _kill(proc)
        raise
    finally:
        # A killed process still has to be waited on, or it leaks as a zombie;
        # the reader thread and the pipes need to be cleaned up too, or a
        # generation loop leaks file descriptors one run at a time. None of
        # this may raise, or it replaces whatever exception is propagating.
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        t.join(timeout=2)
        if t.is_alive():
            warnings.warn("ai_cli: reader thread outlived cleanup; a pipe "
                          "may be leaking", ResourceWarning)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        _write_run_log(log_path, argv, prompt, lines, err_text,
                       proc.returncode, time.monotonic() - start, needles)
    if not tokens and kind == "codex":
        # Scoped to codex, the one backend the comment above `last_usage`
        # describes: claude and agy always carry usage on their own terminal
        # result, so falling back here for them would only ever repeat a
        # figure their own result already gave, never recover a missing one.
        tokens = last_usage
    if error_msg:
        # The CLI's own explanation beats stderr and beats the bare exit code,
        # regardless of returncode: an is_error result is always fatal. Routed
        # through the same readable-message mapping test_connection uses, so
        # a wizard run gets the same plain sentence a Test connection click
        # would have gotten for the identical failure.
        raise GenerationError(_readable_cli_error(kind, error_msg))
    if proc.returncode != 0:
        raise GenerationError(_readable_cli_error(
            kind, err_text or f"assistant exited {proc.returncode}"))
    if not result:
        duration_s = round(time.monotonic() - start, 1)
        turns, refused_tools = ((_agy_empty_reply_diagnostics(lines))
                                if kind == "agy" else (None, 0))
        if kind == "agy":
            msg = (f"{BACKENDS[kind]['label']} finished after "
                  f"{format_duration(duration_s)} without writing a reply")
            if refused_tools:
                msg += (f"; it tried {refused_tools} tool(s) the sandbox "
                       f"refused")
        else:
            msg = (f"the assistant produced no usable reply ({len(lines)} "
                  f"events seen). The full stream is in ai_last_run.log "
                  f"inside the add-on's user_files folder.")
        raise EmptyReply(msg, events=len(lines), turns=turns,
                         refused_tools=refused_tools, duration_s=duration_s)
    return {"text": result, "tokens": tokens, "rate_limits": rate_limits,
            "duration_s": round(time.monotonic() - start, 1)}


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_generation(kind, path, prompt, mode, scratch, image_paths=(),
                   on_event=None, cancel=None, timeout=None, model="", effort="",
                   log_path=None, redact_texts=()):
    argv, via_stdin = build_argv(kind, path, mode, scratch, list(image_paths),
                                 model=model or "", effort=effort or "",
                                 prompt=prompt)
    # codex can go a whole healthy turn (verified live: a 5s run's only
    # silent gap was 5s, with no item.started at all) with nothing on
    # stdout between turn.started and its terminal item; an idle rule would
    # kill that run sooner than the old hard cap ever did. claude and agy
    # stream enough that the idle rule still means something for them.
    idle = None if kind == "codex" else _IDLE_S[mode]
    return _run_argv(argv, kind, prompt, on_event=on_event, cancel=cancel,
                     timeout=timeout or _CAP_S[mode], cwd=scratch,
                     prompt_via_stdin=via_stdin, log_path=log_path,
                     redact_texts=redact_texts, idle=idle)


_TEST_PROMPT = "Reply with exactly one word: ok"
_AUTH_HINTS = ("not logged in", "not authenticated", "unauthoriz", "auth error",
              "please sign in", "please log in", "log in with", "no credentials",
              "authentication required", "run `claude login`", "run `codex login`")


def _readable_cli_error(kind, raw):
    """Turn a CLI's raw stderr into one short, human sentence. Never shown
    verbatim: a not-signed-in CLI's stderr is often a multi-line stack of its
    own auth-library noise, which is exactly what a user waiting on "Test
    connection" should not have to parse to learn "go sign in".

    `kind` gates the Antigravity-specific sentences below to agy: claude and
    codex stderr can legitimately contain the same words ("auto-denied",
    "empty prompt") without being about Antigravity at all, and naming the
    wrong product would be worse than the generic fallback."""
    text = (raw or "").strip()
    low = text.lower()
    if any(h in low for h in _AUTH_HINTS):
        return "not signed in: run it once in a terminal and sign in there"
    if kind == "agy":
        # Antigravity's own two failure sentences, both long and both about
        # something the reader cannot act on as written.
        if "auto-denied" in low or "headless mode cannot prompt" in low:
            return ("Antigravity refused a file write; the add-on never "
                    "enables writes, so this run used a tool it should not "
                    "have.")
        if "empty prompt" in low:
            return ("Antigravity received an empty prompt, so it had "
                    "nothing to work from.")
    first_line = text.splitlines()[0] if text else "no output from the assistant"
    return first_line[:200]


def test_connection(kind, path, timeout=45):
    """Run a trivial prompt through the real backend and report whether it
    actually works, not just whether the binary executes (that's probe()'s
    job, and --version succeeding is not proof of a working, signed-in
    backend: a CLI that has never been signed into still answers
    --version). Costs one real, billed model turn, so callers must only run
    this on demand, never automatically.

    Returns {"state": "working"|"not_working", "detail": <short message>}.
    Never raises: a backend that can't even start comes back as
    "not_working" with its own readable detail, same as an auth failure.
    """
    scratch = tempfile.mkdtemp(prefix="ip-aigen-test-")
    try:
        run_generation(kind, path, _TEST_PROMPT, "quick", scratch, timeout=timeout)
        return {"state": "working", "detail": "connected and responding"}
    except GenerationError as e:
        return {"state": "not_working", "detail": _readable_cli_error(kind, str(e))}
    except Exception as e:
        return {"state": "not_working", "detail": _readable_cli_error(kind, str(e))}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
